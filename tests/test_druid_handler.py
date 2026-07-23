import base64
import gzip
import json

import druid_handler
from druid_handler import DruidHandler


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _js1(nodes):
    data = json.dumps({"gpus": 0, "nodes": nodes, "total_time": 100})
    return "JS1:" + base64.b64encode(gzip.compress(data.encode("ascii"))).decode("ascii")


def test_disabled_returns_none(mocker):
    mocker.patch.dict(druid_handler.DRUID_CONFIG, {"enabled": False}, clear=False)
    assert DruidHandler().get_jobstats("bouchet", "123") is None


def test_returns_js1_from_first_row(mocker):
    mocker.patch.dict(druid_handler.DRUID_CONFIG,
                      {"enabled": True, "url": "http://druid/sql",
                       "datasource": "slurm_jobstats"}, clear=False)
    js1 = _js1({"n1": {"cpus": 1, "total_time": 50,
                       "total_memory": 1, "used_memory": 1}})
    post = mocker.patch("druid_handler.requests.post",
                        return_value=_FakeResp([{"js1": js1}]))

    result = DruidHandler().get_jobstats("bouchet", "19264556")
    assert result == js1
    # jobid must be sent as a numeric BIGINT parameter
    params = post.call_args.kwargs["json"]["parameters"]
    assert params[1] == {"type": "BIGINT", "value": 19264556}


def test_non_numeric_jobid_returns_none(mocker):
    mocker.patch.dict(druid_handler.DRUID_CONFIG, {"enabled": True}, clear=False)
    assert DruidHandler().get_jobstats("bouchet", "12_34") is None


def test_empty_result_returns_none(mocker):
    mocker.patch.dict(druid_handler.DRUID_CONFIG,
                      {"enabled": True, "url": "http://druid/sql",
                       "datasource": "slurm_jobstats"}, clear=False)
    mocker.patch("druid_handler.requests.post", return_value=_FakeResp([]))
    assert DruidHandler().get_jobstats("bouchet", "123") is None
