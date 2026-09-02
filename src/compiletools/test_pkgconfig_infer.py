"""Focused tests for :mod:`pkgconfig_infer` (auto-pkg-config Stage 1).

Everything runs against the shared ``examples-features/pkgs/`` fixtures via
the ``pkgconfig_env`` conftest fixture (PKG_CONFIG_PATH) and opts into the
feature with ``auto_pkg_config_on``. The module's memos are keyed on
``PKG_CONFIG_PATH`` and survive across tests in the same xdist worker, so an
autouse fixture clears them before and after every test here.

Conventions honoured here: no compiler names, no ``os.chdir`` (env is changed
through ``monkeypatch.setenv``), and only the documented subprocess
boundaries (``_run_pkg_config_index``, ``_run_pkg_config_exists``,
``_run_distro_owner_query``, ``_run_distro_list_query``,
``_run_man_lookup``) are ever wrapped/stubbed -- every other function runs
for real.
"""

import os
import shutil

import pytest

import compiletools.apptools as apptools
import compiletools.apptools_pkgconfig as apptools_pkgconfig
import compiletools.pkgconfig_infer as pkgconfig_infer
from compiletools.pkgconfig_infer import (
    REASON_AMBIGUOUS,
    REASON_COMPILER,
    REASON_COVERED,
    REASON_IGNORED,
    REASON_NO_OWNER,
    REASON_NO_PC,
    REASON_NOT_FOUND,
    REASON_STD,
    InferenceConfig,
)


@pytest.fixture(autouse=True)
def _clear_pkgconfig_infer_cache():
    """Keep the module memos from leaking between tests (and into later tests
    in the same worker): the pc-index and exists memos key on
    ``PKG_CONFIG_PATH``, but test isolation must not rely on every test
    changing the env."""
    pkgconfig_infer.clear_cache()
    yield
    pkgconfig_infer.clear_cache()


def _search_dirs(pkgs_dir: str) -> tuple[str, ...]:
    """Header search dirs: every subdirectory of the shared pkgs/ fixture dir
    (the ``-I`` dirs its ``.pc`` files export)."""
    return tuple(
        sorted(
            os.path.join(pkgs_dir, name) for name in os.listdir(pkgs_dir) if os.path.isdir(os.path.join(pkgs_dir, name))
        )
    )


def _config(pkgs_dir: str, **overrides) -> InferenceConfig:
    return InferenceConfig(search_dirs=_search_dirs(pkgs_dir), **overrides)


# ---------------------------------------------------------------------------
# Resolver chain through infer()
# ---------------------------------------------------------------------------


def test_happy_path_unique_dir(pkgconfig_env, auto_pkg_config_on):
    results = pkgconfig_infer.infer(["autoinfer/api.h"], _config(pkgconfig_env), memo={})
    assert len(results) == 1
    result = results[0]
    assert result.package == "autoinfer"
    assert result.resolver == "pc-index"
    assert result.reason == ""
    assert result.candidates == ("autoinfer",)
    assert result.header_path is not None and result.header_path.endswith("autoinfer/api.h")


def test_ambiguity_never_guesses(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """With only the two sibling family packages installed (the tie-break
    Name/stem fixture is hidden), the shared include dir is genuinely
    ambiguous: unresolved + diagnostic, never a guess."""
    pcdir = tmp_path / "pkgs"
    pcdir.mkdir()
    for name in ("autoinfer-fam-a.pc", "autoinfer-fam-b.pc"):
        shutil.copy(os.path.join(pkgconfig_env, name), pcdir / name)
    # The .pc files use -I${pcfiledir}/autoinfer-fam-include, so copying the
    # include tree next to them keeps their Cflags valid in the temp dir.
    shutil.copytree(os.path.join(pkgconfig_env, "autoinfer-fam-include"), pcdir / "autoinfer-fam-include")
    monkeypatch.setenv("PKG_CONFIG_PATH", str(pcdir))
    config = InferenceConfig(search_dirs=(str(pcdir / "autoinfer-fam-include"),))

    result = pkgconfig_infer.infer(["fam/x.h"], config, memo={})[0]
    assert result.package is None
    assert result.resolver is None
    assert result.candidates == ("autoinfer-fam-a", "autoinfer-fam-b")
    assert result.reason.startswith(REASON_AMBIGUOUS)


def test_tie_break_rule_1_name_matches_first_component(pkgconfig_env, auto_pkg_config_on):
    """With the full pkgs dir, ``autoinfer-fam.pc`` (``Name: fam``) joins the
    family and rule 1 (Name == header's first path component) picks it."""
    result = pkgconfig_infer.infer(["fam/x.h"], _config(pkgconfig_env), memo={})[0]
    assert result.package == "autoinfer-fam"
    assert result.resolver == "pc-index"
    assert result.candidates == ("autoinfer-fam", "autoinfer-fam-a", "autoinfer-fam-b")


def test_umbrella_wins_over_required_parts(pkgconfig_env, auto_pkg_config_on):
    """``umbrella/u.h`` must attribute to the umbrella package, not one of its
    parts. On these fixtures the first ``-l`` stem (``-lumbrella``) matches
    the header's first component, so rule 2 decides; the pure rule-3 case
    (drop candidates named in a sibling's Requires) is asserted directly on
    :func:`pkgconfig_infer.tie_break` below, where the component matches no
    name or stem."""
    result = pkgconfig_infer.infer(["umbrella/u.h"], _config(pkgconfig_env), memo={})[0]
    assert result.package == "autoinfer-umbrella"
    assert result.resolver == "pc-index"
    assert result.candidates == ("autoinfer-part-a", "autoinfer-part-b", "autoinfer-umbrella")


def test_tie_break_rule_3_drops_required_parts(pkgconfig_env, auto_pkg_config_on):
    """Rule 3 in isolation: the component (``fam``) matches no candidate's
    Name/stem, so nothing narrows in rules 1-2; rule 3 drops the two parts
    named in ``autoinfer-umbrella``'s Requires and the umbrella wins. The
    contrast run without the umbrella stays ambiguous, isolating rule 3 as
    the decider."""
    candidates = ("autoinfer-part-a", "autoinfer-part-b", "autoinfer-umbrella")
    assert pkgconfig_infer.tie_break(candidates, "fam/x.h", None) == "autoinfer-umbrella"
    assert pkgconfig_infer.tie_break(candidates[:-1], "fam/x.h", None) is None


def test_covered_by_explicit_package_keeps_attribution(pkgconfig_env, auto_pkg_config_on):
    """Documented covered contract: an inferred package the user already
    names explicitly keeps its package/resolver/candidates attribution and
    reports REASON_COVERED as the reason."""
    config = _config(pkgconfig_env, explicit_packages=frozenset({"autoinfer"}))
    result = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})[0]
    assert result.reason == REASON_COVERED
    assert result.package == "autoinfer"
    assert result.resolver == "pc-index"


def test_covered_by_explicit_lib_stem(pkgconfig_env, auto_pkg_config_on):
    """Same demotion via the package's first ``-l`` stem (``-lautoinfer``)."""
    config = _config(pkgconfig_env, explicit_lib_stems=frozenset({"autoinfer"}))
    result = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})[0]
    assert result.reason == REASON_COVERED
    assert result.package == "autoinfer"
    assert result.resolver == "pc-index"


def test_covered_by_explicit_include_dir(pkgconfig_env, auto_pkg_config_on):
    """A header under an explicit ``-I`` is demoted before the resolver chain:
    REASON_COVERED with no package attribution (nothing to infer)."""
    include_dir = os.path.realpath(os.path.join(pkgconfig_env, "autoinfer-include"))
    config = _config(pkgconfig_env, explicit_include_dirs=(include_dir,))
    result = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})[0]
    assert result.reason == REASON_COVERED
    assert result.package is None
    assert result.resolver is None


def test_header_map_beats_everything_including_std(pkgconfig_env, auto_pkg_config_on):
    config = _config(pkgconfig_env, header_map={"vector": "autoinfer"})
    result = pkgconfig_infer.infer(["vector"], config, memo={})[0]
    assert result.package == "autoinfer"
    assert result.resolver == "map"
    assert result.reason == ""


def test_ignore_globs(pkgconfig_env, auto_pkg_config_on):
    config = _config(pkgconfig_env, ignore_globs=("autoinfer/*",))
    result = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})[0]
    assert result.reason == REASON_IGNORED
    assert result.package is None


def test_standard_header_fast_path(pkgconfig_env, auto_pkg_config_on):
    for name in ("vector", "sys/types.h"):
        result = pkgconfig_infer.infer([name], _config(pkgconfig_env), memo={})[0]
        assert result.reason == REASON_STD, name
        assert result.package is None
        assert result.resolver is None


def test_zlib_h_is_not_on_the_static_std_list(pkgconfig_env, auto_pkg_config_on):
    """``zlib.h`` must fall through the std fast path (it is a package
    header on many hosts); here it simply cannot be located under the pkgs
    dirs, proving it was not short-circuited as standard."""
    assert pkgconfig_infer.is_standard_header("zlib.h") is False
    result = pkgconfig_infer.infer(["zlib.h"], _config(pkgconfig_env), memo={})[0]
    assert result.reason == REASON_NOT_FOUND
    assert result.package is None


def test_header_not_found(pkgconfig_env, auto_pkg_config_on):
    result = pkgconfig_infer.infer(["nosuch/x.h"], _config(pkgconfig_env), memo={})[0]
    assert result.reason == REASON_NOT_FOUND
    assert result.package is None
    assert result.header_path is None


# ---------------------------------------------------------------------------
# Memoisation: module index cache and caller memo
# ---------------------------------------------------------------------------


def _counting_index(monkeypatch):
    """Wrap the real ``_run_pkg_config_index`` boundary with a call counter.
    Returns (counter dict, wrapper)."""
    original = pkgconfig_infer._run_pkg_config_index
    calls = {"count": 0}

    def wrapper():
        calls["count"] += 1
        return original()

    monkeypatch.setattr(pkgconfig_infer, "_run_pkg_config_index", wrapper)
    return calls


def test_index_built_once_then_rebuilt_on_cache_and_env_changes(
    pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path
):
    calls = _counting_index(monkeypatch)
    config = _config(pkgconfig_env)

    for _ in range(20):
        results = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
        assert results[0].package == "autoinfer"
    assert calls["count"] == 1

    pkgconfig_infer.clear_cache()
    pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
    assert calls["count"] == 2

    # A changed PKG_CONFIG_PATH is part of the memo key (F10): a new env
    # means a fresh index, even without clear_cache().
    empty_pcdir = tmp_path / "empty-pcdir"
    empty_pcdir.mkdir()
    monkeypatch.setenv("PKG_CONFIG_PATH", str(empty_pcdir))
    pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
    assert calls["count"] == 3


def test_apptools_clear_cache_fans_out_to_infer_index(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    calls = _counting_index(monkeypatch)
    config = _config(pkgconfig_env)

    first = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
    assert calls["count"] == 1

    apptools.clear_cache()
    second = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
    assert calls["count"] == 2
    assert second == first


def test_caller_memo_gains_entry_and_short_circuits(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    config = _config(pkgconfig_env)
    memo: dict = {}
    first = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo=memo)
    assert first[0].package == "autoinfer"
    assert len(memo) == 1

    # clear_cache() only clears module memos; the caller-owned memo still
    # holds the verdict, so the resolver chain (and its index boundary) is
    # never re-entered even with the resolvers poisoned.
    pkgconfig_infer.clear_cache()

    def poisoned(*args, **kwargs):
        raise RuntimeError("memo should have short-circuited the resolver chain")

    monkeypatch.setattr(pkgconfig_infer, "resolve_via_pc_index", poisoned)
    second = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo=memo)
    assert second == first
    assert second[0].package == "autoinfer"

    # Control: with a fresh memo the poisoned resolver really does change the
    # verdict -- proving the short-circuit above came from the memo.
    fresh = pkgconfig_infer.infer(["autoinfer/api.h"], config, memo={})
    assert fresh[0].package is None
    assert fresh[0].resolver is None


# ---------------------------------------------------------------------------
# Quiet existence probe (F5)
# ---------------------------------------------------------------------------


def test_pkg_config_exists_quiet_is_silent_even_under_error_policy(pkgconfig_env, auto_pkg_config_on, capsys):
    """A guessed package that misses is a diagnostic decision, not a build
    error: no stderr noise and no raise, even with --pkg-config-errors=error
    in force (the conftest autouse fixture restores the policy afterwards)."""
    capsys.readouterr()  # drain anything already captured
    apptools_pkgconfig.set_pkg_config_errors("error")

    exists = pkgconfig_infer.pkg_config_exists_quiet("definitely-not-a-package-xyz")

    captured = capsys.readouterr()
    assert exists is False
    assert captured.err == ""
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Never-raise contract (F8) and determinism
# ---------------------------------------------------------------------------


def test_resolver_chain_never_raises(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    def exploding(*args, **kwargs):
        raise RuntimeError("resolver boundary failure must be contained")

    monkeypatch.setattr(pkgconfig_infer, "resolve_via_pc_index", exploding)
    results = pkgconfig_infer.infer(["autoinfer/api.h", "vector"], _config(pkgconfig_env), memo={})
    assert len(results) == 2
    assert results[0].header == "autoinfer/api.h"
    assert results[0].package is None
    assert results[0].resolver is None
    assert results[1].reason == REASON_STD


def test_infer_is_deterministic(pkgconfig_env, auto_pkg_config_on):
    config = _config(pkgconfig_env)
    first = pkgconfig_infer.infer(["autoinfer/api.h", "umbrella/u.h", "vector", "nosuch/x.h"], config, memo={})
    second = pkgconfig_infer.infer(["autoinfer/api.h", "umbrella/u.h", "vector", "nosuch/x.h"], config, memo={})
    assert first == second
    assert [r.header for r in first] == ["autoinfer/api.h", "umbrella/u.h", "vector", "nosuch/x.h"]


# ---------------------------------------------------------------------------
# Stage 2: locate_header / classify_location -- compiler built-in guard
# ---------------------------------------------------------------------------


def test_locate_header_first_search_dir_wins(tmp_path):
    """Same relative spelling under two search dirs: the FIRST dir wins."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    for base in (first, second):
        (base / "myown").mkdir(parents=True)
        (base / "myown" / "dup.h").write_text("#pragma once\n")
    search_dirs = (str(first), str(second))
    expected = os.path.realpath(str(first / "myown" / "dup.h"))
    assert pkgconfig_infer.locate_header("myown/dup.h", search_dirs) == expected
    assert pkgconfig_infer.locate_header("myown/dup.h", search_dirs) == pkgconfig_infer._locate(
        "myown/dup.h", search_dirs
    )


def test_locate_header_absent_everywhere_is_none(tmp_path):
    """No search dir has the spelling: None (REASON_NOT_FOUND upstream)."""
    base = tmp_path / "only"
    base.mkdir()
    assert pkgconfig_infer.locate_header("myown/absent.h", (str(base),)) is None


def test_classify_location_under_builtin_dir(tmp_path):
    """A header under a non-default built-in dir classifies compiler-shipped."""
    gcc_include = tmp_path / "gcc" / "include"
    (gcc_include / "bits").mkdir(parents=True)
    header = gcc_include / "bits" / "foo.h"
    header.write_text("#pragma once\n")

    assert pkgconfig_infer.classify_location(str(header), (str(gcc_include),)) == REASON_COMPILER


def test_classify_location_default_include_dir_not_classified():
    """The /usr/include carve-out: a bare distro header stays resolvable.

    Pure path logic (realpath does not require the path to exist), so this
    exercises the real default dirs without touching the filesystem."""
    usr_include = pkgconfig_infer._DEFAULT_INCLUDE_DIRS[0]
    other_builtin = pkgconfig_infer._DEFAULT_INCLUDE_DIRS[1]
    assert pkgconfig_infer.classify_location(os.path.join(usr_include, "zlib.h"), (usr_include,)) is None
    # Both defaults in the builtin list: still no classification.
    assert pkgconfig_infer.classify_location(os.path.join(usr_include, "zlib.h"), (usr_include, other_builtin)) is None


def test_classify_location_empty_or_none_header_path(tmp_path):
    """Empty/None header paths classify as not-compiler-built-in (None)."""
    assert pkgconfig_infer.classify_location(None, (str(tmp_path),)) is None
    assert pkgconfig_infer.classify_location("", (str(tmp_path),)) is None


def test_classify_location_builtin_dir_not_in_default_dirs(tmp_path):
    """A built-in dir under the fixture tree (not /usr/include) still wins."""
    builtin_dir = tmp_path / "toolchain" / "lib" / "gcc" / "include"
    builtin_dir.mkdir(parents=True)
    header = builtin_dir / "omp.h"
    header.write_text("#pragma once\n")
    default_dirs = tuple(pkgconfig_infer._DEFAULT_INCLUDE_DIRS)

    assert pkgconfig_infer.classify_location(str(header), (*default_dirs, str(builtin_dir))) == REASON_COMPILER


def test_infer_classifies_builtin_dir_header_end_to_end(tmp_path):
    """End to end: a header only reachable under a built-in dir resolves to
    REASON_COMPILER without reaching the pkg-config resolver chain."""
    builtin_dir = tmp_path / "toolchain" / "include"
    (builtin_dir / "gccpriv").mkdir(parents=True)
    header = builtin_dir / "gccpriv" / "foo.h"
    header.write_text("#pragma once\n")
    config = InferenceConfig(
        search_dirs=(str(builtin_dir),),
        compiler_builtin_dirs=(str(builtin_dir),),
    )
    (result,) = pkgconfig_infer.infer(["gccpriv/foo.h"], config, memo={})
    assert result.header_path == os.path.realpath(str(header))
    assert result.package is None
    assert result.resolver is None
    assert result.reason == REASON_COMPILER


def test_infer_default_builtin_dir_header_stays_resolvable(tmp_path, monkeypatch):
    """A header under a built-in dir that counts as a DEFAULT dir must NOT
    classify compiler-shipped (F6 carve-out): it falls through to the
    resolver chain, which yields the unresolved no-owner verdict on this
    fixture tree. The default-dir list is retargeted to the tmp tree so the
    test stays host-independent."""
    usr_include = tmp_path / "usr" / "include"
    usr_include.mkdir(parents=True)
    header = usr_include / "myown.h"
    header.write_text("#pragma once\n")
    monkeypatch.setattr(pkgconfig_infer, "_DEFAULT_INCLUDE_DIRS", (str(usr_include),))
    config = InferenceConfig(
        resolvers=(),
        search_dirs=(str(usr_include),),
        compiler_builtin_dirs=(str(usr_include),),
    )
    (result,) = pkgconfig_infer.infer(["myown.h"], config, memo={})
    # Not classified compiler-shipped: the header fell through to the
    # resolver chain (all stages disabled via resolvers=(), so the terminal
    # verdict is the no-owner default), with no package attributed.
    assert result.reason != REASON_COMPILER
    assert result.package is None
    assert result.header_path == os.path.realpath(str(header))


# ---------------------------------------------------------------------------
# Stage 3: distro-DB resolver
# ---------------------------------------------------------------------------

# Real output formats, one owner-query fixture per backend, all answering for
# /usr/include/zlib.h. The dpkg line uses the arch-qualified shape; apk strips
# the version by dropping the last two dash components.
_ZLIB_OWNER_STUBS = {
    "rpm": "zlib-devel\n",
    "dpkg": "zlib1g-dev:amd64: /usr/include/zlib.h\n",
    "pacman": "zlib\n",
    "apk": "/usr/include/zlib.h is owned by zlib-dev-1.2.11-r17\n",
}
# The file list each backend's owner query implies; the zlib.pc line lives in
# the live pkg-config search dir (the pkgconfig_env dir).
_ZLIB_LIST_STUBS = {
    "rpm": "/usr/include/zlib.h\n/usr/lib64/libz.so\n{pcdir}/zlib.pc\n",
    "dpkg": "/usr/include/zlib.h\n/usr/lib/x86_64-linux-gnu/libz.so\n{pcdir}/zlib.pc\n",
    "pacman": "/usr/include/zlib.h\n/usr/lib/libz.so\n{pcdir}/zlib.pc\n",
    "apk": "/usr/include/zlib.h\n/usr/lib/libz.so\n{pcdir}/zlib.pc\n",
}


@pytest.mark.parametrize("backend", ["rpm", "dpkg", "pacman", "apk"])
def test_distro_owner_with_single_pc_resolves(pkgconfig_env, auto_pkg_config_on, monkeypatch, backend):
    """Each backend's real owner/list output format: /usr/include/zlib.h is
    owned by the distro zlib devel package, whose one .pc ships inside the
    live pkg-config search dir -> package ``zlib`` via resolver
    ``distro:<backend>``. The .pc file itself need not exist on disk: the
    package file list is the evidence."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", backend)
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(
        pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: _ZLIB_OWNER_STUBS[backend]
    )
    monkeypatch.setattr(
        pkgconfig_infer,
        "_run_distro_list_query",
        lambda pkg, called_backend: _ZLIB_LIST_STUBS[backend].format(pcdir=pkgconfig_env),
    )
    result = pkgconfig_infer.resolve_via_distro("zlib.h", "/usr/include/zlib.h")
    assert result.package == "zlib"
    assert result.resolver == f"distro:{backend}"
    assert result.candidates == ("zlib",)
    assert result.reason == ""


def test_distro_umbrella_pc_wins(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """Owner ships libssl.pc/libcrypto.pc/openssl.pc: the tie-break picks the
    umbrella. Here the .pc files must really exist (in a temp PKG_CONFIG_PATH
    dir) so the tie-break can read openssl.pc's Requires line."""
    pcdir = tmp_path / "ssl-pc"
    pcdir.mkdir()
    (pcdir / "openssl.pc").write_text("Name: OpenSSL\nLibs: -lssl -lcrypto\nRequires: libssl libcrypto\n")
    (pcdir / "libssl.pc").write_text("Libs: -lssl\n")
    (pcdir / "libcrypto.pc").write_text("Libs: -lcrypto\n")
    monkeypatch.setenv("PKG_CONFIG_PATH", str(pcdir))
    monkeypatch.setenv("CT_DISTRO_BACKEND", "dpkg")
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(
        pkgconfig_infer,
        "_run_distro_owner_query",
        lambda paths, called_backend: "openssl-devel:amd64: /usr/include/openssl/ssl.h\n",
    )
    monkeypatch.setattr(
        pkgconfig_infer,
        "_run_distro_list_query",
        lambda pkg, called_backend: (
            f"/usr/include/openssl/ssl.h\n{pcdir}/libssl.pc\n{pcdir}/libcrypto.pc\n{pcdir}/openssl.pc\n"
        ),
    )
    result = pkgconfig_infer.resolve_via_distro("openssl/ssl.h", "/usr/include/openssl/ssl.h")
    assert result.package == "openssl"
    assert result.resolver == "distro:dpkg"


def test_distro_owner_without_pc_reports_no_pc(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """Boost case: the owning devel package ships headers but no .pc anywhere
    -> REASON_NO_PC naming the owner, unresolved, no guess."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "rpm")
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: "boost-devel\n")
    monkeypatch.setattr(
        pkgconfig_infer,
        "_run_distro_list_query",
        lambda pkg, called_backend: "/usr/include/boost/version.hpp\n/usr/include/boost/functional.hpp\n",
    )
    result = pkgconfig_infer.resolve_via_distro("boost/version.hpp", "/usr/include/boost/version.hpp")
    assert result.package is None
    assert result.resolver is None
    assert result.reason.startswith(REASON_NO_PC)
    assert "boost-devel" in result.reason


def test_distro_libc_owner_counts_as_standard(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """F6 part 2: an owner that is libc/kernel material (kernel-headers) is a
    standard header for inference purposes; no package-list query is even
    attempted for it."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "rpm")
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: "kernel-headers\n")
    calls = {"list": 0}

    def counting_list(pkg, called_backend):
        calls["list"] += 1
        return ""

    monkeypatch.setattr(pkgconfig_infer, "_run_distro_list_query", counting_list)
    result = pkgconfig_infer.resolve_via_distro("sys/types.h", "/usr/include/sys/types.h")
    assert calls["list"] == 0
    assert result.package is None
    assert result.reason == REASON_STD


def test_distro_backend_none_runs_no_boundary(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """CT_DISTRO_BACKEND=none masks the distro resolver: unresolved with the
    missing-database reason, and NEITHER distro boundary is ever entered
    (this is a counting-stub interaction test; the call counts are the whole
    contract here)."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
    pkgconfig_infer.clear_cache()
    calls = {"owner": 0, "list": 0}

    def counting_owner(paths, called_backend):
        calls["owner"] += 1
        return ""

    def counting_list(pkg, called_backend):
        calls["list"] += 1
        return ""

    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", counting_owner)
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_list_query", counting_list)
    result = pkgconfig_infer.resolve_via_distro("custom.h", "/usr/include/custom.h")
    assert calls == {"owner": 0, "list": 0}
    assert result.package is None
    assert result.resolver is None
    assert result.reason.startswith(REASON_NO_OWNER)
    assert "no distro package database" in result.reason


def test_detect_distro_backend_env_override_and_memo(monkeypatch):
    """CT_DISTRO_BACKEND forces or masks the backend without touching PATH,
    even over a memoised detection result; the PATH probe itself runs once
    and a real None detection result is memoised exactly like a found one."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "pacman")
    assert pkgconfig_infer.detect_distro_backend() == "pacman"
    monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
    assert pkgconfig_infer.detect_distro_backend() is None

    monkeypatch.delenv("CT_DISTRO_BACKEND", raising=False)
    pkgconfig_infer.clear_cache()
    probes = {"count": 0}

    def counting_which(tool):
        probes["count"] += 1
        return  # no backend tool on this PATH

    monkeypatch.setattr(pkgconfig_infer.shutil, "which", counting_which)
    assert pkgconfig_infer.detect_distro_backend() is None
    assert pkgconfig_infer.detect_distro_backend() is None
    assert probes["count"] == len(pkgconfig_infer._DISTRO_BACKEND_TOOLS)
    pkgconfig_infer.clear_cache()
    assert pkgconfig_infer.detect_distro_backend() is None
    assert probes["count"] == 2 * len(pkgconfig_infer._DISTRO_BACKEND_TOOLS)

    # The override wins even while a None detection is memoised.
    monkeypatch.setenv("CT_DISTRO_BACKEND", "apk")
    assert pkgconfig_infer.detect_distro_backend() == "apk"


def test_infer_batches_one_owner_query_per_call(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """Plan §0: ONE distro owner query per TU. Five unresolved headers in a
    private include dir -> exactly one _run_distro_owner_query call carrying
    all five located paths."""
    include_dir = tmp_path / "batch-include"
    include_dir.mkdir()
    names = [f"batched{index}.h" for index in range(5)]
    for name in names:
        (include_dir / name).write_text("#pragma once\n")
    config = InferenceConfig(search_dirs=(str(include_dir),))
    monkeypatch.setenv("CT_DISTRO_BACKEND", "rpm")
    pkgconfig_infer.clear_cache()
    calls = {"owner": 0, "paths": []}

    def counting_owner(paths, called_backend):
        calls["owner"] += 1
        calls["paths"].append(list(paths))
        return "\n".join(f"file {path} is not owned by any package" for path in paths)

    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", counting_owner)
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_list_query", lambda pkg, called_backend: None)
    results = pkgconfig_infer.infer(names, config, memo={})
    assert calls["owner"] == 1
    assert sorted(calls["paths"][0]) == sorted(os.path.realpath(str(include_dir / name)) for name in names)
    assert [result.package for result in results] == [None] * 5
    assert [result.header for result in results] == names


def test_distro_pc_outside_search_dirs_is_ignored(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """A .pc the package ships somewhere pkg-config never looks does not
    count: the owner reports REASON_NO_PC even though its list contains a
    .pc."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "pacman")
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: "zlib\n")
    monkeypatch.setattr(
        pkgconfig_infer,
        "_run_distro_list_query",
        lambda pkg, called_backend: "/usr/include/zlib.h\n/opt/other/pkgconfig/zlib.pc\n",
    )
    result = pkgconfig_infer.resolve_via_distro("zlib.h", "/usr/include/zlib.h")
    assert result.package is None
    assert result.reason.startswith(REASON_NO_PC)
    assert "zlib" in result.reason


@pytest.mark.parametrize(
    ("backend", "garbage"),
    [
        ("rpm", "a-package\nanother-package\n"),  # 2 lines for 1 path: unalignable
        ("rpm", None),  # failed boundary (tool gone, interrupted) -> None
        ("dpkg", "dpkg-query: no path found matching pattern /usr/include/zlib.h\n"),
        ("pacman", "one\ntwo\n"),  # 2 lines for 1 path: unalignable
        ("apk", "ERROR: unable to select packages: zlib\n"),
    ],
)
def test_distro_unparseable_owner_output_is_unresolved(
    pkgconfig_env, auto_pkg_config_on, monkeypatch, backend, garbage
):
    """Garbage or missing owner output maps to REASON_NO_OWNER -- never a
    guess, never an exception, regardless of backend output shape."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", backend)
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: garbage)
    result = pkgconfig_infer.resolve_via_distro("zlib.h", "/usr/include/zlib.h")
    assert result.package is None
    assert result.resolver is None
    assert result.reason == REASON_NO_OWNER


def test_pc_stems_memoised_per_owner_and_env(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """pc_stems_shipped_by hits the package-list boundary once per
    (owner, backend, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR); clear_cache() and a
    different owner each force a fresh query."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "rpm")
    pkgconfig_infer.clear_cache()
    calls = {"count": 0}

    def counting_list(pkg, called_backend):
        calls["count"] += 1
        return f"{pkgconfig_env}/zlib.pc\n"

    monkeypatch.setattr(pkgconfig_infer, "_run_distro_list_query", counting_list)
    first = pkgconfig_infer.pc_stems_shipped_by("zlib-devel", "rpm")
    assert pkgconfig_infer.pc_stems_shipped_by("zlib-devel", "rpm") == first
    assert first == ("zlib",)
    assert calls["count"] == 1

    pkgconfig_infer.clear_cache()
    pkgconfig_infer.pc_stems_shipped_by("zlib-devel", "rpm")
    assert calls["count"] == 2

    pkgconfig_infer.pc_stems_shipped_by("other-devel", "rpm")
    assert calls["count"] == 3


@pytest.mark.skipif(not shutil.which("rpm"), reason="rpm not available on this host")
@pytest.mark.skipif(not os.path.isfile("/usr/include/stdio.h"), reason="/usr/include/stdio.h not present")
def test_live_rpm_owner_smoke(pkgconfig_env, auto_pkg_config_on):
    """Live distro-DB boundary: /usr/include/stdio.h has an owning package on
    any rpm host with glibc headers installed. No assertion on the name --
    distros differ (glibc-headers vs glibc-devel)."""
    owners = pkgconfig_infer.owners_for_headers(["/usr/include/stdio.h"], "rpm")
    assert bool(owners.get("/usr/include/stdio.h"))


# ---------------------------------------------------------------------------
# Stage 4: libc-owner guard through the chain, no-backend fallback, man stub
# ---------------------------------------------------------------------------


def test_kernel_headers_futex_path_counts_as_standard(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """Stage 4 / F6 part 2, futex shape: a ``/usr/include/linux/futex.h``
    path whose distro owner is ``kernel-headers`` is a standard header via
    the ``_LIBC_OWNERS`` check -- no package-list query is attempted."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "rpm")
    pkgconfig_infer.clear_cache()
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", lambda paths, called_backend: "kernel-headers\n")
    calls = {"list": 0}

    def counting_list(pkg, called_backend):
        calls["list"] += 1
        return ""

    monkeypatch.setattr(pkgconfig_infer, "_run_distro_list_query", counting_list)
    result = pkgconfig_infer.resolve_via_distro("linux/futex.h", "/usr/include/linux/futex.h")
    assert calls["list"] == 0
    assert result.package is None
    assert result.resolver is None
    assert result.reason == REASON_STD


def test_no_distro_backend_nonstd_name_is_not_standard(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """Fallback half of the std guard: with no distro backend at all, a
    non-standard name under a default include dir is NOT classified standard
    -- only the static fast-path list applies. A libc-prefix sibling in the
    same call still takes that fast path."""
    monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
    pkgconfig_infer.clear_cache()
    usr_include = tmp_path / "usr" / "include"
    usr_include.mkdir(parents=True)
    (usr_include / "myown.h").write_text("#pragma once\n")
    config = InferenceConfig(search_dirs=(str(usr_include),))
    results = pkgconfig_infer.infer(["myown.h", "sys/types.h"], config, memo={})
    by_header = {result.header: result for result in results}
    assert by_header["myown.h"].reason == REASON_NO_OWNER
    assert by_header["myown.h"].package is None
    assert by_header["myown.h"].resolver is None
    assert by_header["sys/types.h"].reason == REASON_STD


def test_man_resolver_enabled_runs_stub_without_boundary(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """resolvers=("man",): the stub resolver settles the header with the
    not-implemented reason and the man boundary is never entered (the
    call-count is the drop-in contract; the reason assertion is the
    behavioural half)."""
    include_dir = tmp_path / "man-include"
    include_dir.mkdir()
    (include_dir / "weird.h").write_text("#pragma once\n")
    config = InferenceConfig(resolvers=("man",), search_dirs=(str(include_dir),))
    calls = {"man": 0}

    def counting_man(header):
        calls["man"] += 1

    monkeypatch.setattr(pkgconfig_infer, "_run_man_lookup", counting_man)
    (result,) = pkgconfig_infer.infer(["weird.h"], config, memo={})
    assert calls["man"] == 0
    assert result.package is None
    assert result.resolver is None
    assert result.reason == "man resolver not implemented"


def test_default_resolvers_never_invoke_man(pkgconfig_env, auto_pkg_config_on, monkeypatch, tmp_path):
    """Default resolvers ("pc", "distro"): resolve_via_man is not invoked at
    all -- patched to raise, the chain still completes with the distro
    stage's no-owner verdict, never the man stub's reason."""
    include_dir = tmp_path / "default-include"
    include_dir.mkdir()
    (include_dir / "unowned.h").write_text("#pragma once\n")
    config = InferenceConfig(search_dirs=(str(include_dir),))
    monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
    pkgconfig_infer.clear_cache()
    calls = {"man": 0}

    def exploding_man(header, header_path):
        calls["man"] += 1
        raise AssertionError("resolve_via_man must not run under default resolvers")

    monkeypatch.setattr(pkgconfig_infer, "resolve_via_man", exploding_man)
    (result,) = pkgconfig_infer.infer(["unowned.h"], config, memo={})
    assert calls["man"] == 0
    assert result.package is None
    assert result.resolver is None
    assert result.reason.startswith(REASON_NO_OWNER)
    assert result.reason != "man resolver not implemented"


def test_std_vector_short_circuits_before_any_boundary(pkgconfig_env, auto_pkg_config_on, monkeypatch):
    """The std fast path fires before EVERY subprocess boundary: with all
    three counting stubs in place, ``<vector>`` settles without a single
    call (the zero counts are the whole contract here; the REASON_STD
    assertion is the behavioural half)."""
    calls = {"index": 0, "owner": 0, "man": 0}

    def counting_index():
        calls["index"] += 1
        return [], {}

    def counting_owner(paths, called_backend):
        calls["owner"] += 1
        return ""

    def counting_man(header):
        calls["man"] += 1

    monkeypatch.setattr(pkgconfig_infer, "_run_pkg_config_index", counting_index)
    monkeypatch.setattr(pkgconfig_infer, "_run_distro_owner_query", counting_owner)
    monkeypatch.setattr(pkgconfig_infer, "_run_man_lookup", counting_man)
    (result,) = pkgconfig_infer.infer(["vector"], _config(pkgconfig_env), memo={})
    assert calls == {"index": 0, "owner": 0, "man": 0}
    assert result.reason == REASON_STD
    assert result.package is None


@pytest.mark.parametrize(
    "prefix",
    [
        "sys/",
        "netinet/",
        "arpa/",
        "linux/",
        "asm/",
        "asm-generic/",
        "bits/",
        "gnu/",
        "rpc/",
        "net/",
        "netpacket/",
    ],
)
def test_std_prefix_fast_path(prefix):
    """Every libc/kernel prefix (including the Stage 4 addition
    ``netpacket/``) is classified standard by the static fast path."""
    assert pkgconfig_infer.is_standard_header(prefix + "whatever.h") is True
