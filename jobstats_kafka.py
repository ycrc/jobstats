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

# Resolve the script's real location (through a symlink) so we can import the
# jobstats modules that live alongside it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DEVNULL = open(os.devnull, "w")


def enumerate_jobids(cluster, start, end):
    """Return raw job ids that finished in [start, end] for a cluster.

    Uses `sacct --duplicates` so every run of a requeued job is considered (a
    requeued job keeps its id but produces a separate record per run), and
    excludes jobs that are still PENDING/RUNNING or have not yet ended. Ids are
    de-duplicated preserving order. `start`/`end` are sacct time strings
    (e.g. "now-1hours", "2026-07-23T00:00:00").

    Note: jobstats itself reports the *latest* run of a given id, so a requeued
    job yields one record (its most recent run), not one per run.
    """
    cmd = ["sacct", "-X", "-n", "-P", "-a", "--duplicates",
           "-S", start, "-E", end, "-o", "JobIDRaw,State,End", "-M", cluster]
    out = subprocess.check_output(cmd, stderr=DEVNULL).decode("utf-8")
    ordered, seen = [], set()
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        jobidraw, state, jend = parts[0], parts[1].upper(), parts[2].strip().upper()
        if state.startswith(("RUNNING", "PENDING")) or jend in ("", "UNKNOWN"):
            continue
        if jobidraw and jobidraw not in seen:
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
