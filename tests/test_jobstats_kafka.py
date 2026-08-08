import jobstats_kafka


def test_enumerate_jobids_filters_and_dedups(mocker):
    # One windowed sacct read (no -s, no --duplicates). End and ElapsedRaw come
    # back as epochs (SLURM_TIME_FORMAT=%s). Keep only ids with a real past end
    # that ran >= min_elapsed, de-duped and order-preserving.
    sacct = (
        "100|3600|1700000000\n"
        "103|120|1700000500\n"
        "100|3600|1699990000\n"     # duplicate id -> deduped
        "104|7200|1700000600\n"
        "105|3600|Unknown\n"        # still running -> excluded
        "106|3600|9999999999\n"     # end in the future -> excluded
        "107|10|1700000700\n"       # too short (< 60s) -> excluded
        "\n"                        # blank line -> ignored
    )
    mocker.patch("jobstats_kafka.subprocess.check_output",
                 return_value=sacct.encode("utf-8"))

    result = jobstats_kafka.enumerate_jobids("bouchet", "now-1hours", "now", 60)
    assert result == ["100", "103", "104"]


def test_enumerate_jobids_single_windowed_query(mocker):
    m = mocker.patch("jobstats_kafka.subprocess.check_output", return_value=b"")
    jobstats_kafka.enumerate_jobids("grace", "now-2hours", "now", 60)
    # exactly one sacct call -- no expensive unwindowed `-j` follow-up
    assert m.call_count == 1
    cmd = m.call_args.args[0]
    assert cmd[:4] == ["sacct", "-X", "-n", "-P"]
    assert "-S" in cmd and "-E" in cmd and "-a" in cmd
    assert "-j" not in cmd          # windowed, not a per-id lookup
    assert "-s" not in cmd          # would resolve a different record than jobstats
    assert "--duplicates" not in cmd
    assert "-M" in cmd and "grace" in cmd
    assert m.call_args.kwargs["env"]["SLURM_TIME_FORMAT"] == "%s"


def test_is_expected_skip():
    # expected, quiet-skip cases
    assert jobstats_kafka._is_expected_skip(
        "No data was found for job 123. This is probably because it is too old")
    assert jobstats_kafka._is_expected_skip(
        "Run time is very short so only providing seff output")
    assert jobstats_kafka._is_expected_skip(
        "Failed to get details for job 19349322 since it is a PENDING job.")
    # real failures -> surfaced, not skipped
    assert not jobstats_kafka._is_expected_skip(
        "ERROR: Failed to get run query up[...] with time ..., error: timeout")
    assert not jobstats_kafka._is_expected_skip("")
