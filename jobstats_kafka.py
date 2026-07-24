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
import io
import os
import subprocess
import sys
import time

# Resolve the script's real location (through a symlink) so we can import the
# jobstats modules that live alongside it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

DEVNULL = open(os.devnull, "w")


def _chunks(seq, n):
    """Yield successive n-sized chunks of seq (to keep `sacct -j` arg lists sane)."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def enumerate_jobids(cluster, start, end, min_elapsed):
    """Return raw job ids that FINISHED in [start, end] and ran long enough to
    have Prometheus data.

    Requeued array tasks are the tricky case: a single JobIDRaw can carry both a
    past *terminal* record (e.g. PREEMPTED, real End) **and** a current PENDING
    record (End=Unknown). A *windowed* sacct (`-S/-E`) returns the terminal
    record -- the pending one has no time overlap with the window -- but
    `jobstats -j <id>` resolves the job with **no** window and sees the *current*
    (pending) record, then fails on it. So we resolve in two phases to match
    exactly what jobstats will see:

      1. windowed query -> candidate ids that had activity in [start, end].
      2. an *unwindowed* `-j <candidates>` query -> the current record per id
         (the same record jobstats resolves; no `-s`, no `--duplicates`). Keep
         only ids whose current record has a real past End and ran
         >= min_elapsed.

    A job that was requeued and is pending/running again has End=Unknown in phase
    2 and is dropped, so we never hand jobstats a pending job. Jobs shorter than
    `min_elapsed` (no Prometheus data) are skipped too. `start`/`end` are sacct
    time strings (e.g. "now-1hours").
    """
    # %s so End/Elapsed are integers we can range-check.
    env = dict(os.environ, SLURM_TIME_FORMAT="%s")
    now = int(time.time())

    # Phase 1: which ids had any activity in the window.
    cmd = ["sacct", "-X", "-n", "-P", "-a", "-S", start, "-E", end,
           "-o", "JobIDRaw", "-M", cluster]
    out = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8")
    candidates, seen = [], set()
    for line in out.splitlines():
        jid = line.strip().split("|")[0]
        if jid and jid not in seen:
            seen.add(jid)
            candidates.append(jid)
    if not candidates:
        return []

    # Phase 2: resolve each candidate's *current* record the way jobstats does
    # (unwindowed, no -s, no --duplicates) and keep only genuinely-finished,
    # long-enough jobs.
    ordered, kept = [], set()
    for chunk in _chunks(candidates, 1000):
        cmd = ["sacct", "-X", "-n", "-P", "-j", ",".join(chunk),
               "-o", "JobIDRaw,ElapsedRaw,End", "-M", cluster]
        out = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8")
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            jobidraw, elapsed, jend = parts[0], parts[1], parts[2]
            # not finished (pending/running/requeued-again have End=Unknown)
            if not jend.isdigit() or int(jend) > now:
                continue
            # too short to have Prometheus data
            if not elapsed.isdigit() or int(elapsed) < min_elapsed:
                continue
            if jobidraw and jobidraw not in kept:
                kept.add(jobidraw)
                ordered.append(jobidraw)
    return ordered


def _is_nodata(msg):
    """True if a jobstats error just means the job had no utilization data.

    jobstats calls sys.exit(1) both for genuinely-empty jobs (short/old jobs,
    exporter gaps -- expected, and slurm_accounting still has the job) and for
    real failures (Prometheus/query errors, lookup failures). We only want to
    quietly skip the former; the latter should be surfaced.
    """
    m = msg.lower()
    return ("no data was found" in m
            or "no job statistics" in m
            or "very short" in m)


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
            if _is_nodata(msg):
                skipped += 1  # slurm_accounting still has the job; no util row needed
            else:
                failures += 1
                print(f"ERROR: job {jobid} on {cluster}: {msg or 'unknown error'}",
                      file=sys.stderr)

    try:
        if records:
            handler.send(records, dry_run=args.dry_run)
    except Exception as e:
        failures += 1
        print(f"ERROR: failed to produce to Kafka: {e}", file=sys.stderr)
    finally:
        handler.close()

    if skipped:
        print(f"skipped {skipped} job(s) with no utilization data", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
