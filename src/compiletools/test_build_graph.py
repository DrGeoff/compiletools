from compiletools.build_graph import BuildGraph, BuildRule


class TestBuildRule:
    def test_compile_rule_creation(self):
        rule = BuildRule(
            output="/tmp/obj/foo.o",
            inputs=["/src/foo.cpp", "/src/foo.h"],
            command=["g++", "-c", "/src/foo.cpp", "-o", "/tmp/obj/foo.o"],
            rule_type="compile",
        )
        assert rule.output == "/tmp/obj/foo.o"
        assert "/src/foo.cpp" in rule.inputs
        assert rule.rule_type == "compile"

    def test_link_rule_creation(self):
        rule = BuildRule(
            output="/tmp/bin/foo",
            inputs=["/tmp/obj/foo.o", "/tmp/obj/bar.o"],
            command=["g++", "-o", "/tmp/bin/foo", "/tmp/obj/foo.o", "/tmp/obj/bar.o"],
            rule_type="link",
        )
        assert rule.output == "/tmp/bin/foo"
        assert rule.rule_type == "link"

    def test_phony_rule(self):
        rule = BuildRule(
            output="all",
            inputs=["/tmp/bin/foo"],
            command=None,
            rule_type="phony",
        )
        assert rule.rule_type == "phony"
        assert rule.command is None

    def test_rule_equality_by_output(self):
        r1 = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["gcc", "-c", "foo.cpp"], rule_type="compile")
        r2 = BuildRule(output="foo.o", inputs=["bar.cpp"], command=["gcc", "-c", "bar.cpp"], rule_type="compile")
        assert r1 == r2
        assert hash(r1) == hash(r2)

    def test_rule_order_only_deps(self):
        rule = BuildRule(
            output="/tmp/obj/foo.o",
            inputs=["/src/foo.cpp"],
            command=["g++", "-c", "/src/foo.cpp"],
            rule_type="compile",
            order_only_deps=["/tmp/obj/"],
        )
        assert rule.order_only_deps == ["/tmp/obj/"]

    def test_success_marker_defaults_to_none(self):
        rule = BuildRule(
            output="/tmp/obj/foo.o",
            inputs=["/src/foo.cpp"],
            command=["g++", "-c", "/src/foo.cpp"],
            rule_type="compile",
        )
        assert rule.success_marker is None

    def test_success_marker_accepts_path(self):
        rule = BuildRule(
            output="/tmp/bin/foo.result",
            inputs=["/tmp/bin/foo"],
            command=["/tmp/bin/foo"],
            rule_type="test",
            success_marker="/tmp/bin/foo.result",
        )
        assert rule.success_marker == "/tmp/bin/foo.result"


class TestBuildGraph:
    def test_empty_graph(self):
        g = BuildGraph()
        assert len(g) == 0
        assert g.rules == []

    def test_add_and_retrieve(self):
        g = BuildGraph()
        rule = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["gcc", "-c", "foo.cpp"], rule_type="compile")
        g.add_rule(rule)
        assert len(g) == 1
        assert g.get_rule("foo.o") is rule

    def test_deduplication(self):
        g = BuildGraph()
        r1 = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["gcc", "-c", "foo.cpp"], rule_type="compile")
        r2 = BuildRule(
            output="foo.o", inputs=["foo.cpp", "foo.h"], command=["gcc", "-c", "foo.cpp"], rule_type="compile"
        )
        g.add_rule(r1)
        g.add_rule(r2)
        assert len(g) == 1
        # Last-write-wins
        assert g.get_rule("foo.o") is r2

    def test_contains(self):
        g = BuildGraph()
        g.add_rule(BuildRule(output="foo.o", inputs=["foo.cpp"], command=["gcc", "-c", "foo.cpp"], rule_type="compile"))
        assert "foo.o" in g
        assert "bar.o" not in g

    def test_get_rule_missing(self):
        g = BuildGraph()
        assert g.get_rule("nonexistent") is None

    def test_insertion_order_preserved(self):
        g = BuildGraph()
        for name in ["c.o", "a.o", "b.o"]:
            g.add_rule(BuildRule(output=name, inputs=[], command=["gcc"], rule_type="compile"))
        assert [r.output for r in g.rules] == ["c.o", "a.o", "b.o"]

    def test_realistic_graph(self):
        """A minimal but realistic build graph: 2 source files -> 2 objects -> 1 exe."""
        g = BuildGraph()
        g.add_rule(BuildRule(output="obj/", inputs=[], command=["mkdir", "-p", "obj/"], rule_type="mkdir"))
        g.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp", "util.h"],
                command=["g++", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
                order_only_deps=["obj/"],
            )
        )
        g.add_rule(
            BuildRule(
                output="obj/util.o",
                inputs=["util.cpp", "util.h"],
                command=["g++", "-c", "util.cpp", "-o", "obj/util.o"],
                rule_type="compile",
                order_only_deps=["obj/"],
            )
        )
        g.add_rule(
            BuildRule(
                output="bin/main",
                inputs=["obj/main.o", "obj/util.o"],
                command=["g++", "-o", "bin/main", "obj/main.o", "obj/util.o"],
                rule_type="link",
            )
        )
        g.add_rule(BuildRule(output="build", inputs=["bin/main"], command=None, rule_type="phony"))
        g.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))
        assert len(g) == 6
        compile_rules = [r for r in g.rules if r.rule_type == "compile"]
        assert len(compile_rules) == 2


class TestBuildGraphFiltering:
    def test_rules_by_type(self):
        g = BuildGraph()
        g.add_rule(BuildRule(output="a.o", inputs=["a.cpp"], command=["gcc", "-c"], rule_type="compile"))
        g.add_rule(BuildRule(output="b.o", inputs=["b.cpp"], command=["gcc", "-c"], rule_type="compile"))
        g.add_rule(BuildRule(output="app", inputs=["a.o", "b.o"], command=["gcc", "-o"], rule_type="link"))
        g.add_rule(BuildRule(output="all", inputs=["app"], command=None, rule_type="phony"))

        assert len(g.rules_by_type("compile")) == 2
        assert len(g.rules_by_type("link")) == 1
        assert len(g.rules_by_type("phony")) == 1
        assert len(g.rules_by_type("test")) == 0

    def test_outputs(self):
        g = BuildGraph()
        g.add_rule(BuildRule(output="a.o", inputs=["a.cpp"], command=["gcc"], rule_type="compile"))
        g.add_rule(BuildRule(output="b.o", inputs=["b.cpp"], command=["gcc"], rule_type="compile"))
        assert g.outputs == {"a.o", "b.o"}


class TestFilterToChanged:
    """filter_to_changed has nontrivial fixed-point semantics. Cover
    leaf changes, multi-level propagation, diamond dependencies, the
    no-affected case, and the asymmetric phony-rule pruning."""

    def _make_graph(self):
        g = BuildGraph()
        g.add_rule(BuildRule(output="a.o", inputs=["a.cpp"], command=["g++"], rule_type="compile"))
        g.add_rule(BuildRule(output="b.o", inputs=["b.cpp"], command=["g++"], rule_type="compile"))
        g.add_rule(BuildRule(output="c.o", inputs=["c.cpp"], command=["g++"], rule_type="compile"))
        g.add_rule(BuildRule(output="libx.a", inputs=["a.o", "b.o"], command=["ar"], rule_type="static_library"))
        g.add_rule(BuildRule(output="bin/main", inputs=["libx.a", "c.o"], command=["g++"], rule_type="link"))
        g.add_rule(BuildRule(output="build", inputs=["bin/main", "libx.a"], command=None, rule_type="phony"))
        return g

    def test_leaf_change_propagates_to_phony_through_intermediates(self):
        """A change to a.cpp must cascade: a.o -> libx.a -> bin/main -> build."""
        g = self._make_graph()
        filtered = g.filter_to_changed({"a.cpp"})

        outputs = {r.output for r in filtered.rules}
        # Affected non-phony rules must be present
        assert "a.o" in outputs
        assert "libx.a" in outputs
        assert "bin/main" in outputs
        # Unaffected non-phony rules must NOT be present
        assert "b.o" not in outputs
        assert "c.o" not in outputs
        # Phony rules ARE present even if some inputs are unaffected
        assert "build" in outputs

    def test_no_affected_rules_yields_only_phony_with_pruned_inputs(self):
        """When no compile/link rule is affected, the graph keeps phony
        rules but their inputs are pruned to nothing."""
        g = self._make_graph()
        filtered = g.filter_to_changed({"unrelated.cpp"})

        non_phony = [r for r in filtered.rules if r.rule_type != "phony"]
        phony = [r for r in filtered.rules if r.rule_type == "phony"]
        assert non_phony == []
        # Phony rules survive but with empty inputs
        for r in phony:
            assert r.inputs == []

    def test_phony_rule_inputs_pruned_to_only_affected(self):
        """phony.inputs is pruned to those targets actually being rebuilt;
        inputs not in the affected set are dropped from the phony rule."""
        g = self._make_graph()
        filtered = g.filter_to_changed({"c.cpp"})  # only c.o, bin/main affected

        build_rule = next(r for r in filtered.rules if r.output == "build")
        # bin/main is affected, libx.a is not — phony "build" inputs should
        # be pruned to {bin/main} only.
        assert "bin/main" in build_rule.inputs
        assert "libx.a" not in build_rule.inputs

    def test_diamond_dependency_does_not_double_count(self):
        """A change reaching the same output via two paths must produce
        the rule exactly once (set semantics in the affected set)."""
        g = BuildGraph()
        g.add_rule(BuildRule(output="x.o", inputs=["x.cpp"], command=["g++"], rule_type="compile"))
        g.add_rule(BuildRule(output="left", inputs=["x.o"], command=["g++"], rule_type="link"))
        g.add_rule(BuildRule(output="right", inputs=["x.o"], command=["g++"], rule_type="link"))
        g.add_rule(
            BuildRule(
                output="diamond",
                inputs=["left", "right"],
                command=["g++"],
                rule_type="link",
            )
        )

        filtered = g.filter_to_changed({"x.cpp"})
        outputs = [r.output for r in filtered.rules]
        # Diamond convergence — each output appears exactly once
        assert outputs.count("diamond") == 1
        assert outputs.count("x.o") == 1
        assert {r.output for r in filtered.rules} == {"x.o", "left", "right", "diamond"}

    def test_change_to_intermediate_propagates_to_dependents(self):
        """If a.o is "changed" (e.g. command-line drove a rebuild), the
        rule depending on it (libx.a, bin/main) should rebuild too."""
        g = self._make_graph()
        # Simulate: a.o is in the affected set directly (not its source)
        filtered = g.filter_to_changed({"a.o"})
        outputs = {r.output for r in filtered.rules}
        assert "libx.a" in outputs
        assert "bin/main" in outputs
        assert "b.o" not in outputs  # not a downstream of a.o

    def test_filter_returns_new_graph(self):
        """filter_to_changed returns a new BuildGraph; original is intact."""
        g = self._make_graph()
        original_count = len(g)
        filtered = g.filter_to_changed({"a.cpp"})
        assert filtered is not g
        assert len(g) == original_count  # original unchanged

    def test_empty_changed_yields_empty_filter(self):
        """No changes -> no non-phony rules in the filtered graph."""
        g = self._make_graph()
        filtered = g.filter_to_changed(set())
        non_phony = [r for r in filtered.rules if r.rule_type != "phony"]
        assert non_phony == []
