import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    # sacct is read like jobstats does (no -s, no --duplicates) and reports
    # ElapsedRaw + End as epochs (SLURM_TIME_FORMAT=%s). Keep only ids with a real
    # past end that ran >= min_elapsed, de-duped.
    sacct = (
        "100|3600|1700000000\n"
        "103|120|1700000500\n"
        "100|3600|1699990000\n"     # duplicate id -> deduped
        "104|7200|1700000600\n"
        "105|3600|Unknown\n"        # not finished (pending/running) -> excluded
        "106|3600|9999999999\n"     # end in the future -> excluded
        "107|10|1700000700\n"       # too short (< 60s) -> excluded
        "\n"                        # blank line -> ignored
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 return_value=sacct.encode("utf-8"))

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now", 60)
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_no_state_or_duplicates_flags(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now", 60)
    cmd = m.call_args.args[0]
    assert cmd[:4] == ["sacct", "-X", "-n", "-P"]
    # -s would match an id's older completed run while its current record is pending
    assert "-s" not in cmd
    assert "--duplicates" not in cmd
    assert "-M" in cmd and "grace" in cmd
    assert m.call_args.kwargs["env"]["SLURM_TIME_FORMAT"] == "%s"
