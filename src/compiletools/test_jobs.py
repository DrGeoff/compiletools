import compiletools.jobs as jobs


def test_cpu_count_handles_platform_errors(monkeypatch):
    """_cpu_count falls back to 4 when the dispatched function raises."""

    def failing_cpus():
        raise PermissionError("sched_getaffinity denied")

    monkeypatch.setitem(jobs._CPU_DISPATCH, "linux", failing_cpus)
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    assert jobs._cpu_count() == 4


def test_cpu_count_handles_missing_platform(monkeypatch):
    """_cpu_count falls back to os.cpu_count() (or 4) on unknown platforms."""
    monkeypatch.setattr(jobs.sys, "platform", "unknown_os")
    result = jobs._cpu_count()
    assert isinstance(result, int)
    assert result > 0


def test_cpus_linux_returns_positive_int():
    """_cpus_linux returns a positive integer via os.sched_getaffinity."""
    result = jobs._cpus_linux()
    assert isinstance(result, int)
    assert result > 0


def test_cpus_darwin(monkeypatch):
    """_cpus_darwin calls sysctl and returns the parsed integer."""
    import subprocess
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.stdout = "10\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert jobs._cpus_darwin() == 10


def test_cpu_count_success():
    """_cpu_count returns a positive integer on this platform."""
    result = jobs._cpu_count()
    assert isinstance(result, int)
    assert result > 0


def test_add_arguments():
    """add_arguments adds -j/--jobs to a parser."""
    import compiletools.apptools

    cap = compiletools.apptools.create_parser("test", argv=[], include_config=False)
    jobs.add_arguments(cap)
    args = cap.parse_args(["-j", "7"])
    assert args.parallel == 7


def test_main_default(capsys):
    """main([]) prints the CPU count."""
    import compiletools.testhelper as uth

    uth.delete_existing_parsers()
    ret = jobs.main([])
    assert ret == 0
    out = capsys.readouterr().out.strip()
    assert int(out) > 0


def test_main_with_jobs_flag(capsys):
    """main with -j flag prints that value."""
    import compiletools.testhelper as uth

    uth.delete_existing_parsers()
    ret = jobs.main(["-j", "42"])
    assert ret == 0
    assert capsys.readouterr().out.strip() == "42"


def test_main_verbose(capsys):
    """main with -vv prints args and the count."""
    import compiletools.testhelper as uth

    uth.delete_existing_parsers()
    ret = jobs.main(["-vv", "-j", "3"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "3" in out
