import os

import compiletools.listvariants
import compiletools.testhelper as uth
from compiletools.listvariants import FilelistStyle, FlatStyle


def test_flat_style():
    style = FlatStyle()
    style.append_text("ignored")
    style.append_variants(["b", "a"])
    assert style.output == "a b "


def test_filelist_style():
    style = FilelistStyle()
    style.append_text("ignored")
    style.append_variants(["b", "a"])
    assert style.output == "a\nb\n"


def test_find_variants_with_configname():
    with uth.TempDirContextWithChange() as tempdir, uth.ParserContext():
        uth.create_temp_ct_conf(tempdir)
        tempdir_real = os.path.realpath(tempdir)

        from unittest.mock import Mock

        args = Mock()
        args.style = "flat"
        args.shorten = True
        args.repoonly = False
        args.configname = True

        output = compiletools.listvariants.find_possible_variants(
            user_config_dir="/nonexistent",
            system_config_dir="/nonexistent",
            exedir=uth.cakedir(),
            args=args,
            verbose=0,
            gitroot=tempdir_real,
        )
        # With configname=True, variants should end in .conf
        for token in output.split():
            if token and token != " ":
                assert token.endswith(".conf"), f"Expected .conf suffix: {token}"


def test_none_found():
    # This test doesn't need the config file from CompileToolsTestContext,
    # only temp directory and parser reset.
    with uth.TempDirContextWithChange() as tempdir, uth.ParserContext():
        uth.create_temp_ct_conf(tempdir)

        tempdir_real = os.path.realpath(tempdir)

        # These values are deliberately chosen so that we can know that
        # no config files will be found except those in the temp directory.
        ucd = "/home/dummy/.config/ct"
        scd = "/usr/lib"
        ecd = uth.cakedir()

        output = compiletools.listvariants.find_possible_variants(
            user_config_dir=ucd, system_config_dir=scd, exedir=ecd, verbose=9, gitroot=tempdir_real
        )
        # The output now leads with the composition explainer + canonical
        # token order, not a variantaliases dict (which is gone). Validate
        # the structurally interesting parts rather than pinning the exact
        # rendering.
        assert "compose via axis conf files" in output
        assert "Canonical token order" in output
        assert "blank, gcc, ccache-gcc, clang" in output
        # Each searched dir is still listed; the synthetic ones produce "None found".
        for path in (ucd, scd, os.path.join(ecd, "ct", "ct.conf.d")):
            assert path in output
        assert "None found" in output
