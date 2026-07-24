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
import contextlib
import datetime
import io
import os
import subprocess
import sys
import time

# Resolve the script's real location (through a symlink) so we can import the
# jobstats modules that live alongside it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DEVNULL = open(os.devnull, "w")


def enumerate_jobids(cluster, start, end, min_elapsed):
    """Return raw job ids that FINISHED in [start, end] and ran long enough to
    have Prometheus data.

    A single windowed sacct read (no `-s`, no `--duplicates`) -- fast, one call.
    We keep ids whose windowed record has a real past End and ran >= min_elapsed;
    still-running jobs have End=Unknown and drop out, and short jobs (no
    Prometheus data) are skipped.

    We deliberately do NOT try to pre-detect requeued jobs here. A requeued task
    can show a past terminal record in the window (e.g. PREEMPTED) while its
    *current* record -- the one `jobstats -j` resolves unwindowed -- is PENDING.
    Rather than pay for an expensive unwindowed `sacct -j` to catch that, we let
    jobstats reject it: it bails early (during job lookup, before any Prometheus
    query) with a "PENDING job" message that main() classifies as a quiet skip.
    `start`/`end` are sacct time strings (e.g. "now-1hours").
    """
    # %s so End/Elapsed are integers we can range-check.
    env = dict(os.environ, SLURM_TIME_FORMAT="%s")
    cmd = ["sacct", "-X", "-n", "-P", "-a", "-S", start, "-E", end,
           "-o", "JobIDRaw,ElapsedRaw,End", "-M", cluster]
    out = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8")
    now = int(time.time())
    ordered, seen = [], set()
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        jobidraw, elapsed, jend = parts[0], parts[1], parts[2]
        # not finished (running jobs have End=Unknown)
        if not jend.isdigit() or int(jend) > now:
            continue
        # too short to have Prometheus data
        if not elapsed.isdigit() or int(elapsed) < min_elapsed:
            continue
        if jobidraw and jobidraw not in seen:
            seen.add(jobidraw)
            ordered.append(jobidraw)
    return ordered


def _is_expected_skip(msg):
    """True if a jobstats error is an expected 'nothing to emit' case, not a bug.

    jobstats calls sys.exit(1) for several situations. Some are expected and
    slurm_accounting still has the job, so a missing utilization row is fine:
      - genuinely-empty jobs (short/old jobs, exporter gaps): "no data was found"
      - very short jobs that only get seff output
      - a requeued task whose current record is PENDING (jobstats resolves the
        job unwindowed and sees the pending record): "...it is a PENDING job."
    Everything else (Prometheus/query errors, lookup failures) is a real failure
    we want to surface.
    """
    m = msg.lower()
    return ("no data was found" in m
            or "no job statistics" in m
            or "very short" in m
            or "pending job" in m)


def main():
    parser = argparse.ArgumentParser(description="Publish jobstats utilization to Kafka.")
    parser.add_argument("-c", "--cluster", default=None,
                        help="Cluster name (default $SLURM_CLUSTER_NAME / $CLUSTER).")
    parser.add_argument("-j", "--jobid", action="append", default=[], type=int,
                        help="Raw Slurm job id (repeatable).")
    parser.add_argument("--start", default=None,
                        help="Window mode: enumerate jobs that finished since this "
                             "sacct time (e.g. now-1hours). Excludes pending/running "
                             "and jobs too short to have Prometheus data.")
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

    # config.PROM_SERVER is built from $CLUSTER at import time, so set it first.
    os.environ["CLUSTER"] = cluster
    import config
    from jobstats import Jobstats
    from kafka_handler import JobstatsKafkaHandler

    # Collect the job ids: explicit --jobid plus the window enumeration.
    jobids, seen = [], set()
    enumerated = enumerate_jobids(cluster, args.start, args.end,
                                  2 * config.SAMPLING_PERIOD) if args.start else []
    for jid in [str(j) for j in args.jobid] + enumerated:
        if jid not in seen:
            seen.add(jid)
            jobids.append(jid)

    handler = JobstatsKafkaHandler()
    records = []
    skipped = 0   # jobs with no utilization data -- expected, not an error
    failures = 0
    for jobid in jobids:
        # jobstats prints to stdout (e.g. seff for very short jobs) and sys.exit()s
        # for jobs with no data; keep stdout quiet but capture stderr so we can tell
        # an expected "no data" job (skip quietly) from a real failure (surface it).
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(DEVNULL), contextlib.redirect_stderr(err):
                js = Jobstats(jobid=jobid, cluster=cluster,
                              prom_server=config.PROM_SERVER, force_recalc=True)
            records.append(handler.build_record(js))
        except (Exception, SystemExit):
            msg = err.getvalue().strip()
            if _is_expected_skip(msg):
                skipped += 1  # slurm_accounting still has the job; no util row needed
            else:
                failures += 1
                print(f"ERROR: job {jobid} on {cluster}: {msg or 'unknown error'}",
                      file=sys.stderr)

    sent = 0
    try:
        if records:
            handler.send(records, dry_run=args.dry_run)
            sent = len(records)
    except Exception as e:
        failures += 1
        print(f"ERROR: failed to produce to Kafka: {e}", file=sys.stderr)
    finally:
        handler.close()

    # One-line summary per run so a cron log isn't silent: local timestamp, the
    # window (or explicit job count), and how many records went out / were
    # skipped (short/old/pending) / errored.
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    scope = f"start={args.start} end={args.end}" if args.start else f"jobids={len(jobids)}"
    verb = "would_send" if args.dry_run else "sent"
    print(f"[{ts}] {cluster} {scope} {verb}={sent} skipped={skipped} errors={failures}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
