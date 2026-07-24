import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    # sacct is already state-filtered via -s, so it returns only terminal jobs;
    # the function still de-dups and drops any without a real end time.
    sacct = (
        "100|1700000000\n"
        "103|1700000500\n"
        "100|1699990000\n"     # requeue duplicate of 100 -> deduped
        "104|1700000600\n"
        "105|Unknown\n"        # no real end -> excluded by the safety net
        "\n"                   # blank line -> ignored
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 return_value=sacct.encode("utf-8"))

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now")
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_state_and_duplicates_flags(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now")
    cmd = m.call_args.args[0]
    assert cmd[:4] == ["sacct", "-X", "-n", "-P"]
    assert "--duplicates" in cmd
    # state filter excludes pending/running at the sacct level
    assert "-s" in cmd
    assert cmd[cmd.index("-s") + 1] == jobstats_kafka.TERMINAL_STATES
    assert "-M" in cmd and "grace" in cmd
