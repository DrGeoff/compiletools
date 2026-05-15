import logging
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
            assert variant == "gcc.cxx26.debug"

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
            assert variant == "gcc.cxx26.debug"

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

    def test_canonical_order_override_with_project_axis(self):
        # A project can include its own axis tokens in the canonical-order
        # declaration. Tokens listed in the override sort to their declared
        # position; tokens NOT listed still trail in user-typed order. This
        # verifies the override + unknown-token rules compose correctly
        # (the unknown-token tail rule only kicks in for tokens the override
        # doesn't cover, not for tokens that fail to match the BUILTIN
        # order).
        with uth.TempDirContext():
            with open("ct.conf", "w") as fh:
                fh.write("variant = blank\n")
                # myproj is now KNOWN — its declared position is between gcc and debug.
                # extralib is left unknown — should still trail in user-typed order.
                fh.write("variant-canonical-order = gcc, myproj, debug, asan\n")
            assert (
                compiletools.configutils.canonicalize_variant_input(
                    "asan,myproj,debug,gcc",
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir="/var",
                    gitroot=os.getcwd(),
                )
                == "gcc.myproj.debug.asan"
            )
            # Unknown token mixed in with override-known tokens — known
            # tokens sort by declared position; unknown trails in input order.
            assert (
                compiletools.configutils.canonicalize_variant_input(
                    "extralib,asan,gcc,myproj",
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir="/var",
                    gitroot=os.getcwd(),
                )
                == "gcc.myproj.asan.extralib"
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
        # A literal gcc.debug.conf takes precedence — its flags layer last
        # over the synthesized atoms (gcc + debug), not instead of them.
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
            # Atoms still contribute — composite tunes on top, doesn't replace.
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == ["gcc", "debug"]

    def test_bundle_dev_pulls_in_all_extends(self):
        # The `dev` bundle's extends declaration chains the full sanitizer-
        # driven dev iteration setup. Verify each named atom shows up in the
        # resolved axis list (in extends order, deduped, with dev itself last).
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="dev")
            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="dev",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            # dev.conf: extends = ccache-gcc, cxx26, debug, asan, ubsan, werror
            # ccache-gcc itself extends gcc, so gcc appears first in the chain.
            assert axis_names == ["gcc", "ccache-gcc", "cxx26", "debug", "asan", "ubsan", "werror", "dev"]

    def test_bundle_production_full_chain(self):
        # production = ccache-gcc, cxx26, release, lto, hardened, pie, strip
        # ccache-gcc itself extends gcc, so gcc appears first in the chain.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="production")
            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="production",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == [
                "gcc",
                "ccache-gcc",
                "cxx26",
                "release",
                "lto",
                "hardened",
                "pie",
                "strip",
                "production",
            ]

    def test_bundle_safety_uses_clang(self):
        # safety bundle picks clang explicitly because its sanitizer libs
        # are more comprehensive than gcc's.
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="safety")
            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="safety",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            assert axis_names[0] == "clang", f"safety must start with clang; got {axis_names}"
            assert "asan" in axis_names and "ubsan" in axis_names

    def test_linker_axis_composes_with_toolchain_opt_instrumentation(self):
        # The bundled linker axes (ld/gold/mold/wild) sit between toolchain
        # and optimization in canonical order. --variant=gcc,mold,release,asan
        # canonicalizes accordingly and synthesizes all four axes — mold's
        # -fuse-ld=mold lands on append-LDFLAGS so the linker choice flows
        # through to the link step.
        with uth.TempDirContext():
            assert (
                compiletools.configutils.canonicalize_variant_input(
                    "asan,release,mold,gcc",
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir="/var",
                    gitroot=os.getcwd(),
                )
                == "gcc.mold.release.asan"
            )

        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.mold.release.asan")
            with uth.DirectoryContext(repo_root):
                resolution = compiletools.configutils.resolve_variant(
                    variant="gcc,mold,release,asan",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,  # let the bundled dir be discovered
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == ["gcc", "mold", "release", "asan"]
            # The mold axis conf file should be in the resolution's flat path list.
            assert any(p.endswith("/mold.conf") for p in resolution.flat_paths), (
                f"mold.conf missing from flat_paths: {resolution.flat_paths}"
            )

    def test_composite_with_explicit_extends_picks_own_parents(self):
        # A composite that names its own `extends = ...` overrides the
        # implicit "extends from each canonical token" rule. Useful for
        # opting out of the implicit composition (e.g. `extends = blank`).
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            for name, content in [
                ("blank.conf", "# empty floor\n"),
                ("gcc.conf", "CC = gcc\n"),
                ("debug.conf", "append-CFLAGS = -g\n"),
                ("gcc.debug.conf", "extends = blank\nappend-CFLAGS = -DSOLO=1\n"),
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
            # extends=blank wins over the implicit gcc+debug pull-in.
            axis_names = [a.name for a in resolution.axes]
            assert axis_names == ["blank"]

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
            # The composite gcc.debug.conf now implicitly extends from each
            # canonical token, so both atom files must exist for resolution
            # to succeed. Provide minimal placeholders so the composite
            # priority assertion can still be checked.
            for atom in ("gcc.conf", "debug.conf"):
                with open(os.path.join(repo_conf_d, atom), "w") as fh:
                    fh.write("# placeholder atom\n")
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

                # cwd's gcc.debug.conf wins as the composite_override
                # (highest priority). The atom files contribute their own
                # entries earlier in the list.
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

    def test_bundled_ct_conf_comment_example_matches_builtin(self):
        """The commented `variant-canonical-order` example in the bundled
        ct.conf must enumerate the same tokens as `_DEFAULT_VARIANT_CANONICAL_ORDER`.

        The example exists so a user can copy-paste it as a starting point
        for their own override. If it drifts from the builtin (someone adds
        a new axis to one but not the other), copy-pasters silently get a
        stale list. This test is the drift guard.
        """
        import re

        ct_conf_path = os.path.join(
            os.path.dirname(compiletools.configutils.__file__),
            "ct.conf.d",
            "ct.conf",
        )
        with open(ct_conf_path) as fh:
            text = fh.read()
        m = re.search(r"^#\s*variant-canonical-order\s*=\s*(.+)$", text, re.MULTILINE)
        assert m is not None, (
            f"Expected a commented `# variant-canonical-order = ...` example "
            f"in {ct_conf_path}; users rely on it as a starting point for "
            f"writing their own override."
        )
        example_tokens = compiletools.configutils.split_variant(m.group(1))
        assert example_tokens == compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER, (
            "Commented example in bundled ct.conf has drifted from "
            "_DEFAULT_VARIANT_CANONICAL_ORDER. Update both. Diff:\n"
            f"  builtin only: {set(compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER) - set(example_tokens)}\n"
            f"  example only: {set(example_tokens) - set(compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER)}\n"
            f"  builtin head: {compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER[:5]}\n"
            f"  example head: {example_tokens[:5]}"
        )

    def test_canonical_order_can_be_overridden_via_cli(self):
        """--variant-canonical-order on the CLI overrides ct.conf and the
        builtin tuple.

        Mirrors the existing override hierarchy (CLI > env > ct.conf >
        builtin) for every other ct-* option, so a user can scope a
        custom order to a single invocation without editing a conf file.
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="blank")
            with uth.DirectoryContext(repo_root):
                argv = ["--variant-canonical-order=zzz,gcc,debug,asan"]
                order, source = compiletools.configutils.get_canonical_order(
                    argv=argv,
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            assert order == ("zzz", "gcc", "debug", "asan"), order
            assert source == "argv", source

    def test_canonical_order_can_be_overridden_via_env(self, monkeypatch):
        """CT_VARIANT_CANONICAL_ORDER env var overrides ct.conf and builtin,
        but loses to a CLI flag."""
        monkeypatch.setenv("CT_VARIANT_CANONICAL_ORDER", "blank,gcc,debug")
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="blank")
            with uth.DirectoryContext(repo_root):
                order, source = compiletools.configutils.get_canonical_order(
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            assert order == ("blank", "gcc", "debug"), order
            assert source == "env:CT_VARIANT_CANONICAL_ORDER", source

    def test_canonical_order_cli_beats_env_beats_conf(self, monkeypatch):
        """Full priority hierarchy: CLI > env > ct.conf > builtin."""
        monkeypatch.setenv("CT_VARIANT_CANONICAL_ORDER", "env,wins,over,conf")
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="blank")
            with open(os.path.join(repo_root, "ct.conf"), "a") as fh:
                fh.write("variant-canonical-order = conf, only\n")
            with uth.DirectoryContext(repo_root):
                # env beats ct.conf
                order, source = compiletools.configutils.get_canonical_order(
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
                assert order == ("env", "wins", "over", "conf"), order
                assert source.startswith("env:"), source
                # CLI beats env
                order, source = compiletools.configutils.get_canonical_order(
                    argv=["--variant-canonical-order=cli,wins,all"],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
                assert order == ("cli", "wins", "all"), order
                assert source == "argv", source

    def test_bundled_composite_extends_obeys_canonical_order(self):
        """Bundled composite conf files (the ones with ``extends = ...``)
        must list their parents in canonical order, and must themselves be
        positioned after all their parents in ``_DEFAULT_VARIANT_CANONICAL_ORDER``.

        Why this matters: ``_resolve_axis`` walks ``extends`` in declared
        order, and configargparse layers per-axis scalar keys
        last-writer-wins. So ``extends = werror, gcc`` would load
        ``werror.conf`` BEFORE ``gcc.conf`` — different from the
        equivalent ``--variant=gcc,werror`` invocation which loads them
        in canonical (gcc-first) order. Bundles must mirror what a user
        gets from typing the same tokens on the CLI; otherwise
        ``--variant=production`` silently differs from the manually-
        written equivalent.

        Also: a bundle whose canonical position precedes one of its
        parents would canonicalize to a name with a parent trailing the
        bundle, which is semantically nonsensical (bundles are
        composites OF their parents, so they conceptually come "after").
        """
        bundled_conf_d = os.path.join(os.path.dirname(compiletools.configutils.__file__), "ct.conf.d")
        canonical = compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER
        position = {tok: i for i, tok in enumerate(canonical)}

        # Discover all conf files that declare `extends = ...`. This
        # naturally extends to any new bundles added later — no hardcoded
        # bundle list to keep in sync.
        composite_confs = []
        for entry in sorted(os.listdir(bundled_conf_d)):
            if not entry.endswith(".conf"):
                continue
            path = os.path.join(bundled_conf_d, entry)
            extends = compiletools.configutils._parse_extends_directive(path)
            if extends:
                composite_confs.append((entry[: -len(".conf")], path, extends))

        assert composite_confs, (
            "Expected at least one bundled composite conf with `extends = ...` "
            "(dev, ci, production, safety, perf, secure)"
        )

        violations = []
        for name, _path, extends in composite_confs:
            # Every parent must be a known canonical token.
            unknown = [p for p in extends if p not in position]
            if unknown:
                violations.append(
                    f"  {name}.conf extends unknown token(s) {unknown!r}; add them to _DEFAULT_VARIANT_CANONICAL_ORDER."
                )
                continue

            # Parents must be in canonical position order.
            positions = [position[p] for p in extends]
            if positions != sorted(positions):
                expected = sorted(extends, key=lambda p: position[p])
                violations.append(
                    f"  {name}.conf extends list is out of canonical order: "
                    f"got {list(extends)!r}, expected {expected!r}."
                )

            # The bundle itself must come after all its parents.
            if name in position:
                bundle_pos = position[name]
                if positions and bundle_pos <= max(positions):
                    worst_parent = max(extends, key=lambda p: position[p])
                    violations.append(
                        f"  {name}.conf appears at canonical position "
                        f"{bundle_pos} but extends `{worst_parent}` at "
                        f"position {position[worst_parent]}; move {name} "
                        f"later in _DEFAULT_VARIANT_CANONICAL_ORDER."
                    )

        assert not violations, "Composite conf files violate canonical-order invariants:\n" + "\n".join(violations)

    def test_user_conf_with_out_of_order_extends_emits_warning(self, caplog):
        """Runtime guard: a user-authored conf with ``extends = ...``
        parents listed out of canonical order must trigger a logger
        warning naming the file and the recommended order.

        Out-of-order extends silently changes the flag layering compared
        with the equivalent ``--variant=tok1,tok2,...`` CLI form (the
        resolver walks ``extends`` in declared order, configargparse
        layers in load order). The warning surfaces the inconsistency
        without blocking the build — user's call whether to fix.

        Uses ``logging.getLogger(__name__).warning(...)`` (matching the
        ``cache_report.py`` / ``trace_backend.py`` / ``git_sha_report.py``
        precedent and the ``apptools.py`` TODO direction) so users can
        suppress per-module via
        ``logging.getLogger('compiletools.configutils').setLevel(logging.ERROR)``.
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="my-bad-order")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            # werror is canonical-position 41; gcc is 1. Reversed order
            # is the buggy pattern this guard catches.
            with open(os.path.join(conf_d, "my-bad-order.conf"), "w") as fh:
                fh.write("extends = werror, gcc\n")
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            with open(os.path.join(conf_d, "werror.conf"), "w") as fh:
                fh.write("append-CFLAGS = -Werror\n")

            with uth.DirectoryContext(repo_root):
                compiletools.configutils.clear_cache()
                with caplog.at_level(logging.WARNING, logger="compiletools.configutils"):
                    compiletools.configutils.resolve_variant(
                        variant="my-bad-order",
                        argv=[],
                        user_config_dir="/var",
                        system_config_dir="/var",
                        exedir=uth.cakedir(),
                        gitroot=repo_root,
                    )

            matching = [
                rec.getMessage()
                for rec in caplog.records
                if rec.name == "compiletools.configutils" and "my-bad-order.conf" in rec.getMessage()
            ]
            assert matching, (
                "expected a configutils warning naming my-bad-order.conf; "
                f"got {[(r.name, r.getMessage()) for r in caplog.records]!r}"
            )
            msg = matching[0]
            assert "not in canonical order" in msg, msg
            assert "extends = gcc, werror" in msg, msg

    def test_user_axis_extending_known_atoms_resolves_cleanly(self):
        """A user-defined axis (no entry in canonical_order) that
        ``extends`` only builtin tokens must resolve without error: the
        unknown user axis trails after known atoms in canonicalisation,
        and the resolver walks the chain in declared order.

        This pins the "tokens not in the list trail in user-typed order"
        contract documented in `canonicalize_variant_tokens`. Users rely
        on this to add a project axis without redeclaring the whole
        canonical order.
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="myproj")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "myproj.conf"), "w") as fh:
                fh.write("extends = gcc, debug\nappend-CXXFLAGS = -DMYPROJ=1\n")
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\n")
            with open(os.path.join(conf_d, "debug.conf"), "w") as fh:
                fh.write("append-CFLAGS = -g\n")

            with uth.DirectoryContext(repo_root):
                compiletools.configutils.clear_cache()
                resolution = compiletools.configutils.resolve_variant(
                    variant="myproj",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir="/var",
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            # Walk order is declared-order from `extends`, with myproj
            # itself last (composite is loaded after its parents).
            assert axis_names == ["gcc", "debug", "myproj"], axis_names

    def test_user_token_trails_canonical_tokens_in_canonicalisation(self):
        """Mixing a user-defined token with known builtins: the known
        tokens sort to canonical positions, the user token(s) trail in
        the user-typed order they appeared in the input.

        Cases:
          - one unknown + several knowns:    asan,ccache-clang,myproj
          - multiple unknowns preserve order: debug,zproj,gcc,myproj
        """
        order = compiletools.configutils._DEFAULT_VARIANT_CANONICAL_ORDER

        # Single unknown trails after all knowns (knowns canonicalise).
        got = compiletools.configutils.canonicalize_variant_tokens(("myproj", "asan", "ccache-clang"), order)
        assert got == ("ccache-clang", "asan", "myproj"), got

        # Multiple unknowns: knowns canonicalise, unknowns preserve the
        # left-to-right order of their first appearance in the input.
        got = compiletools.configutils.canonicalize_variant_tokens(("debug", "zproj", "gcc", "myproj"), order)
        assert got == ("gcc", "debug", "zproj", "myproj"), got

    def test_user_axis_extending_ccache_wrappers_resolves_cleanly(self):
        """End-to-end: a user-defined axis can extend the bundled
        ``ccache-gcc`` / ``ccache-clang`` axes. Their canonical positions
        sit between the bare toolchain atoms and the rest, so the
        order-check warning stays silent and the chain resolves cleanly.

        Uses ``system_config_dir=None`` so the bundled package conf
        directory is consulted (the other hermetic tests pass an
        explicit ``"/var"`` to block bundled lookup).
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="myproj")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "myproj.conf"), "w") as fh:
                fh.write("extends = ccache-gcc, cxx26, debug\nappend-CXXFLAGS = -DMYPROJ=1\n")

            with uth.DirectoryContext(repo_root):
                compiletools.configutils.clear_cache()
                resolution = compiletools.configutils.resolve_variant(
                    variant="myproj",
                    argv=[],
                    user_config_dir="/var",
                    system_config_dir=None,
                    exedir=uth.cakedir(),
                    gitroot=repo_root,
                )
            axis_names = [a.name for a in resolution.axes]
            # ccache-gcc itself extends gcc -> gcc precedes ccache-gcc.
            assert axis_names == ["gcc", "ccache-gcc", "cxx26", "debug", "myproj"], axis_names

    def test_out_of_order_extends_with_unknown_token_skips_warning(self, caplog):
        """Documented gotcha: when ``extends`` contains ANY token absent
        from canonical_order, the runtime order-check returns early
        without warning (see ``_check_extends_canonical_order``: "unknown
        axis: skip the order check entirely").

        Why pin this: a user with ``extends = werror, ccache-gcc, myhelper``
        (werror canonically sorts AFTER ccache-gcc) gets NO warning because
        ``myhelper`` is unknown — the safety net the other test exercises
        is bypassed. This is by design (avoids spurious warnings on legit
        user-defined axes) but is worth pinning so a future change to the
        unknown-token policy can't silently flip the behaviour.
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="myproj")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            # werror (canonical pos 44+) listed BEFORE gcc (pos 1) — would
            # normally warn. But myhelper is unknown, so the check skips.
            with open(os.path.join(conf_d, "myproj.conf"), "w") as fh:
                fh.write("extends = werror, gcc, myhelper\n")
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            with open(os.path.join(conf_d, "werror.conf"), "w") as fh:
                fh.write("append-CFLAGS = -Werror\n")
            with open(os.path.join(conf_d, "myhelper.conf"), "w") as fh:
                fh.write("append-CXXFLAGS = -DMYHELPER=1\n")

            with uth.DirectoryContext(repo_root):
                compiletools.configutils.clear_cache()
                with caplog.at_level(logging.WARNING, logger="compiletools.configutils"):
                    compiletools.configutils.resolve_variant(
                        variant="myproj",
                        argv=[],
                        user_config_dir="/var",
                        system_config_dir="/var",
                        exedir=uth.cakedir(),
                        gitroot=repo_root,
                    )

            order_warnings = [
                rec.getMessage()
                for rec in caplog.records
                if rec.name == "compiletools.configutils" and "not in canonical order" in rec.getMessage()
            ]
            assert not order_warnings, (
                "extends containing an unknown token must not emit the "
                "canonical-order warning (the check returns early on the "
                "first unknown token). Got: "
                f"{order_warnings!r}"
            )

    def test_resolve_variant_parses_each_conf_at_most_once(self):
        """Regression guard against redundant conf-file parsing.

        The full parseargs flow calls into the resolver several times per
        invocation: extract_variant -> resolve_variant -> canonicalize -> a
        second resolve_variant from _commonsubstitutions. Without caching,
        each call re-opens ct.conf and re-parses every touched axis conf,
        compounding the I/O cost on a 5-axis variant.

        This test mirrors that pattern (resolve + canonicalize + resolve)
        and asserts that no individual conf file is parsed more than once
        across the whole sequence.
        """
        with uth.TempDirContextNoChange() as repo_root:
            uth.create_temp_ct_conf(repo_root, defaultvariant="gcc.debug")
            conf_d = os.path.join(repo_root, "ct.conf.d")
            os.makedirs(conf_d)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\n")
            with open(os.path.join(conf_d, "debug.conf"), "w") as fh:
                fh.write("append-CFLAGS = -g\n")

            compiletools.configutils.clear_cache()

            parse_counts: dict[str, int] = {}
            real_parse = compiletools.configutils.CfgFileParser.parse

            def counting_parse(self, stream):
                path = getattr(stream, "name", "<unknown>")
                parse_counts[path] = parse_counts.get(path, 0) + 1
                return real_parse(self, stream)

            kwargs: dict[str, object] = dict(
                user_config_dir="/var",
                system_config_dir="/var",
                exedir=uth.cakedir(),
                gitroot=repo_root,
            )

            with uth.DirectoryContext(repo_root):
                original = compiletools.configutils.CfgFileParser.parse
                compiletools.configutils.CfgFileParser.parse = counting_parse  # type: ignore[method-assign]
                try:
                    compiletools.configutils.resolve_variant(variant="gcc.debug", argv=[], **kwargs)  # type: ignore[arg-type]
                    compiletools.configutils.canonicalize_variant_input("gcc.debug", **kwargs)  # type: ignore[arg-type]
                    compiletools.configutils.resolve_variant(variant="gcc.debug", argv=[], **kwargs)  # type: ignore[arg-type]
                finally:
                    compiletools.configutils.CfgFileParser.parse = original  # type: ignore[method-assign]

            over_parsed = {p: n for p, n in parse_counts.items() if n > 1}
            assert not over_parsed, (
                "Some conf files were parsed more than once within a single "
                f"parseargs-shaped sequence: {over_parsed}. Ensure all callers "
                "go through configutils._parse_conf_file_cached so repeated "
                "parses within the same process are deduplicated."
            )

    def teardown_method(self):
        uth.reset()
