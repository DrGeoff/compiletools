import os
import platform

import compiletools.jobs as jobs


def test_cpu_count_handles_platform_errors(monkeypatch):
    """_cpu_count falls back to 4 when platform function raises."""

    def failing_cpus():
        raise PermissionError("cpu_affinity denied")

    monkeypatch.setattr(jobs, "_cpus_linux", failing_cpus)
    monkeypatch.setattr(jobs, "_determine_system", lambda: "linux")
    assert jobs._cpu_count() == 4


def test_cpu_count_handles_missing_platform(monkeypatch):
    """_cpu_count falls back to 4 for unknown platforms."""
    monkeypatch.setattr(jobs, "_determine_system", lambda: "unknown_os")
    assert jobs._cpu_count() == 4


def test_determine_system_linux(monkeypatch):
    """On Linux, _determine_system returns 'linux'."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = jobs._determine_system()
    # Could be "linux" or "termux" depending on /proc/stat permissions
    assert result in ("linux", "termux")


def test_determine_system_termux(monkeypatch):
    """On Linux with PermissionError on /proc/stat, returns 'termux'."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "stat", _raise_permission_error)
    assert jobs._determine_system() == "termux"


def _raise_permission_error(path):
    raise PermissionError("no access")


def test_determine_system_darwin(monkeypatch):
    """On Darwin, _determine_system returns 'darwin'."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert jobs._determine_system() == "darwin"


def test_cpus_termux(monkeypatch):
    """_cpus_termux calls nproc and returns its output."""
    import subprocess
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.stdout = "8\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert jobs._cpus_termux() == "8"


def test_cpus_darwin(monkeypatch):
    """_cpus_darwin calls sysctl and returns its output."""
    import subprocess
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.stdout = "10\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
    assert jobs._cpus_darwin() == "10"


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
