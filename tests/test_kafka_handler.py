import base64
import datetime
import gzip
import json
import os

from jobstats import Jobstats
from kafka_handler import JobstatsKafkaHandler

DEVNULL = open(os.devnull, "w")
SLURM_VERSION = "25.11.5"  # major 25 -> no sluid field

FIELDS = ",".join([
    "jobidraw", "start", "end", "cluster", "alloctres", "admincomment", "user",
    "account", "state", "nnodes", "ncpus", "reqmem", "qos", "partition",
    "timelimitraw", "jobname",
])
COLS = ("JobIDRaw|Start|End|Cluster|AllocTRES|AdminComment|User|Account|"
        "State|NNodes|NCPUS|ReqMem|QOS|Partition|TimelimitRaw|JobName\n")


def _js1(nodes, gpus, total_time):
    data = json.dumps({"gpus": gpus, "nodes": nodes, "total_time": total_time},
                      sort_keys=True, indent=4)
    return "JS1:" + base64.b64encode(gzip.compress(data.encode("ascii"))).decode("ascii")


def _make(mocker, jobid, start, end, tres, admincomment,
          ncpus="4", noop_prom=False):
    row = "|".join([jobid, start, end, "bouchet", tres, admincomment, "aturing",
                    "physics", "COMPLETED", "1", ncpus, "8G", "day", "common",
                    "1440", "myjob"]) + "\n"
    sacct_bytes = bytes(COLS + row, "utf-8")

    def side_effect(mylist, stderr=DEVNULL):
        if mylist == ["sacct", "-V"]:
            return bytes(f"slurm {SLURM_VERSION}\n", "utf-8")
        if mylist == ["sacct", "-P", "-X", "-o", FIELDS, "-j", jobid]:
            return sacct_bytes
        return b""

    mocker.patch("subprocess.check_output", side_effect=side_effect)
    if noop_prom:
        mocker.patch("jobstats.Jobstats.get_job_stats", lambda self: None)
    return Jobstats(jobid=jobid, prom_server="DUMMY")


def test_cpu_job(mocker):
    # 2h job, 4 cores, 50% cpu time used, 50% memory used
    nodes = {"n1": {"cpus": 4, "total_time": 14400,
                    "total_memory": 8589934592, "used_memory": 4294967296}}
    js = _make(mocker, "10920562", "1000000000", "1000007200",
               "billing=4,cpu=4,mem=8G,node=1", _js1(nodes, 0, 7200))
    rec = JobstatsKafkaHandler().build_record(js)

    assert rec["cluster"] == "bouchet"
    assert rec["jobid"] == 10920562 and isinstance(rec["jobid"], int)
    assert rec["@end"] == datetime.datetime.fromtimestamp(
        1000007200, tz=datetime.timezone.utc).replace(tzinfo=None).isoformat()
    assert rec["cpu_seconds_used"] == 14400.0
    assert rec["cpu_efficiency_pct"] == 50.0     # 14400 / (7200*4)
    assert rec["mem_efficiency_pct"] == 50.0
    assert rec["gpu_count"] == 0
    assert rec["gpu_util_mean_pct"] is None
    assert rec["status"] == "ok"
    assert rec["js1"].startswith("JS1:")


def test_gpu_job(mocker):
    nodes = {"g1": {"cpus": 8, "total_time": 28800,
                    "total_memory": 68719476736, "used_memory": 34359738368,
                    "gpu_utilization": {"0": 75.0, "1": 25.0},
                    "gpu_used_memory": {"0": 10737418240, "1": 5368709120},
                    "gpu_total_memory": {"0": 42949672960, "1": 42949672960}}}
    js = _make(mocker, "10920563", "1000000000", "1000003600",
               "billing=8,cpu=8,gres/gpu=2,mem=64G,node=1", _js1(nodes, 2, 3600),
               ncpus="8")
    rec = JobstatsKafkaHandler().build_record(js)

    assert rec["gpu_count"] == 2
    assert rec["gpu_util_mean_pct"] == 50.0       # (75 + 25) / 2
    assert rec["gpu_mem_used_bytes"] == 16106127360
    assert rec["gpu_mem_total_bytes"] == 85899345920
    assert rec["status"] == "ok"


def test_short_job(mocker):
    nodes = {"n1": {"cpus": 1, "total_time": 10,
                    "total_memory": 1073741824, "used_memory": 536870912}}
    js = _make(mocker, "10920564", "1000000000", "1000000030",
               "billing=1,cpu=1,mem=1G,node=1", _js1(nodes, 0, 30), ncpus="1")
    rec = JobstatsKafkaHandler().build_record(js)

    assert rec["status"] == "short"
    assert rec["js1"] == "JS1:Short"


class _EmptyStats:
    """Minimal Jobstats-like stub with no per-node data.

    A real Jobstats sys.exit()s in parse_stats() when there is no data, so the
    empty-sp_node branch of build_record can't be reached by constructing one;
    exercise it directly here (build_record is a pure function of a js object).
    """
    sp_node = {}
    diff = 7200
    end = 1000007200
    jobidraw = "10920565"
    cluster = "bouchet"
    gpus = 0
    cpu_util_total__used_alloc_cores = (0, 0, 0)
    cpu_mem_total__used_alloc_cores = (0, 0, 0)
    cpu_util_error_code = 0
    cpu_mem_error_code = 0

    def report_job_json(self, encode):
        return "None"


def test_nodata_record_is_defensive():
    rec = JobstatsKafkaHandler().build_record(_EmptyStats())
    assert rec["status"] == "nodata"
    assert rec["cpu_efficiency_pct"] is None
    assert rec["mem_efficiency_pct"] is None
    assert rec["js1"] == "JS1:None"


def test_send_dry_run_prints_and_sends_nothing(mocker, capsys):
    handler = JobstatsKafkaHandler()
    produced = mocker.patch.object(handler, "_get_producer")
    rec = {"cluster": "bouchet", "jobid": 1, "@end": "2026-07-23T19:09:03"}
    handler.send([rec], dry_run=True)
    out = capsys.readouterr().out
    assert '"jobid": 1' in out
    produced.assert_not_called()
