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
