"""Integration test: back-to-back build with no source changes must not
compile or link.

Verifies that the shake backend's content-addressable cache and trace
system correctly short-circuit both compile and link steps on a repeat
build.  This catches regressions like non-deterministic link command
ordering (which defeats the CA cache and forces a re-link every time).

The two builds run in separate subprocesses with different PYTHONHASHSEED
values so that any ordering that depends on set/dict hash randomization
will differ between runs — exactly the scenario that caused the original
bug.
"""

import json
import os
import subprocess
import sys
import textwrap

import compiletools.testhelper as uth

# Source files for a multi-file build.  Multiple translation units are
# needed so that set iteration order can vary between PYTHONHASHSEED
# values.  With a single .cpp file there is nothing to reorder.
_SOURCES = {
    "main.cpp": textwrap.dedent("""\
        #include "alpha.H"
        #include "bravo.H"
        #include "charlie.H"
        int main() { return alpha() + bravo() + charlie(); }
    """),
    "alpha.H": '#pragma once\nint alpha();\n',
    "alpha.cpp": '#include "alpha.H"\nint alpha() { return 0; }\n',
    "bravo.H": '#pragma once\nint bravo();\n',
    "bravo.cpp": '#include "bravo.H"\nint bravo() { return 0; }\n',
    "charlie.H": '#pragma once\nint charlie();\n',
    "charlie.cpp": '#include "charlie.H"\nint charlie() { return 0; }\n',
}

# Self-contained build script executed in a subprocess.
# Writes each source file, builds with the shake backend, counts
# subprocess.run calls made by trace_backend, writes JSON report.
_BUILD_SCRIPT = textwrap.dedent("""\
    import json
    import os
    import subprocess
    import sys
    from unittest import mock

    import compiletools.apptools
    import compiletools.headerdeps
    import compiletools.hunter
    import compiletools.magicflags
    import compiletools.namer
    import compiletools.testhelper as uth
    import compiletools.trace_backend  # ensure registered
    from compiletools.build_backend import get_backend_class
    from compiletools.test_backend_integration import _add_backend_arguments

    tmp_path = sys.argv[1]
    report_path = sys.argv[2]

    source_path = os.path.realpath(os.path.join(tmp_path, "main.cpp"))
    objdir = os.path.join(tmp_path, "obj")
    bindir = os.path.join(tmp_path, "bin")
    argv = [
        "--include", tmp_path,
        "--objdir", objdir,
        "--bindir", bindir,
        source_path,
    ]

    uth.reset()
    cap = compiletools.apptools.create_parser("noop rebuild test", argv=argv)
    _add_backend_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)

    headerdeps = compiletools.headerdeps.create(args)
    magicparser = compiletools.magicflags.create(args, headerdeps)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

    BackendClass = get_backend_class("shake")
    backend = BackendClass(args=args, hunter=hunter)
    graph = backend.build_graph()

    os.makedirs(bindir, exist_ok=True)
    backend.generate(graph)

    # Count all subprocess.run calls made by trace_backend during execute().
    # This covers both compile (_atomic_compile_no_lock) and link (_run_local)
    # invocations.  On a no-op rebuild, the count should be zero.
    call_count = 0
    orig_run = subprocess.run

    def counting_run(*a, **kw):
        global call_count
        call_count += 1
        return orig_run(*a, **kw)

    with mock.patch("compiletools.trace_backend.subprocess.run", side_effect=counting_run):
        backend.execute("build")

    with open(report_path, "w") as f:
        json.dump({"subprocess_calls": call_count}, f)
""")


def _run_build(tmp_path, report_name, seed):
    """Run the build script in a subprocess with the given PYTHONHASHSEED."""
    report_path = os.path.join(str(tmp_path), report_name)
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = str(seed)
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_SCRIPT, str(tmp_path), report_path],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Build subprocess failed (seed={seed}, rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    with open(report_path) as f:
        return json.load(f)


class TestNoopRebuild:
    """A back-to-back build with no source changes must not compile or link."""

    @uth.requires_functional_compiler
    def test_repeat_build_skips_compile_and_link(self, tmp_path):
        # Write multi-file source tree
        for name, content in _SOURCES.items():
            (tmp_path / name).write_text(content)

        # Build 1 (seed=42): real compilation — produces .o, executable, traces
        report1 = _run_build(tmp_path, "report1.json", seed=42)
        assert report1["subprocess_calls"] >= 1, "First build should invoke the compiler"

        exe_path = os.path.join(str(tmp_path), "bin", "main")
        assert os.path.exists(exe_path), "First build did not produce executable"

        # Build 2 (seed=999): different hash seed, same source files on disk.
        # Nothing should compile or link.
        report2 = _run_build(tmp_path, "report2.json", seed=999)
        assert report2["subprocess_calls"] == 0, (
            f"Expected zero compiler/linker calls on repeat build, "
            f"got {report2['subprocess_calls']}"
        )
