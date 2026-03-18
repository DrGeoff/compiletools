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


def test_cpu_count_success():
    """_cpu_count returns a positive integer on this platform."""
    result = jobs._cpu_count()
    assert isinstance(result, int)
    assert result > 0


def test_main_prints_count(capsys):
    """main([]) prints the CPU count."""
    rc = jobs.main([])
    assert rc == 0
    output = capsys.readouterr().out.strip()
    assert int(output) > 0
