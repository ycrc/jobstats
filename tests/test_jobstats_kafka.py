import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    # sacct is state-filtered via -s and reports End as an epoch (SLURM_TIME_FORMAT=%s);
    # keep only ids with a real past end time, de-duped.
    sacct = (
        "100|1700000000\n"
        "103|1700000500\n"
        "100|1699990000\n"      # duplicate id -> deduped
        "104|1700000600\n"
        "105|Unknown\n"         # no epoch end -> excluded
        "106|9999999999\n"      # end in the future -> excluded (defensive)
        "\n"                    # blank line -> ignored
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 return_value=sacct.encode("utf-8"))

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now")
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_state_filter_no_duplicates(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now")
    cmd = m.call_args.args[0]
    assert cmd[:4] == ["sacct", "-X", "-n", "-P"]
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == jobstats_kafka.TERMINAL_STATES
    assert "--duplicates" not in cmd     # would surface requeued-then-pending ids
    assert "-M" in cmd and "grace" in cmd
    # sacct End is requested as an epoch for the range check
    assert m.call_args.kwargs["env"]["SLURM_TIME_FORMAT"] == "%s"
