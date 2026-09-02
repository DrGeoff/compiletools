"""Stage-0 guard for the default-on ``--auto-pkg-config`` flag.

``--auto-pkg-config`` (registered next to ``--pkg-config`` in
``apptools_argparse``) defaults to ON and reads ``CT_AUTO_PKG_CONFIG``.
Nothing consumes the flag yet — the feature arrives in a later stage — but
the default-on flip cannot be allowed to make the suite host-dependent, so
``conftest.py`` already forces it off (autouse ``auto_pkg_config_off``)
and this file pins the whole contract before any consumer exists:

* under pytest (env ``0``), a parse yields ``auto_pkg_config is False``;
* the opt-in ``auto_pkg_config_on`` fixture (env ``1``) yields ``True``;
* an explicit ``--no-auto-pkg-config`` on argv beats env ``1``;
* an explicit ``--auto-pkg-config`` on argv beats env ``0``.

The parses run through the production parser class
(``_ComposingArgumentParser`` — what ``apptools.create_parser`` builds) and
the production registration path (``apptools.add_common_arguments``, where
the flag is registered), with no conf files so nothing ambient can set it.
The env-var-to-argv translation for the boolean pair lives on that parser
class, so a probe built on a bare ``configargparse.ArgumentParser`` would
not exercise the path the autouse guard depends on.
"""

import configargparse

import compiletools.apptools
import compiletools.apptools_argparse


def _common_arguments_parser():
    """A production parser carrying the full common-arguments surface.

    Same parser class and kwargs ``apptools.create_parser`` uses, but with
    an empty conf-file list so no ambient ct.conf can set the flag, and the
    flag itself registered through ``add_common_arguments`` — the
    registration under test.
    """
    parser = compiletools.apptools_argparse._ComposingArgumentParser(
        description="auto-pkg-config flag probe",
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        auto_env_var_prefix="",
        default_config_files=[],
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
        conflict_handler="resolve",
        config_file_parser_class=compiletools.apptools_argparse._AccumulatingConfigFileParser,
    )
    compiletools.apptools.add_common_arguments(parser, argv=[])
    return parser


class TestAutoPkgConfigIsolationGuard:
    """The env-var guard from conftest must reach the parsed namespace."""

    def test_parsing_under_pytest_defaults_the_flag_off(self):
        """The autouse ``auto_pkg_config_off`` fixture sets
        CT_AUTO_PKG_CONFIG=0 for every test; a parse must see the flag as
        False, or the suite is exercising the host-dependent default."""
        args, unknown = _common_arguments_parser().parse_known_args([])
        assert unknown == []
        assert args.auto_pkg_config is False

    def test_auto_pkg_config_on_fixture_reenables_the_flag(self, auto_pkg_config_on):
        """The opt-in fixture overrides the guard to CT_AUTO_PKG_CONFIG=1."""
        args, unknown = _common_arguments_parser().parse_known_args([])
        assert unknown == []
        assert args.auto_pkg_config is True

    def test_no_form_on_argv_beats_the_truthy_env_var(self, auto_pkg_config_on):
        """``--no-auto-pkg-config`` on argv wins over env ``1`` (and must
        not collide with the env-derived partner token)."""
        args, unknown = _common_arguments_parser().parse_known_args(["--no-auto-pkg-config"])
        assert unknown == []
        assert args.auto_pkg_config is False

    def test_plain_form_on_argv_beats_the_falsey_env_var(self, auto_pkg_config_off):
        """``--auto-pkg-config`` on argv wins over env ``0`` from the
        autouse guard: the env var must not mask an explicit opt-in."""
        args, unknown = _common_arguments_parser().parse_known_args(["--auto-pkg-config"])
        assert unknown == []
        assert args.auto_pkg_config is True
