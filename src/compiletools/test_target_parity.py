"""Cross-tool target parity: what ct-findtargets reports is what ct-cake builds.

ct-findtargets exists to answer "what would ct-cake --auto build here?" --
scripts/ct-build pipes its ``--style=args`` output straight into
ct-create-makefile. When the two tools disagree the answer is silently wrong
downstream, so the agreement is pinned here as set equality over four target
buckets (executables, tests, static libraries, dynamic libraries) rather than
as a property of either tool alone.

Every test drives BOTH tools over one shared fixture tree. The two sides are
read through deliberately different surfaces so a single bug cannot satisfy
both: ct-findtargets is read from its own stdout, ct-cake from the text of
the Makefile it generates. Neither reader calls production naming code -- a
Namer bug would otherwise cancel out and leave the comparison looking
stronger than it is. Target *selection* is what is under test.

``_cake_targets`` is the load-bearing piece of infrastructure and is itself
pinned by ``TestTheCakeReaderIsNotVacuous``: a reader that quietly matched
nothing would make every set-equality assertion below pass.
"""

from __future__ import annotations

import itertools
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

import compiletools.cake
import compiletools.findtargets
import compiletools.makefile_backend
import compiletools.testhelper as uth

# Buckets, in the order a reader should report them. The two library buckets
# are what ct-findtargets gained when the library-slot rejection was replaced
# by reporting; see the module docstring.
EXES, TESTS, STATIC, DYNAMIC = "exes", "tests", "static", "dynamic"
BUCKETS = (EXES, TESTS, STATIC, DYNAMIC)


@pytest.fixture(autouse=True)
def isolated_caches():
    """Every test here runs whole ct-cake and ct-findtargets pipelines, which
    populate the process-global wrappedos / git / configutils caches. Those
    caches key on paths, and a stale entry from the previous test's fixture
    tree makes discovery return NOTHING for this one -- an empty target set,
    exit 0, no diagnostic. Cheap to prevent, expensive to debug."""
    uth.reset()
    yield
    uth.reset()


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------

# The library source is an ORPHAN: no main, no test marker, and included by
# nothing. Discovery cannot reach it and no implied-source rule pulls it in,
# so its presence in any tool's answer is caused by the library slot alone.
#
# Every body carries a tree-unique tag. Two fixture trees with byte-identical
# sources collide in the content-hash registry and the loser builds NOTHING --
# an empty target set, silently, with exit 0. A plain counter is not enough:
# it restarts at zero in every xdist worker, which reproduced the empty set
# under -n only. The tag is therefore unique per process AND per tree.
_ORPHAN_BODY = "// {tag}\nint orphan_widget() { return 3; }\n"
_MAIN_BODY = "// {tag}\nint main() { return 0; }\n"
_TEST_BODY = '// {tag}\n#include <cstdio>\nint main() { std::printf("ok\\n"); return 0; }\n'

_RUN_TOKEN = uuid.uuid4().hex[:12]
_SERIAL = itertools.count()


def _unique_tag():
    return f"parity-{_RUN_TOKEN}-{next(_SERIAL)}"


def make_fixture(tmp_path, *, slot_line=None, slot_tier="subproject", name="parityrepo"):
    """A git repo with one discoverable executable and one orphan library source.

    *slot_line* is a single ct.conf line such as ``static = lib/orphan.cpp``.
    *slot_tier* places it either in ``app/ct.conf`` -- reachable only once
    ``--auto`` discovery finds ``app/main.cpp`` and re-anchors onto it -- or in
    the gitroot ``ct.conf``, which the pre-parse tiers already see. The two
    tiers land on opposite sides of the discovery gate and that difference is
    the subject of ``TestTheDiscoveryGateHasTwoSides``.

    Passing ``slot_line=None`` yields the control arm: a byte-identical tree
    but for the missing conf file, so every "the slot caused this" assertion
    has something to be causal against.
    """
    root = Path(tmp_path) / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    rootconf_extra = [slot_line] if slot_line and slot_tier == "gitroot" else []
    uth.create_temp_ct_conf(str(root), extralines=rootconf_extra)

    tag = _unique_tag()
    (root / "app").mkdir()
    (root / "app" / "main.cpp").write_text(_MAIN_BODY.replace("{tag}", tag))
    (root / "lib").mkdir()
    (root / "lib" / "orphan.cpp").write_text(_ORPHAN_BODY.replace("{tag}", tag))
    (root / "lib" / "test_orphan.cpp").write_text(_TEST_BODY.replace("{tag}", tag))

    if slot_line and slot_tier == "subproject":
        (root / "app" / "ct.conf").write_text(slot_line + "\n")
    return root


def shared_argv(root):
    """The argv every tool gets, so neither side is measured under a config
    the other did not see. Pins the compiler (the suite never hardcodes one)
    and redirects all four CAS pools into the fixture."""
    config = uth.create_temp_config(str(root))
    return [
        "--config=" + config,
        "--cas-objdir=" + str(root / "cas" / "obj"),
        "--cas-pchdir=" + str(root / "cas" / "pch"),
        "--cas-pcmdir=" + str(root / "cas" / "pcm"),
        "--cas-exedir=" + str(root / "cas" / "exe"),
    ]


def _relative(root, path):
    """The one agreed set-identity recipe, applied to both tools' answers.

    ct-findtargets emits a conf-sourced target in the conf file's own
    (relative) spelling beside discovered targets in absolute form, and
    ct-cake's makefile carries realpaths. Comparing before normalising would
    fail on spelling rather than on target selection, which is what is under
    test here; the spelling itself is pinned separately by
    ``TestThePathShapesEachToolEmits``.

    A relative entry is resolved against the fixture root, NOT the process
    cwd: the readers run after the ``DirectoryContext`` has exited, so cwd
    resolution silently produced a path climbing out of /tmp and into the
    worktree, which compares unequal for a reason that has nothing to do
    with either tool.
    """
    if not os.path.isabs(path):
        path = os.path.join(str(root), path)
    return os.path.relpath(os.path.realpath(path), os.path.realpath(str(root)))


# ---------------------------------------------------------------------------
# Reading ct-cake: the generated Makefile
# ---------------------------------------------------------------------------

_PUBLISH = re.compile(r"ct-cas-publish --cas-path (\S+) --user-path \S+ --source-realpath (\S+)")
_ARCHIVE = re.compile(r"^\tar -\S+ (\S+)", re.MULTILINE)
_SHARED = re.compile(r"-shared -o (\S+)")
_RUNTESTS = re.compile(r"^runtests:(.*)$", re.MULTILINE)


def parse_cake_makefile(root, text):
    """Bucket -> set of source paths, read from the Makefile text alone.

    Each published artefact names its own source (``--source-realpath``), so
    the mapping comes from ct-cake's output rather than from re-deriving
    artefact names with Namer. The bucket comes from the rule that PRODUCES
    the cached artefact -- ``ar`` for a static library, a ``-shared`` link for
    a dynamic one, a plain link otherwise -- and a plain link is a test rather
    than an executable when its ``.result`` marker is a ``runtests``
    prerequisite.
    """
    archives = set(_ARCHIVE.findall(text))
    shared = set(_SHARED.findall(text))
    runtests_line = _RUNTESTS.search(text)
    results = set(runtests_line.group(1).split()) if runtests_line else set()

    found = {bucket: set() for bucket in BUCKETS}
    for cas_path, source in _PUBLISH.findall(text):
        if cas_path in archives:
            bucket = STATIC
        elif cas_path in shared:
            bucket = DYNAMIC
        elif cas_path + ".result" in results:
            bucket = TESTS
        else:
            bucket = EXES
        found[bucket].add(_relative(root, source))
    return {bucket: frozenset(paths) for bucket, paths in found.items()}


def cake_targets(root, argv):
    """What ct-cake would build, without building it.

    ``--clean`` runs the same gather -> discovery -> build_graph -> generate
    sequence as a real build and then cleans instead of executing, so the
    Makefile on disk is the build's own target set. That equivalence is not
    assumed: ``test_the_clean_makefile_matches_the_building_one`` measures it.
    """
    makefile = Path(root) / "Makefile.parity"
    full = argv + ["--backend=make", "--makefilename=" + str(makefile), "--clean"]
    with uth.DirectoryContext(str(root)), uth.ParserContext():
        returncode = compiletools.cake.main(full)
    assert returncode == 0, f"ct-cake exited {returncode} on {root}"
    return parse_cake_makefile(root, makefile.read_text())


# ---------------------------------------------------------------------------
# Reading ct-findtargets: its own stdout
# ---------------------------------------------------------------------------

_SECTIONS = {
    "Executable Targets": EXES,
    "Test Targets": TESTS,
    "Static Library Targets": STATIC,
    "Dynamic Library Targets": DYNAMIC,
}


def parse_findtargets_indent(root, text):
    """Bucket -> set of source paths, read from ``--style=indent`` output.

    The labelled style is the stable reader: it survives any ordering choice
    ArgsStyle makes. A section this parser does not know about is an error
    rather than a silent drop, because a silently dropped bucket is the
    defect class this whole module exists to catch.
    """
    found = {bucket: set() for bucket in BUCKETS}
    bucket = None
    for line in text.splitlines():
        if line.startswith("\t"):
            entry = line.strip()
            if entry != "None found":
                assert bucket is not None, f"indented entry {entry!r} outside any section"
                found[bucket].add(_relative(root, entry))
        elif line.endswith(":"):
            label = line[:-1]
            assert label in _SECTIONS, f"unknown ct-findtargets section {label!r}"
            bucket = _SECTIONS[label]
    return {bucket: frozenset(paths) for bucket, paths in found.items()}


def _drain(capsys):
    """Discard anything an earlier in-process tool run left in the capture
    buffer. A test that calls ct-cake first would otherwise read ct-cake's
    banner as ct-findtargets' first target -- the real ct-build pipeline runs
    the two tools as separate processes and has no such crosstalk."""
    capsys.readouterr()


def findtargets_targets(root, argv, capsys, extra=()):
    """ct-findtargets' own answer, read from its stdout.

    A refusal is caught and reported as a failed comparison rather than an
    escaping ``SystemExit``: refusing to answer is one of the ways the two
    tools disagree, so it belongs in the same failure channel as a wrong set.
    """
    _drain(capsys)
    with uth.DirectoryContext(str(root)), uth.ParserContext():
        try:
            returncode = compiletools.findtargets.main(argv + ["--style=indent"] + list(extra))
        except SystemExit as exit_request:
            returncode = exit_request.code
    captured = capsys.readouterr()
    assert returncode == 0, f"ct-findtargets exited {returncode}: {captured.err.strip()}"
    return parse_findtargets_indent(root, captured.out)


def nonempty(targets):
    return frozenset().union(*targets.values())


# ---------------------------------------------------------------------------
# Infrastructure pins: without these, every assertion below could be vacuous
# ---------------------------------------------------------------------------


class TestTheCakeReaderIsNotVacuous:
    """``parse_cake_makefile`` scrapes generated text, so a format change
    would silently turn every bucket empty and every set-equality assertion
    green. These pin the reader against exactly that."""

    def test_the_reader_finds_the_control_tree_targets(self, tmp_path):
        """The plain fixture has one executable and one test and no
        libraries. A reader returning nothing must fail here."""
        root = make_fixture(tmp_path)
        found = cake_targets(root, shared_argv(root))
        assert found[EXES] == frozenset({"app/main.cpp"})
        assert found[TESTS] == frozenset({"lib/test_orphan.cpp"})
        assert found[STATIC] == frozenset()
        assert found[DYNAMIC] == frozenset()

    def test_the_reader_separates_a_library_from_an_executable(self, tmp_path):
        """The bucket, not just the path: a reader that lumped every
        published artefact into one bucket would pass the test above."""
        root = make_fixture(tmp_path, slot_line="static = lib/orphan.cpp", slot_tier="gitroot")
        found = cake_targets(root, shared_argv(root))
        assert found[STATIC] == frozenset({"lib/orphan.cpp"})
        assert found[EXES] == frozenset()

    def test_the_clean_makefile_matches_the_building_one(self, tmp_path):
        """``cake_targets`` reads a ``--clean`` run to avoid compiling. That
        is only sound if the generated Makefile is the same one a real build
        would use, which this measures on both sides rather than assuming."""
        root = make_fixture(tmp_path, slot_line="static = lib/orphan.cpp")
        argv = shared_argv(root)
        built = Path(root) / "Makefile.built"
        with uth.DirectoryContext(str(root)), uth.ParserContext():
            returncode = compiletools.cake.main(argv + ["--backend=make", "--makefilename=" + str(built)])
        assert returncode == 0
        assert parse_cake_makefile(root, built.read_text()) == cake_targets(root, argv)


class TestTheFindtargetsReaderIsNotVacuous:
    def test_the_reader_finds_the_control_tree_targets(self, tmp_path, capsys):
        root = make_fixture(tmp_path)
        found = findtargets_targets(root, shared_argv(root), capsys)
        assert found[EXES] == frozenset({"app/main.cpp"})
        assert found[TESTS] == frozenset({"lib/test_orphan.cpp"})

    def test_an_unknown_section_label_is_an_error_not_a_silent_drop(self, tmp_path):
        """A new bucket added to ct-findtargets without updating this module
        must break the module loudly. Silently ignoring the section would let
        the next reporting gap ship exactly as this one did."""
        with pytest.raises(AssertionError, match="unknown ct-findtargets section"):
            parse_findtargets_indent(tmp_path, "Module Targets:\n\tsrc/thing.cppm\n")


# ---------------------------------------------------------------------------
# The parity matrix
# ---------------------------------------------------------------------------

# Each row is (id, slot_line, slot_tier, extra_argv, expected buckets). The
# expected sets are MEASURED from ct-cake, not predicted: asserting only that
# the two tools agree would pass just as well if both agreed on a wrong set,
# so the row records what the agreed answer has to be.
ROWS = (
    ("plain", None, "subproject", (), {EXES: ("app/main.cpp",), TESTS: ("lib/test_orphan.cpp",)}),
    (
        "discovery_static",
        "static = lib/orphan.cpp",
        "subproject",
        (),
        {EXES: ("app/main.cpp",), TESTS: ("lib/test_orphan.cpp",), STATIC: ("lib/orphan.cpp",)},
    ),
    (
        "discovery_dynamic",
        "dynamic = lib/orphan.cpp",
        "subproject",
        (),
        {EXES: ("app/main.cpp",), TESTS: ("lib/test_orphan.cpp",), DYNAMIC: ("lib/orphan.cpp",)},
    ),
    (
        "discovery_tests",
        "tests = lib/orphan.cpp",
        "subproject",
        (),
        {EXES: ("app/main.cpp",), TESTS: ("lib/orphan.cpp", "lib/test_orphan.cpp")},
    ),
    (
        "explicit_target_plus_subproject_static",
        "static = lib/orphan.cpp",
        "subproject",
        ("app/main.cpp",),
        {EXES: ("app/main.cpp",), STATIC: ("lib/orphan.cpp",)},
    ),
    (
        "argv_static",
        None,
        "subproject",
        ("--static", "lib/orphan.cpp"),
        {STATIC: ("lib/orphan.cpp",)},
    ),
    (
        "gitroot_conf_static",
        "static = lib/orphan.cpp",
        "gitroot",
        (),
        {STATIC: ("lib/orphan.cpp",)},
    ),
    (
        "gitroot_conf_dynamic",
        "dynamic = lib/orphan.cpp",
        "gitroot",
        (),
        {DYNAMIC: ("lib/orphan.cpp",)},
    ),
)

_ROW_IDS = [row[0] for row in ROWS]

# The agreement half does not need the expected sets; keeping them out of its
# signature stops a reader assuming agreement is checked against them.
_TREE_PARAMS = pytest.mark.parametrize("slot_line,slot_tier,extra_argv", [row[1:4] for row in ROWS], ids=_ROW_IDS)
_ROW_PARAMS = pytest.mark.parametrize(
    "slot_line,slot_tier,extra_argv,expected", [row[1:] for row in ROWS], ids=_ROW_IDS
)


def _expected_buckets(expected):
    return {bucket: frozenset(expected.get(bucket, ())) for bucket in BUCKETS}


class TestReportedSetEqualsBuiltSet:
    """The invariant: over one tree, the four buckets ct-findtargets reports
    are the four buckets ct-cake builds.

    The rows cover all four ways a target reaches the namespace -- argv, the
    gitroot conf, a conf tier anchored on an explicitly named target, and a
    conf tier reachable only once ``--auto`` discovery re-anchors onto a
    discovered target. The last is the one that shipped broken: the library
    slot landed after both of ct-findtargets' rejection points and was
    dropped from the listing without a word, while ct-cake built the library
    and linked the discovered executable against it.
    """

    @_TREE_PARAMS
    def test_the_two_tools_agree(self, tmp_path, capsys, slot_line, slot_tier, extra_argv):
        root = make_fixture(tmp_path, slot_line=slot_line, slot_tier=slot_tier)
        argv = shared_argv(root) + list(extra_argv)
        built = cake_targets(root, argv)
        # Guard against the readers both matching nothing, which would make
        # the equality below true and meaningless.
        assert nonempty(built), "the cake reader found no targets at all"
        assert findtargets_targets(root, argv, capsys) == built

    @_ROW_PARAMS
    def test_the_agreed_set_is_the_measured_one(self, tmp_path, slot_line, slot_tier, extra_argv, expected):
        """Agreement alone is satisfiable by two tools that are wrong in the
        same way. This pins what the answer has to be."""
        root = make_fixture(tmp_path, slot_line=slot_line, slot_tier=slot_tier)
        argv = shared_argv(root) + list(extra_argv)
        assert cake_targets(root, argv) == _expected_buckets(expected)


class TestTheDiscoveryGateHasTwoSides:
    """``if args.auto and not any([filename, static, dynamic, tests])`` is
    spelled identically in cake.py and findtargets.py, so WHERE a library
    slot arrives decides whether discovery runs at all.

    A slot the pre-parse tiers already see makes the gate false: discovery
    never runs and the library is the entire target set. A slot reachable
    only through re-anchoring arrives after discovery has run, so the library
    is ADDED to a populated executable set. Conflating the two sides would be
    invisible to any test that looked at one of them alone, which is why the
    contrast is asserted here rather than left implicit across two rows.
    """

    def test_a_post_gate_slot_adds_and_a_pre_gate_slot_suppresses(self, tmp_path, capsys):
        slot = "static = lib/orphan.cpp"
        post_root = make_fixture(tmp_path, slot_line=slot, slot_tier="subproject", name="postgate")
        pre_root = make_fixture(tmp_path, slot_line=slot, slot_tier="gitroot", name="pregate")

        post_argv, pre_argv = shared_argv(post_root), shared_argv(pre_root)
        post_built, pre_built = cake_targets(post_root, post_argv), cake_targets(pre_root, pre_argv)

        assert post_built[STATIC] == frozenset({"lib/orphan.cpp"})
        assert pre_built[STATIC] == frozenset({"lib/orphan.cpp"})
        assert post_built[EXES], "post-gate arrival must leave the discovered executables in place"
        assert not pre_built[EXES], "pre-gate arrival must suppress executable discovery"

        assert findtargets_targets(post_root, post_argv, capsys) == post_built
        assert findtargets_targets(pre_root, pre_argv, capsys) == pre_built


class TestTheSlotLineIsWhatCausesTheLibrary:
    """A causal-delta pin, NOT a both-states control: the with-line arm minus
    the without-line arm must equal the library, in each tool independently.
    The findtargets with-line leg is the parity assertion and is red until the
    reporting fix lands; the without-line legs are the embedded control that
    stops the pin passing against a fixture where something else entirely put
    the source there, and they are green in both states."""

    @pytest.mark.parametrize("slot", ["static", "dynamic"])
    def test_the_library_appears_in_both_tools_only_when_the_conf_line_is_present(self, tmp_path, capsys, slot):
        bucket = STATIC if slot == "static" else DYNAMIC
        with_slot = make_fixture(tmp_path, slot_line=f"{slot} = lib/orphan.cpp", name="withslot")
        without = make_fixture(tmp_path, name="withoutslot")

        assert cake_targets(with_slot, shared_argv(with_slot))[bucket] == frozenset({"lib/orphan.cpp"})
        assert cake_targets(without, shared_argv(without))[bucket] == frozenset()
        assert findtargets_targets(with_slot, shared_argv(with_slot), capsys)[bucket] == frozenset({"lib/orphan.cpp"})
        assert findtargets_targets(without, shared_argv(without), capsys)[bucket] == frozenset()


# ---------------------------------------------------------------------------
# The consumer: scripts/ct-build
# ---------------------------------------------------------------------------


def _args_style_tokens(root, argv, capsys):
    _drain(capsys)
    with uth.DirectoryContext(str(root)), uth.ParserContext():
        try:
            returncode = compiletools.findtargets.main(argv + ["--style=args"])
        except SystemExit as exit_request:
            returncode = exit_request.code
    captured = capsys.readouterr()
    assert returncode == 0, f"ct-findtargets exited {returncode}: {captured.err.strip()}"
    return shlex.split(captured.out)


class TestTheCtBuildRoundTrip:
    """scripts/ct-build is the reason the two tools have to agree at all:

        all=$(ct-findtargets --style=args $@)
        cmd="ct-create-makefile ${all} $@"

    So the pin is the pipeline itself -- real ct-findtargets stdout, split
    the way the shell splits it, into real ct-create-makefile -- and the
    makefile that falls out must describe the same targets ct-cake builds.
    A tool-to-tool set comparison alone would still pass if ``--style=args``
    rendered the agreed set in a form ct-create-makefile cannot consume.

    Measured caveat, worth knowing before reading a green result as proof of
    the reporting fix: on a subproject-conf tree the pipeline is ALREADY
    correct without it. ct-findtargets drops the library slot from its args
    output, but ct-create-makefile is handed the discovered ``app/main.cpp``
    positionally, which pulls ``app/ct.conf`` in through the
    cwd+target-subprojects tier, and it builds the library from the conf on
    its own. So these rows measure that the fix does not BREAK a pipeline
    that already worked -- the reporting gap itself is measured by the set
    equality above, where nothing recovers it.
    """

    def test_the_script_still_feeds_findtargets_output_to_create_makefile(self, pytestconfig):
        """The premise of the test below. If ct-build stops composing the two
        tools this way, the round trip is pinning a pipeline nobody runs."""
        script = Path(pytestconfig.rootpath) / "scripts" / "ct-build"
        text = script.read_text()
        assert "ct-findtargets --style=args" in text
        assert "ct-create-makefile ${all}" in text

    @pytest.mark.parametrize(
        "slot_line,bucket,library",
        [
            (None, STATIC, frozenset()),
            ("static = lib/orphan.cpp", STATIC, frozenset({"lib/orphan.cpp"})),
            ("dynamic = lib/orphan.cpp", DYNAMIC, frozenset({"lib/orphan.cpp"})),
        ],
        ids=["no-library", "static", "dynamic"],
    )
    def test_the_pipeline_reproduces_the_cake_target_set(self, tmp_path, capsys, slot_line, bucket, library):
        root = make_fixture(tmp_path, slot_line=slot_line)
        argv = shared_argv(root)
        expected = cake_targets(root, argv)
        assert expected[bucket] == library, "the fixture did not produce the row it claims"

        tokens = _args_style_tokens(root, argv, capsys)
        makefile = Path(root) / "Makefile.roundtrip"
        with uth.DirectoryContext(str(root)), uth.ParserContext():
            returncode = compiletools.makefile_backend.main(tokens + argv + ["--makefilename=" + str(makefile)])
        assert returncode == 0, f"ct-create-makefile exited {returncode} on {tokens}"
        assert parse_cake_makefile(root, makefile.read_text()) == expected


class TestThePathShapesEachToolEmits:
    """The set comparisons normalise paths, so a spelling change is invisible
    to them by design. It is not invisible to a shell consumer, so the
    spellings are recorded here as their own fact.

    Currently ct-findtargets emits DISCOVERED targets absolute and
    CONF-SOURCED targets in the conf file's own relative spelling, in the
    same listing. That mix is what is true today, not an endorsement; this
    test is the place a deliberate change to it gets noticed and re-agreed.
    """

    def test_discovered_targets_are_absolute_and_conf_targets_keep_their_spelling(self, tmp_path, capsys):
        root = make_fixture(tmp_path, slot_line="tests = lib/orphan.cpp")
        tokens = _args_style_tokens(root, shared_argv(root), capsys)

        discovered = [token for token in tokens if token.endswith("app/main.cpp")]
        conf_sourced = [token for token in tokens if token.endswith("lib/orphan.cpp")]
        assert discovered and conf_sourced, f"fixture did not produce both shapes: {tokens}"
        assert all(os.path.isabs(token) for token in discovered)
        assert conf_sourced == ["lib/orphan.cpp"]


class TestKnownDivergenceOnAContradictoryTree:
    """One tree where the tools deliberately do NOT agree, recorded so the
    difference stays a decision rather than becoming an accident.

    Same-tier conflicting subproject confs are a hard error for ct-cake --
    it cannot pick a flag set -- but ct-findtargets is the tool a user
    reaches for to understand a confusing tree, so it reports what discovery
    found and warns that the answer may be incomplete.
    """

    def test_findtargets_reports_and_warns_where_cake_refuses(self, tmp_path, capsys):
        root = tmp_path / "contradictionrepo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        uth.create_temp_ct_conf(str(root))
        for index, std in enumerate(("c++20", "c++23")):
            subproject = root / f"app{index}"
            subproject.mkdir()
            (subproject / "ct.conf").write_text(f"CXXFLAGS = -std={std}\n")
            (subproject / "main.cpp").write_text(f"// {_unique_tag()}\nint main() {{ return {index}; }}\n")
        argv = shared_argv(root)

        with uth.DirectoryContext(str(root)), uth.ParserContext():
            returncode = compiletools.findtargets.main(argv + ["--style=indent"])
        captured = capsys.readouterr()
        assert returncode == 0
        assert "conflicting subproject configs" in captured.err
        assert "may be incomplete" in captured.err
        reported = parse_findtargets_indent(root, captured.out)
        assert reported[EXES] == frozenset({"app0/main.cpp", "app1/main.cpp"})

        with pytest.raises(SystemExit):
            with uth.DirectoryContext(str(root)), uth.ParserContext():
                compiletools.cake.main(
                    argv + ["--backend=make", "--makefilename=" + str(root / "Makefile.x"), "--clean"]
                )
