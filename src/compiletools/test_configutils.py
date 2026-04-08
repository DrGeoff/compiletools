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

    def test_cwd_ct_conf_d_discovered(self):
        """cwd/ct.conf.d/ appears in default_config_directories() output."""
        with uth.TempDirContext() as _:
            cwd_conf_d = os.path.join(os.getcwd(), "ct.conf.d")
            os.makedirs(cwd_conf_d)

            dirs = compiletools.configutils.default_config_directories(
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                gitroot=os.getcwd(),
            )

            assert cwd_conf_d in dirs

    def test_cwd_ct_conf_d_variant_takes_priority(self):
        """cwd/ct.conf.d/variant.conf overrides gitroot/ct.conf.d/variant.conf."""
        with uth.TempDirContextNoChange() as repo_root:
            subproject = os.path.join(repo_root, "subproject")
            os.makedirs(subproject)

            # Create ct.conf at repo root so extract_variant succeeds
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")

            # Create variant conf at repo level
            repo_conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(repo_conf_d)
            repo_variant = os.path.join(repo_conf_d, "gcc.debug.conf")
            uth.create_temp_config(filename=repo_variant, extralines=["REPO_LEVEL=1"])

            # Create variant conf at cwd level (subproject)
            cwd_conf_d = os.path.join(subproject, "ct.conf.d")
            os.makedirs(cwd_conf_d)
            cwd_variant = os.path.join(cwd_conf_d, "gcc.debug.conf")
            uth.create_temp_config(filename=cwd_variant, extralines=["CWD_LEVEL=1"])

            with uth.DirectoryContext(subproject):
                configs = compiletools.configutils.config_files_from_variant(
                    variant="gcc.debug",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    verbose=0,
                    gitroot=repo_root,
                )

                # Both should be found; cwd version should come first (highest priority,
                # since config_files_from_variant iterates reversed/highest-first)
                assert repo_variant in configs
                assert cwd_variant in configs
                assert configs.index(cwd_variant) < configs.index(repo_variant)

    def test_cwd_ct_conf_d_dedup_when_cwd_equals_gitroot(self):
        """No duplicate ct.conf.d entry when cwd is the git root."""
        with uth.TempDirContext() as _:
            conf_d = os.path.join(os.getcwd(), "ct.conf.d")
            os.makedirs(conf_d)

            dirs = compiletools.configutils.default_config_directories(
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                gitroot=os.getcwd(),
            )

            assert dirs.count(conf_d) == 1

    def teardown_method(self):
        uth.reset()
