import compiletools.jobs as jobs


def test_cpu_count_uses_process_cpu_count_when_available(monkeypatch):
    """On Python 3.13+, _cpu_count uses os.process_cpu_count()."""
    monkeypatch.setattr(jobs.os, "process_cpu_count", lambda: 11, raising=False)
    assert jobs._cpu_count() == 11


def test_cpu_count_process_cpu_count_none_raises(monkeypatch):
    """When os.process_cpu_count() returns None, _cpu_count raises RuntimeError."""
    import pytest

    monkeypatch.setattr(jobs.os, "process_cpu_count", lambda: None, raising=False)
    with pytest.raises(RuntimeError, match="process_cpu_count"):
        jobs._cpu_count()


def test_cpu_count_uses_sched_getaffinity_pre_313(monkeypatch):
    """Pre-3.13 path: when process_cpu_count is missing, use sched_getaffinity."""
    monkeypatch.delattr(jobs.os, "process_cpu_count", raising=False)
    monkeypatch.setattr(jobs.os, "sched_getaffinity", lambda _: {0, 1, 2}, raising=False)
    assert jobs._cpu_count() == 3


def test_cpu_count_sched_getaffinity_oserror_falls_back(monkeypatch):
    """When sched_getaffinity raises OSError, fall back to os.cpu_count()."""
    monkeypatch.delattr(jobs.os, "process_cpu_count", raising=False)

    def raises(_):
        raise PermissionError("denied")

    monkeypatch.setattr(jobs.os, "sched_getaffinity", raises, raising=False)
    monkeypatch.setattr(jobs.os, "cpu_count", lambda: 7)
    assert jobs._cpu_count() == 7


def test_cpu_count_no_process_no_sched(monkeypatch):
    """When neither process_cpu_count nor sched_getaffinity exists, use os.cpu_count()."""
    monkeypatch.delattr(jobs.os, "process_cpu_count", raising=False)
    monkeypatch.delattr(jobs.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(jobs.os, "cpu_count", lambda: 4)
    assert jobs._cpu_count() == 4


def test_cpu_count_all_apis_none_raises(monkeypatch):
    """When every API returns None and process/sched paths are unavailable, raise."""
    import pytest

    monkeypatch.delattr(jobs.os, "process_cpu_count", raising=False)
    monkeypatch.delattr(jobs.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(jobs.os, "cpu_count", lambda: None)
    with pytest.raises(RuntimeError, match="cpu_count"):
        jobs._cpu_count()


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
