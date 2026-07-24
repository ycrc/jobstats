import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    # Phase 1 (windowed) yields candidate JobIDRaws; phase 2 (unwindowed `-j`)
    # resolves each candidate's *current* record the way jobstats does. End and
    # ElapsedRaw come back as epochs (SLURM_TIME_FORMAT=%s). Keep only ids with a
    # real past end that ran >= min_elapsed, de-duped and order-preserving.
    phase1 = "100\n103\n100\n104\n105\n106\n107\n\n"  # dup 100, blank line
    phase2 = (
        "100|3600|1700000000\n"
        "103|120|1700000500\n"
        "104|7200|1700000600\n"
        "105|3600|Unknown\n"        # requeued-again/pending now -> excluded
        "106|3600|9999999999\n"     # end in the future -> excluded
        "107|10|1700000700\n"       # too short (< 60s) -> excluded
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 side_effect=[phase1.encode("utf-8"), phase2.encode("utf-8")])

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now", 60)
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_empty_window_skips_phase2(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    assert jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now", 60) == []
    # no candidates -> only the windowed phase-1 query runs
    assert m.call_count == 1


def test_enumerate_jobids_no_state_or_duplicates_flags(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output",
                     side_effect=[b"100\n", b"100|3600|1700000000\n"])
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now", 60)

    phase1, phase2 = m.call_args_list[0].args[0], m.call_args_list[1].args[0]
    # phase 1 is windowed and enumerates all users; phase 2 is unwindowed `-j`.
    assert phase1[:4] == ["sacct", "-X", "-n", "-P"]
    assert "-S" in phase1 and "-E" in phase1 and "-a" in phase1
    assert "-j" not in phase1
    assert "-j" in phase2 and "-S" not in phase2 and "-E" not in phase2
    # -s / --duplicates would resolve a different record than jobstats does.
    for cmd in (phase1, phase2):
        assert "-s" not in cmd
        assert "--duplicates" not in cmd
        assert "-M" in cmd and "grace" in cmd
    for call in m.call_args_list:
        assert call.kwargs["env"]["SLURM_TIME_FORMAT"] == "%s"
