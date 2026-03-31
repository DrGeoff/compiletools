# Alternate Build Backends Implementation Plan

> **Status:** COMPLETED (2026-03-22). All tasks implemented, plus additional backends (CMake, Bazel, Shake, Tup) and test support added beyond the original scope.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract the Makefile-specific code from MakefileCreator into a pluggable backend architecture so that ct-cake can generate build files for Ninja, CMake, or other build systems without duplicating the dependency analysis, flag computation, and naming logic.

**Architecture:** Introduce a `BuildBackend` abstract base class that defines the contract for generating and executing build files. Extract the reusable logic (source discovery, rule construction from Hunter/Namer queries) into a backend-agnostic `BuildGraph` intermediate representation. The existing `MakefileCreator` becomes `MakefileBackend`, the first concrete implementation. `cake.py` delegates to the selected backend via a `--backend` CLI argument. Compilation database generation remains independent (it already has its own creator).

**Tech Stack:** Python 3.9+, ABC (abstract base classes), existing Hunter/Namer/MagicFlags infrastructure, pytest for TDD.

---

## Background: Current Architecture

### Key Files

| File | Role |
|------|------|
| `src/compiletools/cake.py` | Build orchestrator. `Cake._callmakefile()` creates `MakefileCreator`, calls `.create()`, then invokes `make` subprocess. |
| `src/compiletools/makefile.py` | `MakefileCreator` — generates Makefile rules and writes them to disk. `Rule` class represents a Makefile rule. `LinkRuleCreator` hierarchy generates link commands. |
| `src/compiletools/hunter.py` | Dependency graph — provides `getsources()`, `required_source_files()`, `header_dependencies()`, `magicflags()`, `macro_state_hash()`. |
| `src/compiletools/namer.py` | Output path computation — `object_pathname()`, `executable_pathname()`, `compute_dep_hash()`, etc. |
| `src/compiletools/compilation_database.py` | Generates `compile_commands.json` independently. |

### Current Data Flow

```
cake.py::Cake.process()
  └─ _callmakefile()
     ├─ MakefileCreator(args, hunter).create()
     │  ├─ hunter.huntsource()           # discover all source files
     │  ├─ _create_compile_rules_for_sources()
     │  │  └─ for each source:
     │  │     ├─ hunter.header_dependencies(source)   → prerequisites
     │  │     ├─ hunter.magicflags(source)             → CPPFLAGS/CFLAGS/CXXFLAGS
     │  │     ├─ hunter.macro_state_hash(source)       → object naming
     │  │     ├─ namer.object_pathname(...)             → output path
     │  │     └─ Rule(target=obj, prerequisites=..., recipe="gcc -c ...")
     │  ├─ _create_link_rules_for_sources()
     │  │  └─ LinkRuleCreator subclass.__call__()
     │  │     ├─ hunter.required_source_files()         → all sources for linking
     │  │     ├─ namer.object_pathname() per source     → object paths
     │  │     ├─ hunter.magicflags() per source         → LDFLAGS
     │  │     └─ Rule(target=exe, prerequisites=..., recipe="g++ -o ...")
     │  ├─ _create_test_rules(), _create_clean_rules()
     │  └─ write(makefile_name)                         → Makefile text
     ├─ subprocess.check_call(["make", ...])            # execute build
     └─ _copyexes()                                    # post-build
```

### What's Backend-Specific vs Backend-Agnostic

**Backend-agnostic (reusable):**
- Source discovery (`hunter.huntsource()`, `getsources()`)
- Dependency analysis (`hunter.header_dependencies()`, `required_source_files()`)
- Flag extraction (`hunter.magicflags()`)
- Object naming (`namer.object_pathname()`, `macro_state_hash()`, `compute_dep_hash()`)
- Compiler/linker command construction (which compiler, which flags)
- Test discovery, build-only-changed filtering
- Up-to-date checking logic (concept, not implementation)
- Post-build copy logic (`_copyexes()`)

**Backend-specific:**
- Output file format (Makefile syntax vs Ninja syntax vs CMakeLists.txt)
- Build execution command (`make` vs `ninja` vs `cmake --build`)
- Phony targets, order-only prerequisites, `.DELETE_ON_ERROR` (Makefile concepts)
- Directory creation rules (Makefile pattern vs Ninja's implicit creation)
- Test execution strategy (Make's `-j` parallelism vs Ninja's pool system)

---

## Task 1: Create the BuildRule Data Class

Extract the `Rule` concept into a backend-agnostic intermediate representation. This is NOT Makefile-specific — it's a generic "compile this file into that file using this command."

**Files:**
- Create: `src/compiletools/build_graph.py`
- Test: `src/compiletools/test_build_graph.py`

**Step 1: Write the failing test**

```python
# src/compiletools/test_build_graph.py
from compiletools.build_graph import BuildRule, BuildGraph


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
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_build_graph.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'compiletools.build_graph'`

**Step 3: Write minimal implementation**

```python
# src/compiletools/build_graph.py
"""Backend-agnostic build graph representation.

BuildRule and BuildGraph provide an intermediate representation of the build
that is independent of any specific build system (Make, Ninja, CMake, etc.).
Backends consume a BuildGraph to produce their native output format.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BuildRule:
    """A single build action: produce `output` from `inputs` by running `command`.

    Attributes:
        output: The file this rule produces (or a phony target name).
        inputs: Files this rule depends on (source files, headers, objects).
        command: Shell command list to execute, or None for phony rules.
        rule_type: One of "compile", "link", "test", "phony", "mkdir", "clean", "copy".
        order_only_deps: Dependencies that must exist but whose timestamps are
            not checked (e.g., output directories).
    """

    output: str
    inputs: list[str]
    command: list[str] | None
    rule_type: str
    order_only_deps: list[str] = field(default_factory=list)

    def __eq__(self, other):
        if not isinstance(other, BuildRule):
            return NotImplemented
        return self.output == other.output

    def __hash__(self):
        return hash(self.output)


class BuildGraph:
    """Ordered collection of BuildRules forming a complete build description.

    Rules are stored in insertion order and deduplicated by output path.
    """

    def __init__(self):
        self._rules: dict[str, BuildRule] = {}

    def add_rule(self, rule: BuildRule) -> None:
        self._rules[rule.output] = rule

    def get_rule(self, output: str) -> BuildRule | None:
        return self._rules.get(output)

    @property
    def rules(self) -> list[BuildRule]:
        return list(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    def __contains__(self, output: str) -> bool:
        return output in self._rules
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_graph.py -v`
Expected: PASS — all 5 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/build_graph.py src/compiletools/test_build_graph.py
git commit -m "feat: add BuildRule and BuildGraph backend-agnostic IR"
```

---

## Task 2: Add BuildGraph Population Tests

Test that BuildGraph can hold a realistic set of rules (compile + link + phony) and deduplicate correctly.

**Files:**
- Modify: `src/compiletools/test_build_graph.py`

**Step 1: Write the failing tests**

Append to `test_build_graph.py`:

```python
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
        r2 = BuildRule(output="foo.o", inputs=["foo.cpp", "foo.h"], command=["gcc", "-c", "foo.cpp"], rule_type="compile")
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
        g.add_rule(BuildRule(
            output="obj/main.o", inputs=["main.cpp", "util.h"],
            command=["g++", "-c", "main.cpp", "-o", "obj/main.o"],
            rule_type="compile", order_only_deps=["obj/"],
        ))
        g.add_rule(BuildRule(
            output="obj/util.o", inputs=["util.cpp", "util.h"],
            command=["g++", "-c", "util.cpp", "-o", "obj/util.o"],
            rule_type="compile", order_only_deps=["obj/"],
        ))
        g.add_rule(BuildRule(
            output="bin/main", inputs=["obj/main.o", "obj/util.o"],
            command=["g++", "-o", "bin/main", "obj/main.o", "obj/util.o"],
            rule_type="link",
        ))
        g.add_rule(BuildRule(output="build", inputs=["bin/main"], command=None, rule_type="phony"))
        g.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))
        assert len(g) == 6
        compile_rules = [r for r in g.rules if r.rule_type == "compile"]
        assert len(compile_rules) == 2
```

**Step 2: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_graph.py::TestBuildGraph -v`
Expected: PASS — all 7 tests pass (implementation from Task 1 should already support this)

**Step 3: Commit**

```bash
git add src/compiletools/test_build_graph.py
git commit -m "test: add BuildGraph population and deduplication tests"
```

---

## Task 3: Create the BuildBackend Abstract Base Class

Define the contract that all backends must implement.

**Files:**
- Create: `src/compiletools/build_backend.py`
- Test: `src/compiletools/test_build_backend.py`

**Step 1: Write the failing test**

```python
# src/compiletools/test_build_backend.py
import pytest
from unittest.mock import MagicMock

from compiletools.build_backend import BuildBackend
from compiletools.build_graph import BuildGraph, BuildRule


class TestBuildBackendContract:
    """Verify the ABC contract: cannot instantiate, must implement all abstract methods."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract"):
            BuildBackend(args=MagicMock(), hunter=MagicMock())

    def test_concrete_subclass_must_implement_generate(self):
        class Incomplete(BuildBackend):
            pass

        with pytest.raises(TypeError, match="abstract"):
            Incomplete(args=MagicMock(), hunter=MagicMock())

    def test_concrete_subclass_works(self):
        class Minimal(BuildBackend):
            def generate(self, graph):
                pass

            def execute(self, target="build"):
                pass

            @staticmethod
            def name():
                return "minimal"

            @staticmethod
            def build_filename():
                return "Minimalfile"

        backend = Minimal(args=MagicMock(), hunter=MagicMock())
        assert backend.name() == "minimal"
        assert backend.build_filename() == "Minimalfile"


class TestBuildBackendCommon:
    """Test the common (non-abstract) methods provided by BuildBackend."""

    def _make_backend(self):
        class Stub(BuildBackend):
            def generate(self, graph):
                self.last_graph = graph

            def execute(self, target="build"):
                pass

            @staticmethod
            def name():
                return "stub"

            @staticmethod
            def build_filename():
                return "Stubfile"

        return Stub

    def test_build_graph_construction(self):
        """The base class should provide build_graph() that populates a BuildGraph
        from the hunter/namer data, reusable across all backends."""
        # This test verifies the method exists and returns a BuildGraph
        # Full integration is tested in Task 6
        StubClass = self._make_backend()
        args = MagicMock()
        args.filename = []
        args.tests = []
        args.static = []
        args.dynamic = []
        args.verbose = 0
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = StubClass(args=args, hunter=hunter)
        graph = backend.build_graph()
        assert isinstance(graph, BuildGraph)
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_build_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'compiletools.build_backend'`

**Step 3: Write minimal implementation**

```python
# src/compiletools/build_backend.py
"""Abstract base class for build backends.

A BuildBackend knows how to:
1. Take a BuildGraph (backend-agnostic) and produce a native build file
   (Makefile, build.ninja, CMakeLists.txt, etc.)
2. Execute the build using the native tool (make, ninja, cmake --build, etc.)

The base class provides `build_graph()` which populates a BuildGraph from the
Hunter/Namer dependency data. This is the shared logic across all backends.
"""
from __future__ import annotations

import abc

import compiletools.namer
from compiletools.build_graph import BuildGraph


class BuildBackend(abc.ABC):
    """Abstract base class for build system backends."""

    def __init__(self, args, hunter):
        self.args = args
        self.hunter = hunter
        self.namer = compiletools.namer.Namer(args)

    @staticmethod
    @abc.abstractmethod
    def name() -> str:
        """Short identifier for this backend (e.g., 'make', 'ninja')."""

    @staticmethod
    @abc.abstractmethod
    def build_filename() -> str:
        """Default output filename (e.g., 'Makefile', 'build.ninja')."""

    @abc.abstractmethod
    def generate(self, graph: BuildGraph) -> None:
        """Write the native build file from the given BuildGraph."""

    @abc.abstractmethod
    def execute(self, target: str = "build") -> None:
        """Invoke the native build tool to execute the build."""

    def build_graph(self) -> BuildGraph:
        """Populate a BuildGraph from hunter/namer data.

        This is the backend-agnostic logic shared by all backends.
        Subclasses call this, then pass the result to generate().
        """
        self.hunter.huntsource()
        graph = BuildGraph()
        # Minimal implementation — Task 5 will flesh this out with
        # compile rules, link rules, test rules, etc.
        return graph
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_backend.py -v`
Expected: PASS — all 3 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/build_backend.py src/compiletools/test_build_backend.py
git commit -m "feat: add BuildBackend ABC with build_graph() common method"
```

---

## Task 4: Create the Backend Registry

A simple registry that maps backend names to classes, with a factory function.

**Files:**
- Modify: `src/compiletools/build_backend.py`
- Test: `src/compiletools/test_build_backend.py`

**Step 1: Write the failing tests**

Append to `test_build_backend.py`:

```python
from compiletools.build_backend import register_backend, get_backend_class, available_backends


class TestBackendRegistry:
    def test_register_and_retrieve(self):
        class FakeBackend(BuildBackend):
            def generate(self, graph):
                pass

            def execute(self, target="build"):
                pass

            @staticmethod
            def name():
                return "fake"

            @staticmethod
            def build_filename():
                return "Fakefile"

        register_backend(FakeBackend)
        assert get_backend_class("fake") is FakeBackend

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend_class("nonexistent_backend_xyz")

    def test_available_backends_returns_list(self):
        result = available_backends()
        assert isinstance(result, list)
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_build_backend.py::TestBackendRegistry -v`
Expected: FAIL — `ImportError: cannot import name 'register_backend'`

**Step 3: Write minimal implementation**

Add to bottom of `src/compiletools/build_backend.py`:

```python
_REGISTRY: dict[str, type[BuildBackend]] = {}


def register_backend(cls: type[BuildBackend]) -> type[BuildBackend]:
    """Register a backend class. Can be used as a decorator."""
    _REGISTRY[cls.name()] = cls
    return cls


def get_backend_class(name: str) -> type[BuildBackend]:
    """Look up a backend class by name. Raises ValueError if not found."""
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return _REGISTRY[name]


def available_backends() -> list[str]:
    """Return sorted list of registered backend names."""
    return sorted(_REGISTRY.keys())
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_backend.py -v`
Expected: PASS — all 6 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/build_backend.py src/compiletools/test_build_backend.py
git commit -m "feat: add backend registry with register/get/list functions"
```

---

## Task 5: Implement build_graph() — Compile and Link Rule Population

Flesh out the `BuildBackend.build_graph()` method to create `BuildRule` objects from Hunter/Namer data. This is the core shared logic — every backend reuses it.

**Files:**
- Modify: `src/compiletools/build_backend.py`
- Modify: `src/compiletools/test_build_backend.py`

**Step 1: Write the failing tests**

Append to `test_build_backend.py`:

```python
import stringzilla as sz
from types import SimpleNamespace

import compiletools.namer


def _make_stub_backend_class():
    """Create a concrete BuildBackend subclass for testing."""

    class StubBackend(BuildBackend):
        def generate(self, graph):
            self.last_graph = graph

        def execute(self, target="build"):
            pass

        @staticmethod
        def name():
            return "stub_test"

        @staticmethod
        def build_filename():
            return "Stubfile"

    return StubBackend


class TestBuildGraphPopulation:
    """Test that build_graph() correctly populates a BuildGraph from hunter/namer data."""

    def _make_args(self, **overrides):
        defaults = dict(
            filename=["/src/main.cpp"],
            tests=[],
            static=[],
            dynamic=[],
            verbose=0,
            objdir="/tmp/obj",
            CC="gcc",
            CXX="g++",
            CFLAGS="-O2",
            CXXFLAGS="-O2 -std=c++17",
            LD="g++",
            LDFLAGS="",
            file_locking=False,
            serialisetests=False,
            build_only_changed=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _make_hunter(self, sources=None, headers=None, magicflags_map=None):
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        sources = sources or ["/src/main.cpp"]
        hunter.getsources = MagicMock(return_value=sources)
        hunter.required_source_files = MagicMock(side_effect=lambda s: sources)
        headers = headers or ["/src/util.h"]
        hunter.header_dependencies = MagicMock(return_value=headers)
        default_magic = magicflags_map or {}
        hunter.magicflags = MagicMock(return_value=default_magic)
        hunter.macro_state_hash = MagicMock(return_value="abcdef1234567890")
        return hunter

    def test_single_source_produces_compile_and_link_rules(self):
        StubClass = _make_stub_backend_class()
        args = self._make_args()
        hunter = self._make_hunter()
        backend = StubClass(args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) >= 1, "Should have at least one compile rule"
        assert len(link_rules) >= 1, "Should have at least one link rule"

    def test_compile_rule_has_correct_command(self):
        StubClass = _make_stub_backend_class()
        args = self._make_args()
        hunter = self._make_hunter()
        backend = StubClass(args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        assert len(compile_rules) >= 1
        rule = compile_rules[0]
        # Command should contain the compiler and -c flag
        assert any("-c" in arg for arg in rule.command)
        assert rule.inputs[0] == "/src/main.cpp"  # Source is first input

    def test_link_rule_references_object_outputs(self):
        StubClass = _make_stub_backend_class()
        args = self._make_args()
        hunter = self._make_hunter()
        backend = StubClass(args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        # The link rule's inputs should include the compile rule's output
        object_outputs = {r.output for r in compile_rules}
        link_inputs = set(link_rules[0].inputs)
        assert object_outputs & link_inputs, "Link rule should reference compiled objects"

    def test_phony_targets_created(self):
        StubClass = _make_stub_backend_class()
        args = self._make_args()
        hunter = self._make_hunter()
        backend = StubClass(args=args, hunter=hunter)

        graph = backend.build_graph()

        phony_rules = [r for r in graph.rules if r.rule_type == "phony"]
        phony_names = {r.output for r in phony_rules}
        assert "all" in phony_names
        assert "build" in phony_names

    def test_no_sources_produces_empty_graph(self):
        StubClass = _make_stub_backend_class()
        args = self._make_args(filename=[], tests=[], static=[], dynamic=[])
        hunter = self._make_hunter(sources=[])
        backend = StubClass(args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) == 0
        assert len(link_rules) == 0
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_build_backend.py::TestBuildGraphPopulation -v`
Expected: FAIL — `build_graph()` returns an empty BuildGraph (no rules).

**Step 3: Implement build_graph() fully**

Replace the `build_graph()` method in `build_backend.py`. The implementation should:

1. Call `self.hunter.huntsource()` to pre-discover all sources.
2. Iterate `self.args.filename` and `self.args.tests` to create compile rules (one per source, using `hunter.header_dependencies()`, `hunter.magicflags()`, `hunter.macro_state_hash()`, and `namer.object_pathname()`).
3. Create link rules for each executable (using `hunter.required_source_files()` to find all objects needed).
4. Add "build" and "all" phony targets.
5. Handle `self.args.static` and `self.args.dynamic` library targets.

The exact implementation should mirror the logic in `makefile.py:_create_compile_rule_for_source()` (lines 700-760) and `_create_link_rule()` (lines 83-132), but produce `BuildRule` objects instead of `Rule` objects. Consult those methods carefully when implementing.

Key points from `makefile.py` to replicate:
- **Compile command** (lines 728-749): Check `is_c_source()` to select CC vs CXX, combine `CFLAGS`/`CXXFLAGS` with magic flags.
- **Object naming** (lines 716-720): `namer.object_pathname(filename, macro_state_hash, dep_hash)` where `dep_hash = namer.compute_dep_hash(deplist)`.
- **Link command** (lines 127-131): `[linker, "-o", outputname] + object_names + magic_ldflags + [linkerflags]`.
- **Prerequisites** for compile: `[filename] + sorted(header_dependencies)`.
- **Prerequisites** for link: all object file paths.

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_backend.py::TestBuildGraphPopulation -v`
Expected: PASS — all 5 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/build_backend.py src/compiletools/test_build_backend.py
git commit -m "feat: implement build_graph() to populate BuildGraph from hunter/namer"
```

---

## Task 6: Implement MakefileBackend

Wrap the existing `MakefileCreator.write()` and build execution logic as a `BuildBackend` subclass that consumes a `BuildGraph`.

**Files:**
- Create: `src/compiletools/makefile_backend.py`
- Test: `src/compiletools/test_makefile_backend.py`

**Step 1: Write the failing test**

```python
# src/compiletools/test_makefile_backend.py
import io
from unittest.mock import MagicMock, patch

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.makefile_backend import MakefileBackend


class TestMakefileBackendRegistered:
    def test_registered_as_make(self):
        cls = get_backend_class("make")
        assert cls is MakefileBackend

    def test_name(self):
        assert MakefileBackend.name() == "make"

    def test_build_filename(self):
        assert MakefileBackend.build_filename() == "Makefile"


class TestMakefileGenerate:
    def _make_args(self, **overrides):
        from types import SimpleNamespace

        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
            file_locking=False,
            makefilename="Makefile",
            filename=[],
            tests=[],
            static=[],
            dynamic=[],
            CC="gcc",
            CXX="g++",
            CFLAGS="-O2",
            CXXFLAGS="-O2",
            LD="g++",
            LDFLAGS="",
            serialisetests=False,
            build_only_changed=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_generate_writes_makefile_syntax(self):
        """generate() should produce valid Makefile syntax from a BuildGraph."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="obj/foo.o",
            inputs=["foo.cpp", "foo.h"],
            command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
            rule_type="compile",
            order_only_deps=["/tmp/obj"],
        ))
        graph.add_rule(BuildRule(
            output="bin/foo",
            inputs=["obj/foo.o"],
            command=["g++", "-o", "bin/foo", "obj/foo.o"],
            rule_type="link",
        ))
        graph.add_rule(BuildRule(
            output="build",
            inputs=["bin/foo"],
            command=None,
            rule_type="phony",
        ))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".DELETE_ON_ERROR:" in content
        assert "obj/foo.o: foo.cpp foo.h" in content
        assert "| /tmp/obj" in content
        assert "g++ -c foo.cpp -o obj/foo.o" in content
        assert "bin/foo: obj/foo.o" in content
        assert ".PHONY: build" in content

    def test_generate_phony_no_recipe(self):
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".PHONY: all" in content
        assert "all: build" in content
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_makefile_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'compiletools.makefile_backend'`

**Step 3: Write minimal implementation**

```python
# src/compiletools/makefile_backend.py
"""Makefile backend — generates GNU Makefiles from a BuildGraph."""
from __future__ import annotations

import compiletools.filesystem_utils
from compiletools.build_backend import BuildBackend, register_backend
from compiletools.build_graph import BuildGraph


@register_backend
class MakefileBackend(BuildBackend):
    """Generate and execute GNU Makefiles."""

    @staticmethod
    def name() -> str:
        return "make"

    @staticmethod
    def build_filename() -> str:
        return "Makefile"

    def generate(self, graph: BuildGraph, output=None) -> None:
        """Write Makefile from BuildGraph.

        Args:
            graph: The build graph to render.
            output: A file-like object to write to. If None, writes to
                self.args.makefilename using atomic_output_file.
        """
        if output is not None:
            self._write_makefile(graph, output)
        else:
            with compiletools.filesystem_utils.atomic_output_file(
                self.args.makefilename, mode="w", encoding="utf-8"
            ) as f:
                self._write_makefile(graph, f)

    def _write_makefile(self, graph: BuildGraph, f) -> None:
        f.write(f"# Makefile generated by {self.args}\n\n")
        f.write(".DELETE_ON_ERROR:\n\n")
        for rule in graph.rules:
            if rule.rule_type == "phony":
                f.write(f".PHONY: {rule.output}\n")
            line = f"{rule.output}: {' '.join(rule.inputs)}"
            if rule.order_only_deps:
                line += f" | {' '.join(rule.order_only_deps)}"
            f.write(line + "\n")
            if rule.command:
                f.write("\t" + " ".join(rule.command) + "\n")
            f.write("\n")

    def execute(self, target: str = "build") -> None:
        """Run GNU make."""
        import subprocess

        cmd = ["make"]
        if self.args.verbose <= 1:
            cmd.append("-s")
        cmd.extend(["-j", str(getattr(self.args, "parallel", 1))])
        cmd.extend(["-f", self.args.makefilename, target])
        if self.args.verbose >= 1:
            print(" ".join(cmd))
        subprocess.check_call(cmd, universal_newlines=True)
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_makefile_backend.py -v`
Expected: PASS — all 4 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/makefile_backend.py src/compiletools/test_makefile_backend.py
git commit -m "feat: implement MakefileBackend that generates Makefiles from BuildGraph"
```

---

## Task 7: Implement NinjaBackend

Demonstrate the pluggability by implementing a second backend.

**Files:**
- Create: `src/compiletools/ninja_backend.py`
- Test: `src/compiletools/test_ninja_backend.py`

**Step 1: Write the failing test**

```python
# src/compiletools/test_ninja_backend.py
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.ninja_backend import NinjaBackend


class TestNinjaBackendRegistered:
    def test_registered_as_ninja(self):
        cls = get_backend_class("ninja")
        assert cls is NinjaBackend

    def test_name(self):
        assert NinjaBackend.name() == "ninja"

    def test_build_filename(self):
        assert NinjaBackend.build_filename() == "build.ninja"


class TestNinjaGenerate:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
            file_locking=False,
            filename=[],
            tests=[],
            static=[],
            dynamic=[],
            CC="gcc",
            CXX="g++",
            CFLAGS="-O2",
            CXXFLAGS="-O2",
            LD="g++",
            LDFLAGS="",
            serialisetests=False,
            build_only_changed=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_generate_writes_ninja_syntax(self):
        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="obj/foo.o",
            inputs=["foo.cpp", "foo.h"],
            command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
            rule_type="compile",
            order_only_deps=["/tmp/obj"],
        ))
        graph.add_rule(BuildRule(
            output="bin/foo",
            inputs=["obj/foo.o"],
            command=["g++", "-o", "bin/foo", "obj/foo.o"],
            rule_type="link",
        ))
        graph.add_rule(BuildRule(
            output="build",
            inputs=["bin/foo"],
            command=None,
            rule_type="phony",
        ))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Ninja uses "build <output>: <rule> <inputs>" syntax
        assert "build obj/foo.o: compile_cmd foo.cpp" in content
        assert "build bin/foo: link_cmd obj/foo.o" in content
        # Ninja uses "build <alias>: phony <deps>" for phony targets
        assert "build build: phony bin/foo" in content
        # Order-only deps use || in Ninja
        assert "|| /tmp/obj" in content

    def test_ninja_rule_definitions(self):
        """Ninja requires rule definitions (rule compile_cmd / rule link_cmd)."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="obj/foo.o", inputs=["foo.cpp"],
            command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
            rule_type="compile",
        ))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Should define a Ninja rule with command variable
        assert "rule compile_cmd" in content
        assert "command = $cmd" in content
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_ninja_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'compiletools.ninja_backend'`

**Step 3: Write minimal implementation**

```python
# src/compiletools/ninja_backend.py
"""Ninja backend — generates build.ninja files from a BuildGraph."""
from __future__ import annotations

import compiletools.filesystem_utils
from compiletools.build_backend import BuildBackend, register_backend
from compiletools.build_graph import BuildGraph


@register_backend
class NinjaBackend(BuildBackend):
    """Generate and execute Ninja build files."""

    @staticmethod
    def name() -> str:
        return "ninja"

    @staticmethod
    def build_filename() -> str:
        return "build.ninja"

    def generate(self, graph: BuildGraph, output=None) -> None:
        if output is not None:
            self._write_ninja(graph, output)
        else:
            filename = getattr(self.args, "ninja_filename", "build.ninja")
            with compiletools.filesystem_utils.atomic_output_file(
                filename, mode="w", encoding="utf-8"
            ) as f:
                self._write_ninja(graph, f)

    def _write_ninja(self, graph: BuildGraph, f) -> None:
        f.write("# build.ninja generated by compiletools\n\n")

        # Emit generic rules that use $cmd variable
        rule_types_seen = set()
        for rule in graph.rules:
            if rule.command and rule.rule_type not in rule_types_seen:
                ninja_rule = f"{rule.rule_type}_cmd"
                f.write(f"rule {ninja_rule}\n")
                f.write("  command = $cmd\n")
                if rule.rule_type == "compile":
                    f.write("  description = Compiling $out\n")
                elif rule.rule_type == "link":
                    f.write("  description = Linking $out\n")
                else:
                    f.write(f"  description = {rule.rule_type} $out\n")
                f.write("\n")
                rule_types_seen.add(rule.rule_type)

        # Emit build statements
        for rule in graph.rules:
            if rule.rule_type == "phony":
                f.write(f"build {rule.output}: phony {' '.join(rule.inputs)}\n")
            elif rule.command:
                ninja_rule = f"{rule.rule_type}_cmd"
                # First input is the primary source, rest are implicit deps
                primary = rule.inputs[0] if rule.inputs else ""
                implicit = rule.inputs[1:] if len(rule.inputs) > 1 else []
                line = f"build {rule.output}: {ninja_rule} {primary}"
                if implicit:
                    line += f" | {' '.join(implicit)}"
                if rule.order_only_deps:
                    line += f" || {' '.join(rule.order_only_deps)}"
                f.write(line + "\n")
                f.write(f"  cmd = {' '.join(rule.command)}\n")
            f.write("\n")

    def execute(self, target: str = "build") -> None:
        import subprocess

        filename = getattr(self.args, "ninja_filename", "build.ninja")
        cmd = ["ninja", "-f", filename, target]
        if self.args.verbose >= 1:
            cmd.append("-v")
        subprocess.check_call(cmd, universal_newlines=True)
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_ninja_backend.py -v`
Expected: PASS — all 4 tests pass

**Step 5: Commit**

```bash
git add src/compiletools/ninja_backend.py src/compiletools/test_ninja_backend.py
git commit -m "feat: implement NinjaBackend as second pluggable backend"
```

---

## Task 8: Add --backend CLI Argument to cake.py

Wire up the backend selection into `cake.py`.

**Files:**
- Modify: `src/compiletools/cake.py`
- Test: `src/compiletools/test_cake.py`

**Step 1: Write the failing test**

Append to `test_cake.py` (or create a focused test file `test_cake_backend.py`):

```python
# src/compiletools/test_cake_backend.py
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import compiletools.makefile_backend  # ensure registered
from compiletools.build_backend import get_backend_class, available_backends


class TestBackendCLIArg:
    def test_make_is_default(self):
        cls = get_backend_class("make")
        assert cls.name() == "make"

    def test_available_includes_make_and_ninja(self):
        import compiletools.ninja_backend  # ensure registered

        backends = available_backends()
        assert "make" in backends
        assert "ninja" in backends
```

**Step 2: Run test to verify it passes**

Run: `pytest src/compiletools/test_cake_backend.py -v`
Expected: PASS (these just verify registration).

**Step 3: Modify cake.py**

In `Cake.add_arguments()`, add:

```python
cap.add(
    "--backend",
    default="make",
    choices=["make"],  # Will grow as backends are registered
    help="Build system backend to use (default: make).",
)
```

In `Cake.process()`, after the existing `self._callmakefile()` call, add backend dispatch logic. For now, the `--backend=make` path calls the existing `_callmakefile()` unchanged, preserving backward compatibility.

**Important:** Do NOT remove or modify `_callmakefile()` in this task. The migration from `MakefileCreator` to `MakefileBackend` is a separate future task. This task only adds the CLI plumbing.

**Step 4: Run existing tests to verify nothing is broken**

Run: `pytest src/compiletools/test_cake.py -v`
Expected: PASS — existing tests still work

**Step 5: Commit**

```bash
git add src/compiletools/cake.py src/compiletools/test_cake_backend.py
git commit -m "feat: add --backend CLI argument to cake.py (defaults to make)"
```

---

## Task 9: Add BuildGraph Filter Helpers

Add utility methods to BuildGraph for the `--build-only-changed` feature and test/clean rule filtering, which any backend will need.

**Files:**
- Modify: `src/compiletools/build_graph.py`
- Modify: `src/compiletools/test_build_graph.py`

**Step 1: Write the failing tests**

Append to `test_build_graph.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_build_graph.py::TestBuildGraphFiltering -v`
Expected: FAIL — `AttributeError: 'BuildGraph' object has no attribute 'rules_by_type'`

**Step 3: Implement the methods**

Add to `BuildGraph` in `build_graph.py`:

```python
def rules_by_type(self, rule_type: str) -> list[BuildRule]:
    """Return all rules matching the given type."""
    return [r for r in self._rules.values() if r.rule_type == rule_type]

@property
def outputs(self) -> set[str]:
    """Return the set of all output paths."""
    return set(self._rules.keys())
```

**Step 4: Run test to verify it passes**

Run: `pytest src/compiletools/test_build_graph.py -v`
Expected: PASS — all tests pass

**Step 5: Commit**

```bash
git add src/compiletools/build_graph.py src/compiletools/test_build_graph.py
git commit -m "feat: add rules_by_type() and outputs property to BuildGraph"
```

---

## Task 10: Integration Test — MakefileBackend End-to-End

Write an integration test that goes from real source files through Hunter to BuildGraph to Makefile output. Uses sample files from `src/compiletools/samples/`.

**Files:**
- Create: `src/compiletools/test_backend_integration.py`

**Step 1: Write the integration test**

```python
# src/compiletools/test_backend_integration.py
"""Integration tests verifying the full pipeline:
   source files -> Hunter -> BuildGraph -> MakefileBackend -> Makefile text
"""
import io
import os
import pytest

import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.makefile_backend  # ensure registered
import compiletools.testhelper as uth
from compiletools.build_backend import get_backend_class
from compiletools.test_base import BaseCompileToolsTestCase


class TestMakefileBackendIntegration(BaseCompileToolsTestCase):
    """Full pipeline integration test using real sample files."""

    @pytest.fixture(autouse=True)
    def setup_samples(self, tmp_path):
        self.samples = uth.samplesdir()

    def _make_args_for_sample(self, sample_dir, source_files, **overrides):
        """Create args suitable for building files in a sample directory."""
        args = uth.create_temp_config()
        args.filename = [os.path.join(sample_dir, f) for f in source_files]
        args.tests = []
        args.static = []
        args.dynamic = []
        args.verbose = 0
        args.objdir = os.path.join(sample_dir, "obj")
        args.file_locking = False
        args.serialisetests = False
        args.build_only_changed = None
        args.makefilename = os.path.join(sample_dir, "Makefile")
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def test_simple_source_produces_valid_makefile(self):
        """Verify that a simple .cpp file produces a Makefile with compile + link rules."""
        # Find a simple sample (just needs a .cpp with main())
        simple_dir = os.path.join(self.samples, "simple")
        if not os.path.isdir(simple_dir):
            pytest.skip("simple sample directory not found")

        source_files = [f for f in os.listdir(simple_dir) if f.endswith((".cpp", ".C"))]
        if not source_files:
            pytest.skip("No source files in simple sample")

        args = self._make_args_for_sample(simple_dir, source_files[:1])
        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        BackendClass = get_backend_class("make")
        backend = BackendClass(args=args, hunter=hunter)
        graph = backend.build_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Should have .DELETE_ON_ERROR
        assert ".DELETE_ON_ERROR:" in content
        # Should have at least one compile rule with -c
        assert "-c" in content
        # Should have at least one link rule with -o
        assert "-o" in content
```

**Step 2: Run test to verify it fails**

Run: `pytest src/compiletools/test_backend_integration.py -v`
Expected: FAIL (build_graph() doesn't produce real rules yet — depends on Task 5 being complete)

**Step 3: Fix any issues found during integration**

This is the "make the test green" step. Debug and fix any mismatches between the mocked unit tests and the real Hunter/Namer behavior. Common issues:
- Path handling (absolute vs relative)
- String types (stringzilla `Str` vs Python `str`)
- Missing args attributes

**Step 4: Run full test suite**

Run: `pytest src/compiletools/ -v --timeout=60`
Expected: PASS — all existing tests still pass, integration test passes

**Step 5: Commit**

```bash
git add src/compiletools/test_backend_integration.py
git commit -m "test: add end-to-end integration test for MakefileBackend pipeline"
```

---

## Task 11: Run Full Test Suite and Fix Regressions

Verify nothing is broken across the entire codebase.

**Step 1: Run ruff linting**

Run: `ruff check src/compiletools/build_graph.py src/compiletools/build_backend.py src/compiletools/makefile_backend.py src/compiletools/ninja_backend.py`
Expected: No errors

**Step 2: Run ruff formatting**

Run: `ruff format src/compiletools/build_graph.py src/compiletools/build_backend.py src/compiletools/makefile_backend.py src/compiletools/ninja_backend.py`

**Step 3: Run full test suite**

Run: `pytest src/compiletools/ -v`
Expected: All tests pass

**Step 4: Fix any issues**

Address any failures found in steps 1-3.

**Step 5: Commit**

```bash
git add -A
git commit -m "chore: fix lint and formatting for new backend modules"
```

---

## Summary of New Files

| File | Purpose |
|------|---------|
| `src/compiletools/build_graph.py` | `BuildRule` + `BuildGraph` — backend-agnostic IR |
| `src/compiletools/build_backend.py` | `BuildBackend` ABC + registry functions |
| `src/compiletools/makefile_backend.py` | `MakefileBackend` — generates GNU Makefiles |
| `src/compiletools/ninja_backend.py` | `NinjaBackend` — generates Ninja build files |
| `src/compiletools/test_build_graph.py` | Tests for BuildRule and BuildGraph |
| `src/compiletools/test_build_backend.py` | Tests for ABC contract, registry, and build_graph() |
| `src/compiletools/test_makefile_backend.py` | Tests for Makefile generation |
| `src/compiletools/test_ninja_backend.py` | Tests for Ninja generation |
| `src/compiletools/test_cake_backend.py` | Tests for --backend CLI integration |
| `src/compiletools/test_backend_integration.py` | End-to-end integration tests |

## Files Modified

| File | Change |
|------|--------|
| `src/compiletools/cake.py` | Add `--backend` CLI argument |

## Files NOT Modified

| File | Reason |
|------|--------|
| `src/compiletools/makefile.py` | Kept as-is. `MakefileCreator` continues to work unchanged. Migration from `MakefileCreator` to `MakefileBackend` is a future task after this foundation is proven. |
| `src/compiletools/hunter.py` | No changes needed — its API is already backend-agnostic. |
| `src/compiletools/namer.py` | No changes needed — its API is already backend-agnostic. |

## Future Work (Not In This Plan)

1. ~~**Migrate cake.py** from `MakefileCreator` to `MakefileBackend`~~ — DONE: `cake.py:_call_backend()` dispatches to the selected backend.
2. **Add file-locking support** to `MakefileBackend.generate()` (lock wrapping).
3. ~~**Add CMakeBackend** for IDE integration~~ — DONE: `cmake_backend.py` implemented.
4. **Add --build-only-changed** support in `BuildGraph.filter_changed()`.
5. ~~**Add test/clean rule generation** to `build_graph()`~~ — DONE: test support added to all backends via `_run_tests()`.
6. **Remove `MakefileCreator`** after migration is complete and stable.

Additional backends implemented beyond original plan: Bazel (`bazel_backend.py`), Shake (`shake_backend.py`), Tup (`tup_backend.py`).
