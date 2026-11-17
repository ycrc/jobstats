import os
from jobstats import Jobstats
from output_formatters import ClassicOutput
import pytest


DEVNULL = open(os.devnull, 'w')
SLURM_VERSION = "25.11.5"

fields = ["jobid",
          "jobidraw",
          "start",
          "end",
          "cluster",
          "alloctres",
          "admincomment",
          "user",
          "account",
          "state",
          "nnodes",
          "ncpus",
          "reqmem",
          "qos",
          "partition",
          "timelimitraw",
          "jobname"]
fields = ",".join(fields)

@pytest.fixture
def simple_stats(mocker):
    def side_effect(mylist, stderr=DEVNULL):
        cols = ('JobIDRaw|Start|End|Cluster|AllocTRES|AdminComment|User|Account|'
                'State|NNodes|NCPUS|ReqMem|QOS|Partition|TimelimitRaw|JobName\n')
        ss64 = ('JS1:H4sIAPdcJmcC/1WNQQqDMBBF7zLrtEzG0ZhcphQzqGBM0bgQyd0bUii4fe8'
                '//gVr9LKDuyDNo2yPibpBr00FMb2XV5AQtxOcRtMY1j0xKjh28X/TdsRMhRfxa9'
                'IcBJyxbPsnKRg+R3lgzPk+ICSrYKwW8xcnjeJ8iwAAAA==')
        data = ('10920562|1730212549|1730214578|tiger2|billing=40,cpu=40,mem=10G,no'
                'de=1|%s|aturing|physics|COMPLETED|1|40|10G|tiger-short|serial|144'
                '0|9\n' % ss64)
        sacct_bytes = bytes(cols + data, "utf-8")
        if mylist == ["sacct", "-V"]:
            return bytes(f"slurm {SLURM_VERSION}\n", "utf-8")
        elif mylist == ["sacct", "-P", "-X", "-o", fields, "-j", "10920562"]:
            return sacct_bytes
    mocker.patch("subprocess.check_output", side_effect=side_effect)
    stats = Jobstats(jobid="10920562", prom_server="DUMMY-SERVER")
    return stats


def test_human_bytes(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.human_bytes(0) == "0.0B"
    assert formatter.human_bytes(42) == "42.0B"
    assert formatter.human_bytes(1024**3) == "1.0GB"
    assert formatter.human_bytes(4_000_000_000) == "3.7GB"
    assert formatter.human_bytes(4_000_000_000_000) == "3.6TB"
    assert formatter.human_bytes(4_000_000_000_000_000) == "3.6PB"


def test_human_seconds(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.human_seconds(0) == "00:00:00"
    assert formatter.human_seconds(42) == "00:00:42"
    assert formatter.human_seconds(420) == "00:07:00"
    assert formatter.human_seconds(4200) == "01:10:00"
    assert formatter.human_seconds(42003) == "11:40:03"
    assert formatter.human_seconds(420002) == "4-20:40:02"
    assert formatter.human_seconds(4200001) == "48-14:40:01"


def test_cpu_memory_formatted(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.cpu_memory_formatted(with_label=False) == "10GB"
    expected = "     CPU Memory: 10GB (250MB per CPU-core)"
    assert formatter.cpu_memory_formatted() == expected
    # no prefix (e.g., 10G)
    formatter.js.reqmem = "10"
    assert formatter.cpu_memory_formatted(with_label=False) == "10"
    formatter.js.reqmem = "10000G"
    assert formatter.cpu_memory_formatted(with_label=False) == "10TB"
    formatter.js.reqmem = "100.50G"
    formatter.js.ncpus = 9
    expected = "     CPU Memory: 100.5GB (11.2GB per CPU-core)"
    assert formatter.cpu_memory_formatted() == expected


def test_rounded_memory_with_safety(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.rounded_memory_with_safety(0) == 1
    assert formatter.rounded_memory_with_safety(9) == 11
    assert formatter.rounded_memory_with_safety(26) == 35
    assert formatter.rounded_memory_with_safety(90) == 110
    assert formatter.rounded_memory_with_safety(900) == 1100
    assert formatter.rounded_memory_with_safety(2600) == 3200


def test_time_limit_formatted(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.time_limit_formatted() == "     Time Limit: 1-00:00:00"
    formatter.js.timelimitraw = 4242
    assert formatter.time_limit_formatted() == "     Time Limit: 2-22:42:00"


def test_draw_meter(simple_stats):
    formatter = ClassicOutput(simple_stats)
    expected = "[                                                0%]"
    assert formatter.draw_meter(0, "cpu") == expected
    expected = "[|||||||||||||||||||||||||||||||||||||          75%]"
    assert formatter.draw_meter(75, "cpu") == expected
    expected = "[||||||||||||||||||||||||||||||||||||||||||||||100%]"
    assert formatter.draw_meter(100, "cpu") == expected


def test_format_note(simple_stats):
    formatter = ClassicOutput(simple_stats)
    note = "A simple note."
    expected = f"  * {note}\n\n"
    assert formatter.format_note(note) == expected
    note = "A simple note:"
    url = "https://mysite.ext"
    expected = f"  * {note}\n      {url}\n\n"
    assert formatter.format_note(note, url) == expected


def test_output_metadata(simple_stats):
    formatter = ClassicOutput(simple_stats)
    expected = """
           Job ID: 10920562
     User/Account: aturing/physics
         Job Name: 9
            State: COMPLETED
            Nodes: 1
        CPU Cores: 40
       CPU Memory: 10GB (250MB per CPU-core)
    QOS/Partition: tiger-short/serial
          Cluster: tiger2
       Start Time: Tue Oct 29, 2024 at 10:35 AM
         Run Time: 00:33:49
       Time Limit: 1-00:00:00
    """
    actual = formatter.output_metadata()
    for a, e in zip([""] + actual.split("\n"), expected.split("\n")):
        # avoid time zone complications
        if "Start Time" not in a:
            assert a.strip() == e.strip()


def test_output_overall_cpu_util(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.js.cpu_util_error_code == 0
    actual = formatter.output_overall_cpu_util()
    expected = "  CPU utilization  "
    expected += "[|||||||||||||||||||||||||||||||||||||||||||||||98%]\n"
    assert actual == expected


def test_output_overall_cpu_memory_usage(simple_stats):
    formatter = ClassicOutput(simple_stats)
    assert formatter.js.cpu_mem_error_code == 0
    actual = formatter.output_overall_cpu_memory_usage()
    expected = "  CPU memory usage "
    expected += "[||||||||||||||||||||||||||                     52%]\n"
    assert actual == expected


@pytest.fixture
def partial_detailed_gpu_stats(mocker):
    """A two-node GPU job where only one node reported a detailed GPU metric.

    This happens when the GPU exporter on one node is down or hung while the
    other node's exporter works (e.g. a1116u09n01 on Bouchet in 2026-11).
    """
    import base64
    import gzip
    import json

    def gpu_node(with_detailed):
        node = {"cpus": 4,
                "total_memory": 42949672960,
                "used_memory": 10737418240,
                "total_time": 10000.0,
                "gpu_total_memory": {"0": 161061273600},
                "gpu_used_memory": {"0": 64424509440},
                "gpu_utilization": {"0": 80.0}}
        if with_detailed:
            # the exporter stores fractions; jobstats multiplies by 100
            node["fp64_util_percent_max"] = {"0": 0.42}
        return node

    js_data = {"gpus": 2,
               "nodes": {"a1116u05n01": gpu_node(True),
                         "a1116u09n01": gpu_node(False)},
               "total_time": 3600}
    blob = json.dumps(js_data, sort_keys=True)
    ss64 = "JS1:" + base64.b64encode(gzip.compress(blob.encode("ascii"))).decode("ascii")

    def side_effect(mylist, stderr=DEVNULL):
        cols = ('JobIDRaw|Start|End|Cluster|AllocTRES|AdminComment|User|Account|'
                'State|NNodes|NCPUS|ReqMem|QOS|Partition|TimelimitRaw|JobName\n')
        data = ('900003|1763300000|1763303600|bouchet|billing=1,cpu=8,gres/gpu=2,'
                'mem=80G,node=2|%s|someuser|someacct|COMPLETED|2|8|80G|normal|'
                'gpu_b200|120|test\n' % ss64)
        if mylist == ["sacct", "-V"]:
            return bytes(f"slurm {SLURM_VERSION}\n", "utf-8")
        return bytes(cols + data, "utf-8")

    mocker.patch("subprocess.check_output", side_effect=side_effect)
    mocker.patch.dict("config.DRUID_CONFIG", {"enabled": False})
    mocker.patch("config.GPU_METRICS_EXPORTER", "NVML")
    mocker.patch("config.GPU_METRICS",
                 {"FP64 Max": {"metric": "fp64_util_percent",
                               "operation": "max_over_time",
                               "show_overall": True,
                               "show_per_gpu": True,
                               "write_to_db": True,
                               "long_name": "FP64 (max) utilization"}})
    return Jobstats(jobid="900003", prom_server="DUMMY-SERVER")


def test_partial_detailed_gpu_metric(partial_detailed_gpu_stats):
    """A metric missing on one node reports 'unknown' instead of crashing."""
    gm = partial_detailed_gpu_stats.detailed_gpu_metrics[0]
    assert gm.error_code == 1
    assert gm.total__value_gpus[1] == 1  # one node did report a value
    report = ClassicOutput(partial_detailed_gpu_stats).output(no_color=True)
    assert "FP64 Max utilization  (Value is unknown)" in report
