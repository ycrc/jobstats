#!/bin/bash
# Compute-node epilog: publish this job's jobstats utilization to Kafka.
#
# Runs synchronously with a hard timeout so a slow Prometheus/Kafka cannot wedge
# the node's return to service, and leaves nothing detached. It must never fail
# the epilog, so it always exits 0.

# Path to the jobstats_kafka entry point. This is a system/epilog tool, not
# user-facing, so it lives in its own dir with its own venv under /apps/services
# (the script's shebang points at that venv). This path is intended to be the
# same on every cluster; override JOBSTATS_KAFKA_BIN only if it ever differs.
JOBSTATS_KAFKA_BIN="${JOBSTATS_KAFKA_BIN:-/apps/services/jobstats_kafka/jobstats_kafka.py}"
[ -x "$JOBSTATS_KAFKA_BIN" ] || exit 0

# jobstats aggregates every node of the job via Prometheus, so emit exactly once
# per job -- only from the first node of the allocation.
FIRST_NODE="$(scontrol show hostnames "$SLURM_JOB_NODELIST" 2>/dev/null | head -1)"
if [ -n "$FIRST_NODE" ] && [ "$SLURMD_NODENAME" != "$FIRST_NODE" ]; then
    exit 0
fi

# $SLURM_JOB_ID is the raw per-task id (array tasks included), which is what
# `sacct -j` and jobstats resolve on. Bound the whole thing with a hard timeout.
timeout 15 "$JOBSTATS_KAFKA_BIN" \
    --cluster "${SLURM_CLUSTER_NAME:-$CLUSTER}" \
    --jobid "$SLURM_JOB_ID" >/dev/null 2>&1

exit 0
