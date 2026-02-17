import os

import compiletools.apptools
import compiletools.configutils
import compiletools.testhelper as uth


class TestVariant:
    def setup_method(self):
        uth.reset()

    def test_extract_value_from_argv(self):
        argv = ["/usr/bin/ct-config", "--pkg-config=fig", "-v"]

        value = compiletools.configutils.extract_value_from_argv("pkg-config", argv)
        assert value == "fig"

        value = compiletools.configutils.extract_value_from_argv("config", argv)
        assert value is None

    def test_extract_variant(self):
        assert compiletools.configutils.extract_variant(["--variant=abc"]) == "abc"
        assert compiletools.configutils.extract_variant(["--variant", "abc"]) == "abc"
        assert (
            compiletools.configutils.extract_variant(
                ["-a", "-b", "-x", "--blah", "--variant=abc.123", "-a", "-b", "-z", "--blah"]
            )
            == "abc.123"
        )
        assert (
            compiletools.configutils.extract_variant(
                ["-a", "-b", "-x", "--blah", "--variant", "abc.123", "-a", "-b", "-cz--blah"]
            )
            == "abc.123"
        )

        # Note the -c overrides the --variant
        assert (
            compiletools.configutils.extract_variant(
                ["-a", "-b", "-c", "blah.conf", "--variant", "abc.123", "-a", "-b", "-cz--blah"]
            )
            == "blah"
        )

    def test_extract_variant_from_ct_conf(self):
        # Should find the one in the temp directory ct.conf
        with uth.TempDirContext() as _:
            compiletools.testhelper.create_temp_ct_conf(os.getcwd())
            variant = compiletools.configutils.extract_item_from_ct_conf(
                key="variant",
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                gitroot=os.getcwd(),
            )
            assert variant == "dbg"

    def test_extract_variant_from_blank_argv(self):
        # Force to find the temp directory ct.conf
        with uth.TempDirContext() as _:
            compiletools.testhelper.create_temp_ct_conf(os.getcwd())
            variant = compiletools.configutils.extract_variant(
                argv=[],
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                verbose=0,
                gitroot=os.getcwd(),
            )
            assert variant == "foo.debug"

    def test_default_configs(self):
        with uth.TempDirContext() as _:
            compiletools.testhelper.create_temp_ct_conf(os.getcwd())
            compiletools.testhelper.create_temp_config(os.getcwd())

            configs = compiletools.configutils.get_existing_config_files(
                filename="ct.conf",
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                verbose=0,
                gitroot=os.getcwd(),
            )

            assert [
                os.path.join(os.getcwd(), "ct.conf"),
            ] == configs

    def test_config_files_from_variant(self):
        with uth.TempDirContext() as _:
            compiletools.testhelper.create_temp_ct_conf(os.getcwd())
            # Deliberately call the next config gcc.debug.conf to verify that
            # the hierarchy of directories is working
            compiletools.testhelper.create_temp_config(os.getcwd(), "gcc.debug.conf")

            configs = compiletools.configutils.config_files_from_variant(
                variant="gcc.debug",
                argv=[],
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                verbose=0,
                gitroot=os.getcwd(),
            )

            assert [
                os.path.join(os.getcwd(), "ct.conf"),
                os.path.join(os.getcwd(), "gcc.debug.conf"),
            ] == configs

    def teardown_method(self):
        uth.reset()
