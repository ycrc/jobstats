import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    sacct = (
        "100|COMPLETED|1700000000\n"
        "101|RUNNING|Unknown\n"          # running -> excluded
        "102|PENDING|Unknown\n"          # pending -> excluded
        "103|TIMEOUT|1700000500\n"
        "100|FAILED|1699990000\n"        # requeue duplicate of 100 -> deduped
        "104|CANCELLED by 123|1700000600\n"
        "105|COMPLETED|Unknown\n"        # not actually ended -> excluded
        "\n"                             # blank line -> ignored
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 return_value=sacct.encode("utf-8"))

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now")
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_passes_duplicates_flag(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now")
    cmd = m.call_args.args[0]
    assert "--duplicates" in cmd
    assert cmd[:4] == ["sacct", "-X", "-n", "-P"]
    assert "-M" in cmd and "grace" in cmd
