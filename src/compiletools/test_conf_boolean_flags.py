"""A conf-file boolean must be settable to false.

``utils.add_flag_argument`` registers ``action="store_true"`` plus a
``--no-<name>`` partner in a mutually exclusive group. configargparse
translates a conf-file (or env-var) entry into command-line tokens in
``convert_item_to_command_line_arg``, and for an action that takes no value
a *falsey* entry hits an explicit ``pass`` -- nothing is injected, no
diagnostic, no error. So ``auto = False`` in a ct.conf is silently inert
for every flag registered through that helper, and the only spelling that
works is ``no-auto = True``.

Two auditors independently reached for ``key = False``, concluded from the
unchanged result that the capability was absent, and both were wrong: the
capability exists, its obvious spelling is dead. That is why these tests
use the falsey spellings deliberately, across the whole production flag
inventory rather than one sample -- a test written with ``no-<key> = True``
would pass over the top of the defect.

The parser-level cases run through the production parser class
(``_ComposingArgumentParser`` + ``_AccumulatingConfigFileParser``, the pair
``apptools.create_parser`` builds) so a fix anywhere in the conf-to-argv
translation is exercised, not just a fix in ``utils``. The end-to-end cases
drive ``ct-findtargets`` and assert on which bucket a target lands in.

Three properties the flags have today must survive any fix, and are pinned
here so a fix that trades them away fails: the ``--no-<name>`` partner, the
mutually exclusive group, and a value-less command line (``--timing
main.cpp`` must leave ``main.cpp`` a positional -- a ``nargs="?"`` shape
would swallow it).
"""

import ast
import os
import pathlib
import subprocess

import configargparse
import pytest

import compiletools.apptools
import compiletools.apptools_argparse
import compiletools.findtargets
import compiletools.testhelper as uth
import compiletools.utils

# configargparse's own vocabulary for a conf-file boolean
# (``convert_item_to_command_line_arg``); anything else is a parse error, so
# these five are the whole falsey surface a user can write.
_FALSEY_CONF_SPELLINGS = ("False", "false", "0", "no", "off")

# Distinctive prefix so an ambient env var (the production parsers use
# ``auto_env_var_prefix=""``, which would pick up a bare ``AUTO`` or
# ``STATUS`` from the developer's shell) cannot reach the probe parser.
_PROBE_ENV_PREFIX = "CT_CONF_BOOLEAN_PROBE_"


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for dirpath, _, filenames in os.walk(src_dir):
        for fname in sorted(filenames):
            if fname.endswith(".py") and not fname.startswith("test_"):
                yield os.path.join(dirpath, fname)


def _registered_flag_names(helper_name):
    """Names passed to *helper_name* by every production call site.

    AST rather than a hand-maintained list: the inventory is the point of
    the matrix, and a flag added later must join it without anyone
    remembering to. ``utils.py``'s own delegation from
    ``add_flag_argument`` to ``add_boolean_argument`` passes a variable
    rather than a literal, so it drops out naturally.
    """
    names = set()
    for path in _production_python_files():
        tree = ast.parse(pathlib.Path(path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called != helper_name:
                continue
            name_arg = node.args[1] if len(node.args) > 1 else None
            if name_arg is None:
                name_arg = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
            if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
                names.add(name_arg.value)
    return frozenset(names)


# The defective helper: store_true, no value conversion.
_FLAG_ARGUMENT_NAMES = _registered_flag_names("add_flag_argument")

# The correct helper: nargs="?" + to_bool, which configargparse translates
# as a value-taking action, so a falsey conf entry reaches to_bool. Present
# here as the control arm -- the matrix has to show the falsey spelling
# working where it is supposed to as well as failing where it is not.
_BOOLEAN_ARGUMENT_NAMES = _registered_flag_names("add_boolean_argument") - _FLAG_ARGUMENT_NAMES

# Pinned so a newly registered flag joins the matrix loudly instead of
# silently going untested. Update alongside the registration.
_EXPECTED_FLAG_ARGUMENT_NAMES = frozenset(
    {
        "allow-fake-git",
        "auto",
        "compilation-database",
        "disable-exes",
        "disable-tests",
        "filenametestmatch",
        "force-flat-exe-layout",
        "force-mmap",
        "git-root",
        "merge",
        "no-fetch",
        "otel-export",
        "otel-metrics-as-spans",
        "scope-diagnostics",
        "separate-flags-CPP-CXX",
        "serialise-tests",
        "shorten",
        "shuffle",
        "status",
        "suppress-fd-warnings",
        "suppress-filesystem-warnings",
        "timing",
        "update",
        "use-mmap",
    }
)

_EXPECTED_BOOLEAN_ARGUMENT_NAMES = frozenset(
    {
        "all",
        "allow-magic-source-in-header",
        "configname",
        "file-locking",
        "preprocess",
        "repoonly",
        "use-mtime",
    }
)


def _dest_for(name):
    return name.replace("-", "_")


def _probe_parser(conf_path, name, default, helper):
    """A parser carrying exactly one boolean flag plus a positional.

    Mirrors ``apptools.create_parser``'s config-aware kwargs so the
    conf-file reading path is the production one, but with a single
    conf file and an isolating env-var prefix.
    """
    parser = compiletools.apptools._ComposingArgumentParser(
        description="conf boolean probe",
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        auto_env_var_prefix=_PROBE_ENV_PREFIX,
        default_config_files=[str(conf_path)],
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
        conflict_handler="resolve",
        config_file_parser_class=compiletools.apptools_argparse._AccumulatingConfigFileParser,
    )
    helper(parser, name, dest=_dest_for(name), default=default)
    parser.add_argument("filename", nargs="*")
    return parser


def _parse_with_conf(tmp_path, name, conf_body, default, helper, argv=()):
    conf = tmp_path / "ct.conf"
    conf.write_text(conf_body)
    parser = _probe_parser(conf, name, default=default, helper=helper)
    args, unknown = parser.parse_known_args(list(argv))
    return getattr(args, _dest_for(name)), unknown


class TestTheFlagInventory:
    def test_add_flag_argument_registrations_are_the_matrix_the_tests_cover(self):
        assert _FLAG_ARGUMENT_NAMES == _EXPECTED_FLAG_ARGUMENT_NAMES

    def test_add_boolean_argument_registrations_are_the_control_arm(self):
        assert _BOOLEAN_ARGUMENT_NAMES == _EXPECTED_BOOLEAN_ARGUMENT_NAMES

    def test_every_flag_argument_flag_has_a_no_partner_registered(self):
        """The partner is what makes a falsey conf value expressible at all;
        it is also the surface a fix is most likely to trade away."""
        missing = []
        for name in sorted(_FLAG_ARGUMENT_NAMES):
            parser = _probe_parser(
                pathlib.Path(os.devnull), name, default=False, helper=compiletools.utils.add_flag_argument
            )
            if f"--no-{name}" not in parser._option_string_actions:
                missing.append(name)
        assert missing == []


class TestFalseyConfValues:
    """The defect. Every one of these fails before the fix."""

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    @pytest.mark.parametrize("spelling", _FALSEY_CONF_SPELLINGS)
    def test_a_falsey_conf_value_turns_a_default_on_flag_off(self, tmp_path, name, spelling):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = {spelling}\n",
            default=True,
            helper=compiletools.utils.add_flag_argument,
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("name", sorted(_EXPECTED_BOOLEAN_ARGUMENT_NAMES))
    @pytest.mark.parametrize("spelling", _FALSEY_CONF_SPELLINGS)
    def test_the_value_conversion_helper_already_honours_a_falsey_conf_value(self, tmp_path, name, spelling):
        """Control arm: same conf text, same parser, the other helper. If
        this ever fails alongside the case above, the defect is somewhere
        shared rather than in the store_true shape."""
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = {spelling}\n",
            default=True,
            helper=compiletools.utils.add_boolean_argument,
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("spelling", _FALSEY_CONF_SPELLINGS)
    def test_a_falsey_env_var_turns_a_default_on_flag_off(self, tmp_path, monkeypatch, spelling):
        """Same translation function, same explicit ``pass``: env vars are
        inert for these flags for exactly the reason conf files are."""
        monkeypatch.setenv(f"{_PROBE_ENV_PREFIX}TIMING", spelling)
        value, unknown = _parse_with_conf(
            tmp_path, "timing", "", default=True, helper=compiletools.utils.add_flag_argument
        )
        assert unknown == []
        assert value is False


class TestSpellingsThatAlreadyWork:
    """Guard rail on the fix, not a demonstration of the defect: these pass
    today and must keep passing."""

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_no_key_conf_spelling_still_turns_the_flag_off(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"no-{name} = True\n",
            default=True,
            helper=compiletools.utils.add_flag_argument,
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_a_truthy_conf_value_still_turns_the_flag_on(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_command_line_no_form_still_turns_the_flag_off(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            "",
            default=True,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--no-{name}"],
        )
        assert unknown == []
        assert value is False


class TestValuesOutsideTheBooleanVocabulary:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_an_unrecognised_conf_value_still_reports_the_configargparse_error(self, tmp_path, name, capsys):
        """A fix that gives falsey values a meaning must not quietly give
        every other value one too. configargparse's eight-word vocabulary
        stays the arbiter, and a word outside it keeps exiting 2 with
        configargparse's own message rather than being read as false.
        """
        conf = tmp_path / "ct.conf"
        conf.write_text(f"{name} = maybe\n")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([])
        assert excinfo.value.code == 2
        assert f"Unexpected value for {name}: 'maybe'" in capsys.readouterr().err


class TestTheCommandLineOverridesTheConfFile:
    """The second defect, in the same translation function.

    Making a falsey conf value live is not enough on its own: stock
    configargparse skips a conf entry only when the SAME action's option
    string was typed, so the injected partner option lands next to the typed
    one and the mutually exclusive group exits 2. Measured on the production
    parser pair, ``no-auto = True`` plus ``--auto`` exits 2 today, and a
    falsey-only fix would newly break ``auto = False`` plus ``--auto`` --
    turning a silent True into a crash for exactly the users who wrote the
    inert spelling. So both halves are pinned here.
    """

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_plain_form_on_the_command_line_beats_the_no_key_conf_entry(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"no-{name} = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--{name}"],
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_no_form_on_the_command_line_beats_the_truthy_conf_entry(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--no-{name}"],
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_plain_form_on_the_command_line_beats_the_falsey_conf_entry(self, tmp_path, name):
        """The row that makes the two halves inseparable: this is a silent
        True today, and a falsey-only fix would make it exit 2."""
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = False\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--{name}"],
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_an_abbreviated_no_form_beats_the_truthy_conf_entry(self, tmp_path, name):
        """``--no-au`` is not a registered option string; argparse resolves
        it to ``--no-auto`` by unambiguous prefix. A suppression check that
        compares option strings literally -- which is what
        ``configargparse.already_on_command_line`` does -- misses it and the
        conf entry injects the conflicting partner anyway. Truncating by one
        character keeps the prefix unambiguous for every name in the matrix.
        """
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--no-{name}"[:-1]],
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_an_abbreviated_plain_form_beats_the_falsey_conf_entry(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = False\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--{name}"[:-1]],
        )
        assert unknown == []
        assert value is True


class TestTheDoubleNegativeConfKey:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_a_falsey_value_on_the_no_key_turns_the_flag_on(self, tmp_path, name):
        """The one row where the fix changes an outcome rather than
        restoring one. ``no-auto = False`` is inert today, so the default
        stands; under the symmetric rule -- a truthy value applies the option
        its key names, a falsey value applies the opposite -- it reads as
        written and turns the flag on. Named separately from the falsey
        sweep because it is a semantic choice, not a bug fix.
        """
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"no-{name} = False\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[],
        )
        assert unknown == []
        assert value is True


class TestCommandLineShapeIsUnchanged:
    """A fix that reaches for ``nargs="?"`` + ``to_bool`` would fix the conf
    file and break the command line: an optional-value flag swallows the
    following positional. Measured on this probe parser --
    ``["--timing", "main.cpp"]`` yields ``timing=True filename=['main.cpp']``
    with the store_true shape and exits 2 with ``invalid to_bool value:
    'main.cpp'`` with the conversion shape. Pin the working half."""

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_flag_does_not_consume_the_following_positional(self, tmp_path, name):
        conf = tmp_path / "ct.conf"
        conf.write_text("")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        args, unknown = parser.parse_known_args([f"--{name}", "main.cpp"])
        assert unknown == []
        assert getattr(args, _dest_for(name)) is True
        assert args.filename == ["main.cpp"]

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_help_renders_the_flag_with_no_value_placeholder(self, tmp_path, name):
        """``--timing`` not ``--timing []``. The bracket is what the
        conversion shape adds, in both the usage line and the option list.
        Whitespace is collapsed first: argparse wraps the usage line at the
        terminal width, so the longer flag names straddle a newline."""
        conf = tmp_path / "ct.conf"
        conf.write_text("")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        usage = " ".join(parser.format_usage().split())
        assert f"[--{name} | --no-{name}]" in usage

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_both_forms_on_one_command_line_is_still_an_error(self, tmp_path, name):
        """The mutually exclusive group. Named separately from the partner
        test because a fix could keep both options and lose the group."""
        conf = tmp_path / "ct.conf"
        conf.write_text("")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([f"--{name}", f"--no-{name}"])
        assert excinfo.value.code == 2


@pytest.fixture(autouse=True)
def _reset_parser_state():
    uth.reset()
    yield
    uth.reset()


@pytest.fixture
def filenametestmatch_repo(tmp_path):
    """A repo whose single source is named ``test...`` and holds a ``main``.

    That is the one file shape ``--filenametestmatch`` moves: with the flag
    on (the default) it is classified a test despite the exe marker; with it
    off it is an executable. So the bucket the target lands in is a direct
    readout of the flag's value, end to end.
    """
    root = tmp_path / "fntm"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ct.conf").write_text("exemarkers = [main]\ntestmarkers = unit_test.hpp\n")
    (root / "testthing.cpp").write_text("int main() { return 0; }\n")
    return root


def _findtargets_args_output(repo, capsys, extra_conf=None, argv=()):
    if extra_conf is not None:
        conf = repo / "ct.conf"
        conf.write_text(conf.read_text() + extra_conf)
    with uth.DirectoryContext(str(repo)):
        with uth.ParserContext():
            rc = compiletools.findtargets.main(["--style=args", *argv])
    assert rc == 0
    return capsys.readouterr().out.split()


class TestFindTargetsEndToEnd:
    """The parser-level cases prove the translation is wrong; these prove a
    user-visible consequence -- ct-findtargets reports the target in the
    wrong bucket, and ``--style=args`` feeds that bucketing straight to
    ct-create-makefile via scripts/ct-build."""

    def test_the_default_puts_a_test_prefixed_main_in_the_test_bucket(self, filenametestmatch_repo, capsys):
        tokens = _findtargets_args_output(filenametestmatch_repo, capsys)
        assert "--tests" in tokens
        assert tokens[-1].endswith("testthing.cpp")

    @pytest.mark.parametrize("spelling", _FALSEY_CONF_SPELLINGS)
    def test_a_falsey_conf_value_moves_the_target_to_the_executable_bucket(
        self, filenametestmatch_repo, capsys, spelling
    ):
        tokens = _findtargets_args_output(
            filenametestmatch_repo, capsys, extra_conf=f"filenametestmatch = {spelling}\n"
        )
        assert "--tests" not in tokens
        assert tokens[-1].endswith("testthing.cpp")

    def test_the_no_key_conf_spelling_moves_the_target_to_the_executable_bucket(self, filenametestmatch_repo, capsys):
        """Control: the working spelling, same repo, same assertion. Its
        passing is what makes the falsey cases a translation defect rather
        than a claim that the flag does nothing."""
        tokens = _findtargets_args_output(filenametestmatch_repo, capsys, extra_conf="no-filenametestmatch = True\n")
        assert "--tests" not in tokens
        assert tokens[-1].endswith("testthing.cpp")

    def test_the_command_line_no_form_moves_the_target_to_the_executable_bucket(self, filenametestmatch_repo, capsys):
        tokens = _findtargets_args_output(filenametestmatch_repo, capsys, argv=["--no-filenametestmatch"])
        assert "--tests" not in tokens
        assert tokens[-1].endswith("testthing.cpp")
