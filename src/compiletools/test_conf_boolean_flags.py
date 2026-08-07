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
import compiletools.cake
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


def _registered_flag_names(helper_name, value_converting=None):
    """Names passed to *helper_name* by every production call site.

    AST rather than a hand-maintained list: the inventory is the point of
    the matrix, and a flag added later must join it without anyone
    remembering to. ``utils.py``'s own delegation from
    ``add_flag_argument`` to ``add_boolean_argument`` passes a variable
    rather than a literal, so it drops out naturally.

    *value_converting* filters ``add_boolean_argument`` call sites on the
    ``allow_value_conversion`` keyword. Filtering on the keyword rather than
    subtracting the ``add_flag_argument`` inventory matters because a name
    can be registered BOTH ways in different tools -- ``shorten`` is a
    value-converting boolean in ``listvariants`` and a plain flag in
    ``git_utils`` -- so a set difference silently drops it from one arm.
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
            if value_converting is not None:
                converts = next((kw.value for kw in node.keywords if kw.arg == "allow_value_conversion"), None)
                converts = True if converts is None else getattr(converts, "value", None)
                if converts is not value_converting:
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
# working where it is supposed to as well as failing where it is not. Only
# the POSITIVE half converts values though: --no-<name> is a store_false in
# both arms, so the double-negative conf key is a flag-action translation
# and lands on the same defect (TestTheValueConvertingHelpersDoubleNegative).
_VALUE_CONVERTING_ARGUMENT_NAMES = _registered_flag_names("add_boolean_argument", value_converting=True)

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

# The names those value-converting call sites register.
_EXPECTED_BOOLEAN_ARGUMENT_NAMES = frozenset(
    {
        "all",
        "allow-magic-source-in-header",
        "configname",
        "file-locking",
        "preprocess",
        "repoonly",
        "shorten",
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
        assert _VALUE_CONVERTING_ARGUMENT_NAMES == _EXPECTED_BOOLEAN_ARGUMENT_NAMES

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

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_diagnosis_does_not_depend_on_which_flag_the_user_typed(self, tmp_path, name, capsys):
        """The value is validated BEFORE the command line is consulted. A
        conf typo whose diagnosis appears or disappears according to an
        unrelated flag on the command line is the silent-inert class this
        file exists to remove, so the suppression must not reach a value
        configargparse would have rejected."""
        conf = tmp_path / "ct.conf"
        conf.write_text(f"no-{name} = maybe\n")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([f"--{name}"])
        assert excinfo.value.code == 2
        assert f"Unexpected value for no-{name}: 'maybe'" in capsys.readouterr().err

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_stocks_own_same_action_silence_is_inherited_not_introduced(self, tmp_path, name):
        """The asymmetry that remains, recorded rather than fixed. When the
        command line names the SAME key's action, configargparse's own
        ``already_on_command_line`` check drops the conf entry before this
        parser's translation is called at all, so a malformed value there is
        silent on every revision. Out of reach from inside the translation
        function; pinned so a later fix is a deliberate one."""
        conf = tmp_path / "ct.conf"
        conf.write_text(f"{name} = maybe\n")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        args, _ = parser.parse_known_args([f"--{name}"])
        assert getattr(args, _dest_for(name)) is True

    @pytest.mark.parametrize("padded", [" false ", " true ", "\tno", "yes\n"])
    def test_a_whitespace_padded_env_value_is_outside_the_vocabulary(self, tmp_path, monkeypatch, padded):
        """configargparse compares ``value.lower()`` with no strip, so a
        padded word is not one of the eight. Stripping here would have
        widened the vocabulary on the falsey side only -- padded ``false``
        accepted while padded ``true`` still errored -- so the two sides now
        agree by not stripping. Env vars only; the conf parser strips its own
        values before this function sees them."""
        monkeypatch.setenv(_env_var_for("auto"), padded)
        parser = _probe_parser(
            tmp_path / "absent.conf", "auto", default=True, helper=compiletools.utils.add_flag_argument
        )
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("word,expected", [("false", False), ("true", True), ("0", False), ("1", True)])
    def test_the_unpadded_word_is_still_honoured(self, tmp_path, monkeypatch, word, expected):
        """Anti-vacuity for the case above: the vocabulary itself still
        works, so what the padded cases pin is the padding."""
        monkeypatch.setenv(_env_var_for("auto"), word)
        parser = _probe_parser(
            tmp_path / "absent.conf", "auto", default=not expected, helper=compiletools.utils.add_flag_argument
        )
        args, _ = parser.parse_known_args([])
        assert args.auto is expected


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


def _env_var_for(name):
    return _PROBE_ENV_PREFIX + name.replace("-", "_").upper()


class TestTheEnvironmentOverridesTheConfFile:
    """configargparse's documented precedence is command line, then env var,
    then conf file. Making a falsey value live puts these flags back under
    it, and the same mutually-exclusive-group hazard applies one rung down:
    an env var emitting the partner option next to a conf entry emitting the
    plain one exits 2 unless the conf entry is suppressed. Measured against
    the parent revision, ``auto = True`` in a conf plus ``AUTO=0`` in the
    environment was a silent True; without this half it becomes exit 2."""

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_a_falsey_env_var_beats_a_truthy_conf_entry(self, tmp_path, monkeypatch, name):
        monkeypatch.setenv(_env_var_for(name), "0")
        value, unknown = _parse_with_conf(
            tmp_path, name, f"{name} = True\n", default=False, helper=compiletools.utils.add_flag_argument
        )
        assert unknown == []
        assert value is False

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_a_truthy_env_var_beats_a_falsey_conf_entry(self, tmp_path, monkeypatch, name):
        monkeypatch.setenv(_env_var_for(name), "1")
        value, unknown = _parse_with_conf(
            tmp_path, name, f"{name} = False\n", default=False, helper=compiletools.utils.add_flag_argument
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_a_truthy_env_var_beats_the_no_key_conf_entry(self, tmp_path, monkeypatch, name):
        monkeypatch.setenv(_env_var_for(name), "1")
        value, unknown = _parse_with_conf(
            tmp_path, name, f"no-{name} = True\n", default=False, helper=compiletools.utils.add_flag_argument
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_the_command_line_beats_both_the_env_var_and_the_conf_entry(self, tmp_path, monkeypatch, name):
        monkeypatch.setenv(_env_var_for(name), "0")
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"{name} = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=[f"--{name}"],
        )
        assert unknown == []
        assert value is True


class TestTheConfigFileContentsPath:
    """The one path where the env half does not hold, pinned as unreachable.

    ``convert_item_to_command_line_arg`` learns which options the caller
    already named from an argv stash that ``_open_config_files`` widens with
    configargparse's env-var tokens. configargparse skips
    ``_open_config_files`` entirely when a caller passes
    ``config_file_contents``, so on that path the stash keeps the un-widened
    argv, the conf entry is not suppressed, and a contradicting env var exits
    2 rather than winning.

    Reading ``os.environ`` inside the suppression instead is refuted by
    measurement rather than by argument: the env-sourced item would match its
    own variable and suppress its own token, so ``AUTO=1`` with no conf file
    at all resolves to False. The degradation is therefore kept, and these
    two cases hold it to being unreachable and to being loud.
    """

    def test_no_compiletools_caller_supplies_config_file_contents(self):
        """The reachability claim, pinned. ``config_file_contents`` may
        appear only as ``_ComposingArgumentParser.parse_known_args``'
        parameter and its pass-through to super; any call site binding it to
        a real value puts the degradation below into production."""
        offenders = []
        for path in _production_python_files():
            tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func.attr if isinstance(node.func, ast.Attribute) else None
                if called in ("parse_args", "parse_known_args") and len(node.args) >= 3:
                    # config_file_contents is the third positional.
                    offenders.append(f"{os.path.basename(path)}:{node.lineno} (positional)")
                for kw in node.keywords:
                    if kw.arg != "config_file_contents":
                        continue
                    # The two sanctioned bindings: the pass-through to super
                    # forwarding this parser's own parameter, and an explicit
                    # None. Anything else supplies real contents.
                    if isinstance(kw.value, ast.Name) and kw.value.id == "config_file_contents":
                        continue
                    if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                        continue
                    offenders.append(f"{os.path.basename(path)}:{kw.value.lineno}")
        assert offenders == [], (
            "config_file_contents is bound to a real value; the env-var half of the flag-pair "
            f"precedence does not hold on that path: {offenders}"
        )

    def test_a_contradicting_env_var_is_loud_rather_than_silently_wrong(self, monkeypatch):
        """The degradation itself, so a future caller inherits a named test
        instead of a surprise. Exit 2 from the mutually exclusive group, not
        a silently wrong boolean -- which is why it is tolerable while the
        case above keeps it unreachable."""
        monkeypatch.setenv(_env_var_for("auto"), "0")
        parser = compiletools.apptools._ComposingArgumentParser(
            auto_env_var_prefix=_PROBE_ENV_PREFIX,
            add_help=False,
            conflict_handler="resolve",
            config_file_parser_class=compiletools.apptools_argparse._AccumulatingConfigFileParser,
        )
        compiletools.utils.add_flag_argument(parser, "auto", dest="auto", default=False)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([], config_file_contents="auto = True\n")
        assert excinfo.value.code == 2

    def test_the_falsey_conf_fix_itself_still_holds_on_that_path(self, monkeypatch):
        """Anti-vacuity for the two above: only the env half degrades. The
        defect this whole file exists for -- a falsey conf value being inert
        -- is fixed on the config_file_contents path too, so the case above
        is a narrow precedence gap and not the fix failing wholesale."""
        monkeypatch.delenv(_env_var_for("auto"), raising=False)
        parser = compiletools.apptools._ComposingArgumentParser(
            auto_env_var_prefix=_PROBE_ENV_PREFIX,
            add_help=False,
            conflict_handler="resolve",
            config_file_parser_class=compiletools.apptools_argparse._AccumulatingConfigFileParser,
        )
        compiletools.utils.add_flag_argument(parser, "auto", dest="auto", default=True)
        args, unknown = parser.parse_known_args([], config_file_contents="auto = False\n")
        assert unknown == []
        assert args.auto is False


class TestConflictsLeftOutOfScope:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_two_contradicting_conf_keys_still_exit_two(self, tmp_path, name):
        """The documented boundary. Both keys come from the conf hierarchy,
        so neither can suppress the other from inside a translation function
        handed one item at a time. Pinned so that a later change to that
        behaviour is a deliberate one that updates the docstring with it."""
        conf = tmp_path / "ct.conf"
        conf.write_text(f"{name} = True\nno-{name} = True\n")
        parser = _probe_parser(conf, name, default=False, helper=compiletools.utils.add_flag_argument)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_two_env_vars_naming_opposite_halves_still_exit_two(self, tmp_path, monkeypatch, name):
        """The second boundary, structurally out of reach for a different
        reason than the conf-vs-conf one. configargparse converts every env
        item BEFORE splicing any of them into the argv, so at the moment
        either call runs the other's token does not exist yet -- widening the
        stash cannot help, because there is nothing there to see."""
        monkeypatch.setenv(_env_var_for(name), "1")
        monkeypatch.setenv(_env_var_for(f"no-{name}"), "1")
        parser = _probe_parser(
            tmp_path / "absent.conf", name, default=False, helper=compiletools.utils.add_flag_argument
        )
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_known_args([])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("name", sorted(_EXPECTED_FLAG_ARGUMENT_NAMES))
    def test_either_env_var_alone_is_still_honoured(self, tmp_path, monkeypatch, name):
        """Anti-vacuity for the case above: the mutex fires because BOTH env
        vars are set, not because env vars stopped working."""
        monkeypatch.delenv(_env_var_for(f"no-{name}"), raising=False)
        monkeypatch.setenv(_env_var_for(name), "1")
        parser = _probe_parser(
            tmp_path / "absent.conf", name, default=False, helper=compiletools.utils.add_flag_argument
        )
        args, _ = parser.parse_known_args([])
        assert getattr(args, _dest_for(name)) is True

        monkeypatch.delenv(_env_var_for(name), raising=False)
        monkeypatch.setenv(_env_var_for(f"no-{name}"), "1")
        parser = _probe_parser(
            tmp_path / "absent.conf", name, default=True, helper=compiletools.utils.add_flag_argument
        )
        args, _ = parser.parse_known_args([])
        assert getattr(args, _dest_for(name)) is False


class TestTheDoubleDashTerminator:
    """A post-terminator token spelling either half of a pair suppresses the
    conf entry, because the stash is scanned without honouring ``--``.

    A behaviour change rather than an inherited wart: stock matched only the
    same action's literal option string, so a positional could never reach
    the partner. Reaching it needs a source file literally named ``--auto``,
    which is why this is pinned rather than fixed -- a later fix should flip
    these cells deliberately.
    """

    def test_a_post_terminator_token_suppresses_the_conf_entry(self, tmp_path):
        value, _ = _parse_with_conf(
            tmp_path,
            "auto",
            "no-auto = True\n",
            default=True,
            helper=compiletools.utils.add_flag_argument,
            argv=["--", "--auto"],
        )
        assert value is True

    def test_a_post_terminator_abbreviation_suppresses_it_too(self, tmp_path):
        value, _ = _parse_with_conf(
            tmp_path,
            "auto",
            "auto = True\n",
            default=False,
            helper=compiletools.utils.add_flag_argument,
            argv=["--", "--no-au"],
        )
        assert value is False

    def test_an_ordinary_post_terminator_positional_leaves_the_conf_alone(self, tmp_path):
        """Anti-vacuity: the suppression above needs a token that SPELLS a
        half of the pair, not merely a token after the terminator."""
        value, _ = _parse_with_conf(
            tmp_path,
            "auto",
            "no-auto = True\n",
            default=True,
            helper=compiletools.utils.add_flag_argument,
            argv=["--", "main.cpp"],
        )
        assert value is False


class TestTheAmbiguousAbbreviationBail:
    """``_command_line_names_any`` counts a prefix as typed only when it
    resolves to exactly one action. The two cases that bail are asserted in
    prose there and nothing else pins them, so they are named here.

    Both are correct-by-inspection rather than observable today: whenever a
    prefix is ambiguous argparse exits 2 regardless of what the conf did, and
    no compiletools parser sets ``allow_abbrev=False``. Pinned at the
    function rather than through a parse so the bail itself is the subject.
    """

    def test_an_ambiguous_prefix_does_not_count_as_naming_either_half(self, tmp_path):
        parser = _probe_parser(
            tmp_path / "absent.conf", "auto", default=False, helper=compiletools.utils.add_flag_argument
        )
        compiletools.utils.add_flag_argument(parser, "august", dest="august", default=False)
        pair = (parser._option_string_actions["--auto"], parser._option_string_actions["--no-auto"])
        assert parser._command_line_names_any(["--au"], pair) is False
        assert parser._command_line_names_any(["--aut"], pair) is True

    def test_a_prefix_of_an_unrelated_action_does_not_count(self, tmp_path):
        parser = _probe_parser(
            tmp_path / "absent.conf", "auto", default=False, helper=compiletools.utils.add_flag_argument
        )
        compiletools.utils.add_flag_argument(parser, "august", dest="august", default=False)
        pair = (parser._option_string_actions["--auto"], parser._option_string_actions["--no-auto"])
        assert parser._command_line_names_any(["--augu"], pair) is False

    def test_abbreviations_are_ignored_when_the_parser_disables_them(self, tmp_path):
        parser = _probe_parser(
            tmp_path / "absent.conf", "auto", default=False, helper=compiletools.utils.add_flag_argument
        )
        pair = (parser._option_string_actions["--auto"], parser._option_string_actions["--no-auto"])
        assert parser._command_line_names_any(["--aut"], pair) is True
        parser.allow_abbrev = False
        assert parser._command_line_names_any(["--aut"], pair) is False
        assert parser._command_line_names_any(["--auto"], pair) is True


class TestTheValueConvertingHelpersDoubleNegative:
    """``utils.add_boolean_argument``'s value-converting arm registers a
    ``nargs="?"`` positive half and a ``store_false`` ``--no-<name>``, so
    only the negative key is a flag action and ``no-<name> = False`` was
    inert -- the charter defect in the double-negative spelling.

    The partner finder types the CANDIDATE by its ``const`` alone, which
    reaches the pair. Emitting a bare ``--<name>`` for it is safe because
    configargparse appends conf and env tokens after the command line, so
    the optional value slot has no user positional to swallow -- the
    ``test_the_emitted_partner_does_not_consume_a_positional`` cell is what
    holds that.
    """

    @pytest.mark.parametrize("name", sorted(_VALUE_CONVERTING_ARGUMENT_NAMES))
    def test_a_falsey_value_on_the_no_key_turns_the_flag_on(self, tmp_path, name):
        value, unknown = _parse_with_conf(
            tmp_path,
            name,
            f"no-{name} = False\n",
            default=False,
            helper=compiletools.utils.add_boolean_argument,
            argv=[],
        )
        assert unknown == []
        assert value is True

    @pytest.mark.parametrize("name", sorted(_VALUE_CONVERTING_ARGUMENT_NAMES))
    def test_the_command_line_beats_the_no_key(self, tmp_path, name):
        """The precedence half, which exited 2 for these flags before the
        candidate was typed by its ``const``."""
        value, _ = _parse_with_conf(
            tmp_path,
            name,
            f"no-{name} = True\n",
            default=False,
            helper=compiletools.utils.add_boolean_argument,
            argv=[f"--{name}"],
        )
        assert value is True

    def test_the_emitted_partner_does_not_consume_a_positional(self, tmp_path):
        """The risk the ``nargs="?"`` shape carries, measured rather than
        assumed: a bare ``--use-mtime`` typed BEFORE a positional exits 2
        (``invalid to_bool value: 'main.cpp'``), so the token order the conf
        injection produces is what keeps the emission safe."""
        value, _ = _parse_with_conf(
            tmp_path,
            "use-mtime",
            "no-use-mtime = False\n",
            default=False,
            helper=compiletools.utils.add_boolean_argument,
            argv=["main.cpp"],
        )
        assert value is True

    @pytest.mark.parametrize("name", sorted(_VALUE_CONVERTING_ARGUMENT_NAMES))
    def test_the_positive_key_is_unchanged(self, tmp_path, name):
        """Anti-vacuity: the positive half already worked through stock's
        value-taking translation and must keep doing so untouched."""
        for body, default, expected in (
            (f"{name} = False\n", True, False),
            (f"{name} = True\n", False, True),
        ):
            value, _ = _parse_with_conf(
                tmp_path, name, body, default=default, helper=compiletools.utils.add_boolean_argument
            )
            assert value is expected


def _cake_parser(conf_path):
    """The real ``ct-cake`` flag inventory on a probe parser.

    ``preprocess`` is the one dest in the tree carrying a third action, and
    the hand-built single-registration probe above cannot show it. Built
    from ``Cake.add_arguments`` rather than a replica so a change to the
    deprecated synonym reaches these cells.
    """
    parser = compiletools.apptools._ComposingArgumentParser(
        description="cake conf boolean probe",
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        auto_env_var_prefix=_PROBE_ENV_PREFIX,
        default_config_files=[str(conf_path)],
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
        conflict_handler="resolve",
        config_file_parser_class=compiletools.apptools_argparse._AccumulatingConfigFileParser,
    )
    compiletools.cake.Cake.add_arguments(parser)
    return parser


def _parse_cake_with_conf(tmp_path, conf_body, argv=()):
    conf = tmp_path / "ct.conf"
    conf.write_text(conf_body)
    return _cake_parser(conf).parse_known_args(list(argv))


class TestADestCarryingAThirdValueTakingAction:
    """``cake.py`` registers a deprecated ``--CT_PREPROCESS`` synonym against
    the ``preprocess`` dest, so that dest carries THREE actions where every
    other boolean carries two.

    Typing the candidate by ``const`` alone admits the synonym, because a
    plain store action's ``const`` is None and ``None is not False``. Two
    candidates then trip the ambiguity guard, ``_boolean_negation_partner``
    declines to choose, and ``no-preprocess = False`` stays inert -- the one
    flag of eight that the const-typed candidate did not reach when the
    action-class filter came off. That filter had been masking the synonym
    incidentally.

    A boolean partner always carries a boolean ``const``; a value-taking
    synonym never does. Requiring one is what separates them without
    weakening the ambiguity guard, which the last cell holds.
    """

    def test_the_preprocess_dest_carries_a_third_action_with_a_non_boolean_const(self, tmp_path):
        """Premise pin: without the third action the other cells would pass
        against a two-action shape that never had the defect."""
        parser = _cake_parser(tmp_path / "absent.conf")
        consts = [action.const for action in parser._actions if action.dest == "preprocess"]
        assert sorted(str(const) for const in consts) == ["False", "None", "True"]

    def test_a_falsey_value_on_the_no_key_turns_preprocess_on(self, tmp_path):
        args, unknown = _parse_cake_with_conf(tmp_path, "no-preprocess = False\n")
        assert unknown == []
        assert args.preprocess is True

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("no-preprocess = True\n", False),
            ("preprocess = True\n", True),
            ("preprocess = False\n", False),
        ],
    )
    def test_the_spellings_that_already_worked_are_unchanged(self, tmp_path, body, expected):
        args, _ = _parse_cake_with_conf(tmp_path, body)
        assert args.preprocess is expected

    def test_two_boolean_candidates_still_refuse_to_resolve(self, tmp_path):
        """The ambiguity guard is narrowed, not disabled: a second candidate
        carrying a boolean ``const`` still leaves the partner unresolved."""
        parser = _probe_parser(
            tmp_path / "absent.conf",
            "auto",
            default=False,
            helper=compiletools.utils.add_flag_argument,
        )
        parser.add_argument("--also-auto", dest="auto", action="store_true")
        negative = parser._option_string_actions["--no-auto"]
        assert parser._boolean_negation_partner(negative) is None


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
