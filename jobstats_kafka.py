#!/apps/services/jobstats_kafka/venv/bin/python3
"""Compute a job's jobstats utilization and publish it to Kafka (for Druid).

Constructs a Jobstats object per job (one sacct + one Prometheus query, reusing
jobstats' own compute), builds a flat JSON record, and produces it to the Kafka
topic in config.KAFKA_CONFIG. Driven by the compute-node epilog for live jobs;
also usable standalone for testing / backfill.

    # explicit job(s)
    jobstats_kafka --cluster bouchet --jobid 12345 --dry-run
    jobstats_kafka --cluster bouchet --jobid 12345 --jobid 12346

    # every job that finished in a window (for an hourly timer / backfill)
    jobstats_kafka --cluster bouchet --start now-1hours
    jobstats_kafka --cluster bouchet --start 2026-07-23T00:00:00 --end 2026-07-24T00:00:00
"""
import argparse
import os
import subprocess
import sys
import time

# Resolve the script's real location (through a symlink) so we can import the
# jobstats modules that live alongside it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DEVNULL = open(os.devnull, "w")

# Terminal job states to enumerate -- excludes PENDING/RUNNING/SUSPENDED/REQUEUED
# etc. so those never enter the list. Extend via $JOBSTATS_KAFKA_STATES if needed.
TERMINAL_STATES = os.environ.get("JOBSTATS_KAFKA_STATES", "CD,F,CA,TO,OOM,NF,BF,DL,PR")


def enumerate_jobids(cluster, start, end):
    """Return raw job ids that finished in [start, end] for a cluster.

    Filters at the sacct level with `-s TERMINAL_STATES` so only finished jobs
    come back (no PENDING/RUNNING). We deliberately do NOT pass `--duplicates`:
    `jobstats -j <id>` reports an id's *current* record, so surfacing an older
    completed run of an id that is now pending/running again only makes jobstats
    fail on the current pending state. One row per id (its latest run) is exactly
    what jobstats can report. `start`/`end` are sacct time strings
    (e.g. "now-1hours", "2026-07-23T00:00:00").
    """
    # %s so End is an epoch we can range-check; a finished job's End is in the past.
    env = dict(os.environ, SLURM_TIME_FORMAT="%s")
    cmd = ["sacct", "-X", "-n", "-P", "-a", "-s", TERMINAL_STATES,
           "-S", start, "-E", end, "-o", "JobIDRaw,End", "-M", cluster]
    out = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8")
    now = int(time.time())
    ordered, seen = [], set()
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        jobidraw, jend = parts[0], parts[1].strip()
        # safety net: a finished job has a real end time in the past
        if not jobidraw or not jend.isdigit() or int(jend) > now:
            continue
        if jobidraw not in seen:
            seen.add(jobidraw)
            ordered.append(jobidraw)
    return ordered


def main():
    parser = argparse.ArgumentParser(description="Publish jobstats utilization to Kafka.")
    parser.add_argument("-c", "--cluster", default=None,
                        help="Cluster name (default $SLURM_CLUSTER_NAME / $CLUSTER).")
    parser.add_argument("-j", "--jobid", action="append", default=[], type=int,
                        help="Raw Slurm job id (repeatable).")
    parser.add_argument("--start", default=None,
                        help="Window mode: enumerate jobs that finished since this "
                             "sacct time (e.g. now-1hours). Excludes pending/running jobs.")
    parser.add_argument("--end", default="now",
                        help="End of the enumeration window (sacct time; default now).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the JSON record(s) instead of producing to Kafka.")
    args = parser.parse_args()

    if not args.jobid and not args.start:
        parser.error("provide --jobid and/or --start (window mode)")

    cluster = args.cluster or os.environ.get("SLURM_CLUSTER_NAME") or os.environ.get("CLUSTER")
    if not cluster:
        parser.error("no cluster given and neither SLURM_CLUSTER_NAME nor CLUSTER is set")

    # Collect the job ids: explicit --jobid plus the window enumeration.
    jobids, seen = [], set()
    for jid in [str(j) for j in args.jobid] + (
            enumerate_jobids(cluster, args.start, args.end) if args.start else []):
        if jid not in seen:
            seen.add(jid)
            jobids.append(jid)

    # config.PROM_SERVER is built from $CLUSTER at import time, so set it first.
    os.environ["CLUSTER"] = cluster
    import config
    from jobstats import Jobstats
    from kafka_handler import JobstatsKafkaHandler

    handler = JobstatsKafkaHandler()
    records = []
    failures = 0
    for jobid in jobids:
        # jobstats calls sys.exit(1) (SystemExit) for jobs with no Prometheus
        # data (too old / too short), so catch that too -- one such job must not
        # abort a backfill of many. Such jobs simply produce no record.
        try:
            js = Jobstats(jobid=jobid, cluster=cluster,
                          prom_server=config.PROM_SERVER, force_recalc=True)
            records.append(handler.build_record(js))
        except (Exception, SystemExit) as e:
            failures += 1
            print(f"ERROR: no record for job {jobid} on {cluster}: {e or 'no data'}",
                  file=sys.stderr)

    try:
        if records:
            handler.send(records, dry_run=args.dry_run)
    except Exception as e:
        failures += 1
        print(f"ERROR: failed to produce to Kafka: {e}", file=sys.stderr)
    finally:
        handler.close()

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
