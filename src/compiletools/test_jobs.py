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
