import os

import pytest

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
        # Composite variants on the CLI canonicalize via the builtin order
        assert compiletools.configutils.extract_variant(["--variant=gcc,debug"]) == "gcc.debug"
        # Whitespace and dot separators are equivalent
        assert compiletools.configutils.extract_variant(["--variant", "debug gcc"]) == "gcc.debug"
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
            uth.create_temp_ct_conf(os.getcwd())
            variant = compiletools.configutils.extract_item_from_ct_conf(
                key="variant",
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                gitroot=os.getcwd(),
            )
            assert variant == "gcc.debug"

    def test_extract_variant_from_blank_argv(self):
        # Force to find the temp directory ct.conf
        with uth.TempDirContext() as _:
            uth.create_temp_ct_conf(os.getcwd())
            variant = compiletools.configutils.extract_variant(
                argv=[],
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                verbose=0,
                gitroot=os.getcwd(),
            )
            assert variant == "gcc.debug"

    def test_canonicalize_variant_input_with_default_order(self):
        # No project ct.conf — falls back to builtin canonical order.
        with uth.TempDirContext():
            for raw, expected in [
                ("gcc,debug,asan", "gcc.debug.asan"),
                ("debug gcc asan", "gcc.debug.asan"),
                ("asan.debug.gcc", "gcc.debug.asan"),
                ("gcc", "gcc"),
                ("", ""),
            ]:
                assert (
                    compiletools.configutils.canonicalize_variant_input(
                        raw,
                        user_config_dir="/var",
                        system_config_dir="/var",
                        exedir="/var",
                        gitroot=os.getcwd(),
                    )
                    == expected
                ), f"raw={raw!r}"

    def test_unknown_axis_preserves_user_order(self):
        # Tokens not in the canonical order go to the end in user-typed
        # order, so a project can introduce a new axis without re-declaring
        # the whole ordering.
        with uth.TempDirContext():
            assert (
                compiletools.configutils.canonicalize_variant_input(
                    "myproj,gcc,debug",
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir="/var",
                    gitroot=os.getcwd(),
                )
                == "gcc.debug.myproj"
            )

    def test_project_canonical_order_override(self):
        # A project-level ct.conf can override the canonical order entirely.
        with uth.TempDirContext():
            with open("ct.conf", "w") as fh:
                fh.write("variant = blank\n")
                fh.write("variant-canonical-order = debug, gcc, asan\n")
            assert (
                compiletools.configutils.canonicalize_variant_input(
                    "gcc,debug,asan",
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir="/var",
                    gitroot=os.getcwd(),
                )
                == "debug.gcc.asan"
            )

    def test_default_configs(self):
        with uth.TempDirContext() as _:
            uth.create_temp_ct_conf(os.getcwd())
            uth.create_temp_config(os.getcwd())

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

    def test_config_files_from_variant_synthesizes_composite(self):
        # When no literal gcc.debug.conf exists but gcc.conf and debug.conf
        # do, the resolver should synthesize the composition.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
                fh.write("append-CFLAGS = -fPIC\n")
            with open(os.path.join(conf_d, "debug.conf"), "w") as fh:
                fh.write("append-CFLAGS = -g\n")

            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="gcc.debug",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )

            assert resolution.canonical_name == "gcc.debug"
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == ["gcc", "debug"]
            assert resolution.composite_override is None  # synthesized, not literal

    def test_explicit_composite_file_overrides_synthesis(self):
        # A literal gcc.debug.conf takes precedence — its flags layer last.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            for name, content in [
                ("gcc.conf", "CC = gcc\n"),
                ("debug.conf", "append-CFLAGS = -g\n"),
                ("gcc.debug.conf", "append-CFLAGS = -DTUNED=1\n"),
            ]:
                with open(os.path.join(conf_d, name), "w") as fh:
                    fh.write(content)

            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="gcc.debug",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )

            assert resolution.composite_override is not None
            assert resolution.composite_override.endswith("gcc.debug.conf")
            assert resolution.flat_paths[-1] == resolution.composite_override

    def test_missing_axis_raises_with_full_hierarchy(self):
        # An axis that exists in NO config dir surfaces a clear error
        # listing every dir searched.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            # debug.conf intentionally absent

            with uth.DirectoryContext(repo_root):
                with pytest.raises(
                    compiletools.configutils.VariantResolutionError,
                ) as exc_info:
                    compiletools.configutils.resolve_variant(
                        variant="gcc.debug",
                        argv=[],
                        user_config_dir="/var",
                        system_config_dir="/var",
                        exedir="/var",
                        gitroot=repo_root,
                    )
            msg = str(exc_info.value)
            assert "debug" in msg
            assert "searched" in msg.lower()

    def test_axis_found_in_bundled_dir_passes(self):
        # gcc.conf and debug.conf live in src/compiletools/ct.conf.d/ (bundled).
        # A project that doesn't redeclare them should still resolve cleanly.
        # This exercises the "missing from full hierarchy" check.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            with uth.DirectoryContext(repo_root):
                # Use the real bundled exedir so gcc.conf/debug.conf are visible.
                resolution = compiletools.configutils.resolve_variant(
                    variant="gcc.debug",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,  # let the bundled dir be discovered
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            assert resolution.canonical_name == "gcc.debug"
            assert {a.name for a in resolution.axes} == {"gcc", "debug"}

    def test_extends_directive_pulls_in_parent(self):
        # If a conf file has `extends = ...`, the parent's flags layer
        # before the child's.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="myrelease")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            with open(os.path.join(conf_d, "myrelease.conf"), "w") as fh:
                fh.write("extends = gcc\n")
                fh.write("append-CFLAGS = -O3\n")

            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="myrelease",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == ["gcc", "myrelease"]

    def test_extends_cycle_detected(self):
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="a")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "a.conf"), "w") as fh:
                fh.write("extends = b\n")
            with open(os.path.join(conf_d, "b.conf"), "w") as fh:
                fh.write("extends = a\n")

            with uth.DirectoryContext(repo_root):
                with pytest.raises(
                    compiletools.configutils.VariantResolutionError,
                    match="cycle",
                ):
                    compiletools.configutils.resolve_variant(
                        variant="a",
                        argv=[],
                        user_config_dir="/var",
                        system_config_dir="/var",
                        exedir=uth.cakedir(),
                        gitroot=repo_root,
                    )

    def test_diamond_dedup(self):
        # x extends a, b ; a extends base ; b extends base -> base appears once
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="x")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "base.conf"), "w") as fh:
                fh.write("append-CFLAGS = -Dbase=1\n")
            with open(os.path.join(conf_d, "a.conf"), "w") as fh:
                fh.write("extends = base\n")
            with open(os.path.join(conf_d, "b.conf"), "w") as fh:
                fh.write("extends = base\n")
            with open(os.path.join(conf_d, "x.conf"), "w") as fh:
                fh.write("extends = a, b\n")

            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="x",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            assert axis_names.count("base") == 1
            # base must precede a and b
            assert axis_names.index("base") < axis_names.index("a")
            assert axis_names.index("base") < axis_names.index("b")

    def test_legacy_variant_alias_key_raises(self):
        # An old ct.conf with `variantaliases = ...` should be flagged loudly
        # via the legacy-key guard, not silently ignored.
        with uth.TempDirContext():
            with open("ct.conf", "w") as fh:
                fh.write("variant = debug\n")
                fh.write("variantaliases = {'debug':'gcc.debug'}\n")
            with pytest.raises(RuntimeError, match="variantaliases"):
                compiletools.apptools._check_legacy_variant_config_keys([os.path.join(os.getcwd(), "ct.conf")])

    def test_format_variant_resolution_includes_axes(self):
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            with open(os.path.join(conf_d, "debug.conf"), "w") as fh:
                fh.write("append-CFLAGS = -g\n")

            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="gcc,debug",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            text = compiletools.configutils.format_variant_resolution(resolution)
            assert "gcc" in text
            assert "debug" in text
            assert "Canonical order" in text
            assert "Axes" in text

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

            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")

            repo_conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(repo_conf_d)
            repo_variant = os.path.join(repo_conf_d, "gcc.debug.conf")
            uth.create_temp_config(filename=repo_variant, extralines=["REPO_LEVEL=1"])

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

                # Both literal gcc.debug.conf files should appear.
                # Since this is a literal-file composite, it lands as the
                # composite_override entry (highest priority — the cwd one).
                assert any(p.endswith(os.path.join("subproject", "ct.conf.d", "gcc.debug.conf")) for p in configs)

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
