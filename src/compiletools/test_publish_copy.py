"""``publish_copy`` is an import leaf for measured performance reasons.

The behavioural tests for :func:`secure_publish_copy` live in
``test_filesystem_utils.py::TestSecurePublishCopy`` and reach it through the
re-export, so they cover this module too. What lives here is the property
those tests cannot see: that importing this module stays cheap.
"""

import subprocess
import sys

import compiletools.filesystem_utils
import compiletools.publish_copy

# On PyPy, several hashlib/ssl/lzma C-accelerator backends are implemented as
# bundled cffi shim modules rather than C extensions, so their names are
# absent from sys.stdlib_module_names (unlike CPython's _hashlib/_sha3/etc,
# which ARE listed there). `secrets` -> `hmac` -> `hashlib` pulls them in
# transitively; they're genuinely part of PyPy's stdlib, not a dependency
# this module adds.
_PYPY_STDLIB_BACKEND_ROOTS = frozenset(
    {
        "_blake2",
        "_sha3",
        "_hashlib",
        "_lzma_cffi",
        "_cffi_ssl",
        "_pypy_openssl",
        "_pypy_util_cffi_inner",
    }
)


def _is_stdlib_root(root: str) -> bool:
    if root in sys.stdlib_module_names:
        return True
    return sys.implementation.name == "pypy" and root in _PYPY_STDLIB_BACKEND_ROOTS


def _modules_added_by(import_stmt: str) -> set[str]:
    """Return the sys.modules delta a fresh interpreter shows for *import_stmt*
    over a bare ``import compiletools``.

    Measuring the DELTA against the package baseline (rather than the absolute
    module set) isolates what ``publish_copy`` itself pulls in, so this test
    does not fail merely because ``compiletools/__init__`` changed.
    """
    prog = (
        "import sys\n"
        "import compiletools\n"
        "base = set(sys.modules)\n"
        f"{import_stmt}\n"
        "print(chr(10).join(sorted(set(sys.modules) - base)))\n"
    )
    out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, check=True)
    return {line for line in out.stdout.splitlines() if line}


def test_module_imports_no_heavy_dependencies():
    """Importing ``publish_copy`` must add only stdlib modules.

    This module is spawned as a subprocess once per test on a cold or
    fresh-workspace/warm-CAS build (deposit and materialise respectively), so
    its import graph is on the critical path of warm CI builds. Measured
    when the same copy lived in ``filesystem_utils``: +18.4 ms per spawn
    versus the inline snippet it replaced -- ~740 ms of CPU on a 40-test
    build, ~5% wall clock -- of which only 6.7 ms was the security
    primitives (``secrets``, ``contextlib``). The remainder was
    ``stringzilla``, ``apptools_canonicalize`` and ``subprocess``, none of
    which a byte copy needs.

    So an import added here is a measurable regression on every warm CI
    build. The ``publish-copy-is-a-leaf`` contract in ``.importlinter``
    covers first-party edges; this covers third-party ones, which a
    forbidden-list cannot enumerate.
    """
    added = _modules_added_by("import compiletools.publish_copy")

    assert "compiletools.publish_copy" in added, (
        "the probe did not actually import the module under test -- if this "
        "fires, the interpreter subprocess is measuring nothing and the "
        "emptiness check below would pass vacuously"
    )

    non_stdlib = {
        name for name in added if not _is_stdlib_root(name.partition(".")[0]) and name != "compiletools.publish_copy"
    }
    assert non_stdlib == set(), (
        f"publish_copy grew non-stdlib imports {sorted(non_stdlib)}; each one is paid twice "
        f"per test on every warm cache-hit build. Import it lazily inside the function that "
        f"needs it, or keep it out."
    )


def test_control_filesystem_utils_is_measurably_heavier():
    """Control for the test above: the probe must be able to SEE a heavy
    import. ``filesystem_utils`` is the module this code was moved out of, so
    importing it still pulls the third-party/first-party set that motivated
    the split. If this ever comes back empty the delta probe has broken and
    the leaf assertion above is worthless.
    """
    added = _modules_added_by("import compiletools.filesystem_utils")

    non_stdlib = {name for name in added if not _is_stdlib_root(name.partition(".")[0])}
    assert non_stdlib, (
        "importing filesystem_utils added no non-stdlib modules -- the probe cannot "
        "distinguish a leaf from a non-leaf, so test_module_imports_no_heavy_dependencies "
        "proves nothing"
    )


def test_filesystem_utils_reexports_are_the_same_objects():
    """In-process callers (``trace_backend``) reach the copy through
    ``filesystem_utils``. The re-export must be the identical object, not a
    second definition -- two implementations of the security properties is
    exactly what the move was meant to avoid.
    """
    assert compiletools.filesystem_utils.secure_publish_copy is compiletools.publish_copy.secure_publish_copy
    assert compiletools.filesystem_utils.QUIET_MISSING_SRC_FLAG == compiletools.publish_copy.QUIET_MISSING_SRC_FLAG
    assert compiletools.filesystem_utils._PUBLISH_TMP_SUFFIX == compiletools.publish_copy.PUBLISH_TMP_SUFFIX


def test_copy_command_runs_the_module_by_path_not_dash_m():
    """``-m`` would pay ``compiletools/__init__`` plus ``runpy`` on every
    spawn; by-path pays neither. Measured 41.4 ms (-m) vs 35.7 ms (by path)
    against a 29.6 ms inline-snippet baseline, so this is roughly half the
    remaining overhead on a per-test hot path -- not a style preference.
    """
    argv = compiletools.publish_copy.copy_command("/usr/bin/python3", "a.xml", "b.xml")

    assert "-m" not in argv, f"copy_command regressed to a -m invocation: {argv}"
    assert argv[0] == "/usr/bin/python3"
    assert argv[1] == compiletools.publish_copy.__file__
    assert argv[-2:] == ["a.xml", "b.xml"]
    assert compiletools.publish_copy.QUIET_MISSING_SRC_FLAG not in argv


def test_copy_command_places_the_quiet_flag_before_the_positionals():
    """``main`` only strips the flag when it is argv[0]; an argv that put it
    after src/dst would be parsed as a bad argument count and silently
    return 2, skipping the copy."""
    argv = compiletools.publish_copy.copy_command("py", "a.xml", "b.xml", quiet_missing_src=True)

    assert argv[-3:] == [compiletools.publish_copy.QUIET_MISSING_SRC_FLAG, "a.xml", "b.xml"]
    # And the form main() actually receives round-trips to a successful parse.
    assert compiletools.publish_copy.main(argv[2:]) == 0


def test_cli_rejects_a_bad_argument_count():
    assert compiletools.publish_copy.main([]) == 2
    assert compiletools.publish_copy.main(["only-one"]) == 2
    assert compiletools.publish_copy.main([compiletools.publish_copy.QUIET_MISSING_SRC_FLAG, "only-one"]) == 2


def test_cli_copies_and_honours_the_quiet_flag(tmp_path, capsys):
    src = tmp_path / "report.xml"
    src.write_text("<testsuite/>")

    assert compiletools.publish_copy.main([str(src), str(tmp_path / "loud.xml")]) == 0
    assert (tmp_path / "loud.xml").read_text() == "<testsuite/>"

    missing = tmp_path / "never-written.xml"
    assert compiletools.publish_copy.main([str(missing), str(tmp_path / "a.xml")]) == 0
    assert "Warning" in capsys.readouterr().err, "a missing src must warn by default"

    assert (
        compiletools.publish_copy.main(
            [compiletools.publish_copy.QUIET_MISSING_SRC_FLAG, str(missing), str(tmp_path / "b.xml")]
        )
        == 0
    )
    assert capsys.readouterr().err == "", (
        f"{compiletools.publish_copy.QUIET_MISSING_SRC_FLAG} must suppress the warning"
    )
    assert not (tmp_path / "b.xml").exists()
