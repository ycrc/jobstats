"""Publish per-job jobstats utilization summaries to Kafka for the Druid pipeline.

Given a computed Jobstats object, `build_record` produces a flat, typed JSON
record (the queryable analytics data) plus the opaque JS1 blob (for report
reconstruction by the Druid read-back). `send` produces it to the single YCRC
Kafka broker as plain JSON, keyed by cluster:jobid:@end.

Field names mirror the slurm_accounting datasource (@-prefixed Slurm timestamps,
`jobid` long, `cluster` string) so the two datasources join cleanly on
(cluster, jobid, __time) where __time == @end == job end.

Schema version 2 additionally denormalizes the job's own sacct dimensions
(username, account, partition, qos, state, elapsed, total_cpus, total_nodes,
time_limit, tres_alloc) so that grouped aggregates need no join at all. That
join is not merely slow but unusable: Bouchet alone produces ~100k jobs/day and
Druid rejects any subquery over 100k rows, so even a one-day job-level join
fails outright. Rows written before the version-2 cutover carry nulls for these
columns -- filter `WHERE schema_version = 2` when querying them.

The opaque `js1` blob is unchanged and remains the point of the datasource for
`jobstats <jobid>`: it is the only durable copy of a job's report once
Prometheus retention expires (see druid_handler.py).
"""
import datetime
import json

import config as c

SAMPLING_PERIOD = c.SAMPLING_PERIOD


def _iso_utc(epoch):
    """Naive-UTC ISO string (e.g. '2026-07-23T19:09:03'), matching slurm_accounting's @start/@end."""
    return datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc).replace(tzinfo=None).isoformat()


def _pct(used, alloc):
    return round(100.0 * used / alloc, 1) if alloc else None


def _int_or_none(v):
    """sacct numeric fields arrive as strings; non-numeric sentinels return None.

    TimelimitRaw in particular is 'UNLIMITED' or 'Partition_Limit' for jobs with
    no explicit limit, and jobstats leaves those as the raw string.
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v):
    """Empty sacct fields become null, matching slurm_accounting's convention."""
    if v is None:
        return None
    v = str(v).strip()
    return v or None


class JobstatsKafkaHandler:
    def __init__(self):
        self._producer = None

    def build_record(self, js):
        """Build the flat Kafka record from a computed Jobstats object.

        Reuses the aggregate tuples jobstats computes in parse_stats(); a metric
        is emitted only when its *_error_code is clean, else null.
        """
        # overall status
        if js.diff < 2 * SAMPLING_PERIOD:
            status = "short"
        elif len(js.sp_node) == 0:
            status = "nodata"
        elif any(getattr(js, f"{k}_error_code", 0)
                 for k in ("cpu_util", "cpu_mem", "gpu_util", "gpu_mem")):
            status = "partial"
        else:
            status = "ok"

        # Job dimensions, denormalized from the sacct read jobstats already does
        # in __get_job_info(). These cost nothing extra and exist so aggregates
        # never have to join slurm_accounting: a job-level join blows Druid's
        # 100k subquery-row limit within a single day at Bouchet's job rate.
        # Names and units mirror slurm_accounting so queries read the same
        # against either datasource.
        timelimit_min = _int_or_none(getattr(js, "timelimitraw", None))

        rec = {
            "schema_version": 2,
            "cluster": js.cluster,
            "jobid": int(js.jobidraw),
            "@end": _iso_utc(js.end),
            "@start": _iso_utc(js.start),
            "username": _str_or_none(getattr(js, "user", None)),
            "account": _str_or_none(getattr(js, "account", None)),
            "partition": _str_or_none(getattr(js, "partition", None)),
            "qos": _str_or_none(getattr(js, "qos", None)),
            "state": _str_or_none(getattr(js, "state", None)),
            "elapsed": int(js.diff),
            "total_cpus": _int_or_none(getattr(js, "ncpus", None)),
            "total_nodes": _int_or_none(getattr(js, "nnodes", None)),
            # sacct reports TimelimitRaw in minutes; slurm_accounting stores seconds.
            "time_limit": timelimit_min * 60 if timelimit_min is not None else None,
            "tres_alloc": _str_or_none(getattr(js, "tres", None)),
            "cpu_seconds_used": None,
            "cpu_efficiency_pct": None,
            "mem_used_bytes": None,
            "mem_alloc_bytes": None,
            "mem_efficiency_pct": None,
            "gpu_count": int(js.gpus) if js.gpus else 0,
            "gpu_util_mean_pct": None,
            "gpu_mem_used_bytes": None,
            "gpu_mem_total_bytes": None,
            "status": status,
            "js1": "JS1:" + js.report_job_json(encode=True),
        }

        # CPU time efficiency
        cpu_used, cpu_alloc, _ = getattr(js, "cpu_util_total__used_alloc_cores", (0, 0, 0))
        if getattr(js, "cpu_util_error_code", 0) == 0 and cpu_alloc:
            rec["cpu_seconds_used"] = round(cpu_used, 1)
            rec["cpu_efficiency_pct"] = _pct(cpu_used, cpu_alloc)

        # CPU memory efficiency
        mem_used, mem_alloc, _ = getattr(js, "cpu_mem_total__used_alloc_cores", (0, 0, 0))
        if getattr(js, "cpu_mem_error_code", 0) == 0 and mem_alloc:
            rec["mem_used_bytes"] = int(mem_used)
            rec["mem_alloc_bytes"] = int(mem_alloc)
            rec["mem_efficiency_pct"] = _pct(mem_used, mem_alloc)

        # GPU utilization / memory (only for GPU jobs with clean data)
        if js.gpus and getattr(js, "gpu_util_error_code", 0) == 0 and hasattr(js, "gpu_util_total__util_gpus"):
            util_sum, gpu_n = js.gpu_util_total__util_gpus
            rec["gpu_util_mean_pct"] = round(util_sum / gpu_n, 1) if gpu_n else None
        if js.gpus and getattr(js, "gpu_mem_error_code", 0) == 0 and hasattr(js, "gpu_mem_total__used_alloc"):
            gpu_used, gpu_total = js.gpu_mem_total__used_alloc
            rec["gpu_mem_used_bytes"] = int(gpu_used)
            rec["gpu_mem_total_bytes"] = int(gpu_total)

        return rec

    def _get_producer(self):
        if self._producer is None:
            from kafka import KafkaProducer  # lazy: only needed for real sends
            self._producer = KafkaProducer(
                bootstrap_servers=c.KAFKA_CONFIG["bootstrap_servers"].split(","),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8"),
                acks="all",
            )
        return self._producer

    def send(self, records, dry_run=False):
        """Produce records to Kafka. With dry_run, print them and send nothing."""
        topic = c.KAFKA_CONFIG["topic"]
        if dry_run:
            for rec in records:
                print(json.dumps(rec, indent=2))
            return
        producer = self._get_producer()
        for rec in records:
            key = "%s:%s:%s" % (rec["cluster"], rec["jobid"], rec["@end"])
            producer.send(topic, key=key, value=rec)
        producer.flush()

    def close(self):
        if self._producer is not None:
            self._producer.close()
            self._producer = None
