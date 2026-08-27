"""Stage 7 single-writer / single-channel AST lints.

Four independent slices, one lint each, sharing the same machinery. Each
lint answers "who is allowed to write this piece of ambient state" with an
``ast`` scan over every non-test production module (``src/compiletools/
*.py`` excluding ``test_*`` files -- ``testhelper.py`` and ``conftest.py``
ARE scanned; ``test_base.py`` starts with ``test_`` and is not; the glob is
non-recursive, so the ``otel/`` subpackage is unscanned -- verified free of
tracked-attr writes at landing time, same scope as the template lint).
Exemptions are live-checked at BOTH granularities: an exempt file that
stops matching anything fails, and a pinned owner FUNCTION that stops
containing any site fails (``_assert_owners_live``) -- an entry that goes
stale must be dropped instead of silently widening the lint, mirroring
``test_build_apply.py::test_no_module_reaches_past_the_build_state_accessors``
(the template this file follows) and ``test_cas_dir_resolver_contract.py``'s
``_RESOLVER_EXEMPT`` pattern.

Every slice already re-verified that its underlying code is single-writer
(or a documented, deliberately-bounded multi-writer set); what was missing
was a machine check pinning that fact so a future edit cannot silently
regress it. None of these lints changed any executable production
behaviour -- only prose/docstrings/comments were touched alongside them.

Shared write-detection ("does this AST node WRITE one of these attribute
names") deliberately leaves reads alone: an owner-pinned lint that also
banned reads would force every consumer to funnel through an accessor
function that, for three of the four slices, does not need to exist (the
attribute is legitimately read from several call sites; only the write
side is a contract worth pinning). Slice S1 is the one exception -- its
target is banned from ALL access (read or write) outside its owning file,
because the pre-existing symlinked-source crash path
(``test_function_like_macros.py``) depends on nothing outside
``magicflags.py`` ever touching the store directly.
"""

from __future__ import annotations

import ast
import pathlib

_SRCDIR = pathlib.Path(__file__).parent


def _production_trees() -> dict[str, ast.Module]:
    """{filename: ast tree} for every non-test production module."""
    out = {}
    for path in sorted(_SRCDIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        out[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return out


def _function_spans(tree: ast.Module, names: frozenset[str]) -> list[range]:
    """Inclusive line-number ranges of every function/async function in
    *tree* whose name is in *names*. A write inside a NESTED function
    still falls inside its outer owner's span (e.g. a helper closure
    defined inside the pinned owner).

    Spans are matched by bare function NAME, not class-qualified: a file
    with several same-named methods (magicflags.py has five ``__init__``
    and three ``get_structured_data``) satisfies an owner pin through ANY
    of them. The docstrings below therefore promise function names, not
    ``Class.method`` identities.
    """
    return [
        range(node.lineno, (node.end_lineno or node.lineno) + 1)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]


def _assert_owners_live(filename: str, tree: ast.Module, owners: frozenset[str], lines: list[int], what: str) -> None:
    """Every pinned owner function must still contain at least one site.

    The file-level stale checks catch a whole exemption going dead; this
    is the per-function arm — an owner entry whose function no longer
    writes/calls anything silently widens the lint exactly like a stale
    file exemption would (measured: deleting one of two writers in a file
    leaves the file-level checks green with a dead owner pinned).
    """
    for owner in sorted(owners):
        spans = _function_spans(tree, frozenset({owner}))
        if not any(ln in span for ln in lines for span in spans):
            raise AssertionError(f"{filename}: pinned owner {owner!r} no longer contains any {what}; drop it")


_MUTATORS = frozenset({"update", "add", "clear", "pop", "popitem", "setdefault", "discard"})


def _attr_store_target_lineno(target: ast.expr, attr_names: frozenset[str]) -> int | None:
    """lineno if *target* is a store-shaped write to one of *attr_names*,
    recursing into tuple/list unpacking. None otherwise."""
    if isinstance(target, ast.Attribute) and target.attr in attr_names:
        return target.lineno
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr in attr_names
    ):
        return target.lineno
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value in attr_names
    ):
        # __dict__/vars() spellings of the same store: obj.__dict__["name"] = v
        # and vars(obj)["name"] = v. Idiomatic enough in this codebase
        # (hunter/headerdeps use __dict__ elsewhere) to be worth an arm.
        base = target.value
        if (isinstance(base, ast.Attribute) and base.attr == "__dict__") or (
            isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "vars"
        ):
            return target.lineno
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            found = _attr_store_target_lineno(elt, attr_names)
            if found is not None:
                return found
    return None


def _attr_store_receiver(target: ast.expr) -> ast.expr | None:
    """Return the receiver expression of a store target shaped like
    ``x.attr = ...`` or ``x.attr[k] = ...`` -- i.e. the ``x``. None when
    *target* isn't directly one of those two shapes (e.g. it's the
    Tuple/List container itself in an unpacking target; the self-skip
    check simply doesn't fire for that nested case)."""
    attr_node = target.value if isinstance(target, ast.Subscript) else target
    if isinstance(attr_node, ast.Attribute):
        return attr_node.value
    return None


def _write_sites(tree: ast.Module, attr_names: frozenset[str], *, skip_self: bool = False):
    """Yield lineno for every WRITE-shaped use of *attr_names*:
    - Attribute store targets (plain, annotated, augmented, tuple-unpack)
    - Subscript stores whose base is such an Attribute (``x.attr[k] = v``)
    - ``__dict__``/``vars()`` subscript stores with a Constant key
    - mutator method calls on such an Attribute (``x.attr.update(...)``)
    - ``setattr(obj, "<name>", ...)`` and ``__setattr__`` calls with a
      Constant name

    Reads are deliberately free. Documented blind spots:

    * ``setattr`` with a VARIABLE name is invisible. Production has eight
      such sites; two touch names a slice tracks: ``apptools._strip_quotes``
      (a namespace-wide cosmetic unquote pass rewriting every string attr
      in place — including ``variant`` and the ``cas_*dir`` values it does
      NOT derive) and ``resolve_cas_directory_arguments``'s nested
      ``_resolve`` helper (inside the sanctioned owner's span, so exempt
      even if it were visible). A blanket variable-name ban would
      false-positive on the six unrelated sites, so the blind spot is
      documented rather than closed.
    * Aliasing (``d = obj.attr; d[k] = v``) is invisible — zero production
      instances today, noted rather than chased with data flow.
    * ``_MUTATORS`` covers dict/set mutators only; every tracked attr is a
      dict / frozenset / str / BuildGraph today, so list mutators
      (``append``/``extend``/...) are deliberately not matched.

    When *skip_self* is set, a store whose receiver is literally named
    ``self`` is excluded -- used only where the attribute name is common
    enough (``variant``) that an unrelated class's ``self.variant = ...``
    must not count as a hit.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                lineno = _attr_store_target_lineno(target, attr_names)
                if lineno is None:
                    continue
                receiver = _attr_store_receiver(target)
                if skip_self and isinstance(receiver, ast.Name) and receiver.id == "self":
                    continue
                yield lineno
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _MUTATORS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in attr_names
            ):
                if skip_self and isinstance(func.value.value, ast.Name) and func.value.value.id == "self":
                    continue
                yield node.lineno
            elif (
                isinstance(func, ast.Name)
                and func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in attr_names
            ):
                yield node.lineno
            elif isinstance(func, ast.Attribute) and func.attr == "__setattr__":
                # object.__setattr__(obj, "name", v) carries the name in
                # args[1]; obj.__setattr__("name", v) carries it in
                # args[0]. Both are writes by definition.
                for arg in node.args[:2]:
                    if isinstance(arg, ast.Constant) and arg.value in attr_names:
                        yield node.lineno
                        break


def _class_body_declarations(tree: ast.Module, attr_names: frozenset[str]):
    """Yield lineno for bare-Name assignments directly inside a ClassDef
    body (class-level attribute declarations, e.g. ``_foo: T = default``)
    whose target is in *attr_names*. These are NOT instance attribute
    stores (the target is a plain Name, not ``self.<attr>``), so
    ``_write_sites`` cannot see them -- a separate arm for the class-scope
    declaration form."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id in attr_names:
                yield stmt.lineno
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id in attr_names:
                        yield stmt.lineno


def test_variant_and_cas_dirs_have_exactly_two_writers():
    """Lint: ``args.variant`` and the four ``cas_*dir`` attrs have exactly
    two sanctioned production writers -- ``apptools_argparse.
    resolve_cas_directory_arguments`` (the raw-argparse resolver,
    including its nested ``_resolve`` helper's variable-attr ``setattr``
    loop over the four cas kinds) and ``build_apply.populate_args`` (the
    BuildState-driven stash). Both already call the same three
    ``build_state`` implementations (``canonical_variant_name`` /
    ``cas_dir_name`` / ``anchor_cas_dir_to_gitroot``); this lint is the
    third machine-checked guard beside ``test_cas_dir_resolution_parity.py``
    and ``test_cas_dir_resolver_contract.py``.

    ``variant`` is matched only through ``_write_sites``'s Attribute-store
    and ``setattr``-Constant arms -- never a blanket "any Constant equal to
    the string 'variant'" scan, since that string is ubiquitous (docstrings,
    conf keys, log messages, local variable names) and would drown the
    lint in false positives the way it would not for a rarer name.

    Self-receiver stores are skipped (``skip_self=True``): ``self.variant
    = ...`` or ``self.cas_objdir = ...`` on an unrelated class is a
    different object, not this channel.

    Documented blind spot: a ``setattr(obj, some_variable_name, ...)``
    with a non-Constant name is invisible to this scan, and two such
    production sites DO touch these names -- see ``_write_sites``'s
    docstring for the honest inventory (``apptools._strip_quotes``'s
    namespace-wide unquote pass, and the sanctioned resolver's nested
    ``_resolve``). "Exactly two writers" therefore means the two
    functions that DERIVE these values, which is the contract worth
    pinning.

    Exempt files and pinned owner functions are checked live: a stale
    entry at either granularity fails instead of silently widening the
    lint.
    """
    attr_names = frozenset({"variant", "cas_objdir", "cas_pchdir", "cas_pcmdir", "cas_exedir"})
    exempt = {
        "build_apply.py": frozenset({"populate_args"}),
        "apptools_argparse.py": frozenset({"resolve_cas_directory_arguments"}),
    }

    trees = _production_trees()
    sites: dict[str, list[int]] = {}
    for name, tree in trees.items():
        lines = list(_write_sites(tree, attr_names, skip_self=True))
        if lines:
            sites[name] = lines

    unexpected = {name: lns for name, lns in sites.items() if name not in exempt}
    assert not unexpected, (
        "variant/cas_*dir have exactly two sanctioned writers "
        "(apptools_argparse.resolve_cas_directory_arguments, build_apply.populate_args); "
        f"found additional write sites: {unexpected}"
    )

    stale = [name for name in exempt if name not in sites]
    assert not stale, f"exemption no longer needed, drop it: {stale}"

    for name, owners in exempt.items():
        spans = _function_spans(trees[name], owners)
        strays = [ln for ln in sites[name] if not any(ln in span for span in spans)]
        assert not strays, f"{name} writes variant/cas_*dir outside {sorted(owners)} at lines {strays}"
        _assert_owners_live(name, trees[name], owners, sites[name], "variant/cas_*dir write")


def test_final_macro_states_is_owned_by_magicflags():
    """Lint: ``MagicFlagsBase._final_macro_states`` stays instance-owned
    (context-owned was considered and rejected -- the store's lifetime is
    exactly the magicflags instance's, and moving it would touch the
    documented symlinked-source crash path pinned by
    ``test_function_like_macros.py``, lines 750-809, which this lint must
    not mask). The guard has two parts:

    1. FULL access ban (read or write, dot-access or the bare string)
       outside ``magicflags.py`` -- mirrors
       ``test_no_module_reaches_past_the_build_state_accessors``'s
       predicate exactly: an ``ast.Attribute`` named ``_final_macro_states``
       (no self-skip -- even a self-receiver outside the file is a
       violation, since there IS no legitimate outside owner) plus an
       ``ast.Constant`` equal to the bare name (catches a hypothetical
       ``getattr``/``vars()``/``__dict__`` spelling in one predicate,
       the same subsumption argument the template lint documents).
    2. Inside ``magicflags.py``, WRITES are pinned to four owning
       function NAMES (``__init__`` declares the empty dict; ``_parse``
       rebuilds it per convergence pass; ``_finalize_and_cache_result``
       and ``get_structured_data`` -- today the CppMagicFlags one --
       cache the settled/initial state; see ``_function_spans`` for the
       name-granularity caveat). Reads -- the two public accessors
       (``get_final_macro_state_key`` / ``get_final_macro_state_hash``),
       ``_parse``'s own lookups, and ``_check_cache``'s cache-hit rebind
       of ``defined_macros`` FROM the store -- stay free.

    Exempt files are checked live: a stale entry here fails instead of
    silently widening the lint.
    """
    name = "_final_macro_states"
    write_owners = frozenset({"__init__", "_parse", "_finalize_and_cache_result", "get_structured_data"})
    exempt_ban = {"magicflags.py": "declaring class + the three writers + accessors"}

    def access_sites(tree):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == name) or (
                isinstance(node, ast.Constant) and node.value == name
            ):
                yield node.lineno

    trees = _production_trees()
    sites: dict[str, list[int]] = {}
    for fname, tree in trees.items():
        hits = list(access_sites(tree))
        if hits:
            sites[fname] = hits

    unexpected = {fname: lns for fname, lns in sites.items() if fname not in exempt_ban}
    assert not unexpected, (
        f"{name} is owned by MagicFlagsBase; reach it through get_final_macro_state_key / "
        f"get_final_macro_state_hash instead of touching it directly outside magicflags.py: {unexpected}"
    )

    stale = [fname for fname in exempt_ban if fname not in sites]
    assert not stale, f"exemption no longer needed, drop it: {stale}"

    magicflags_tree = trees["magicflags.py"]
    write_lines = sorted(set(_write_sites(magicflags_tree, frozenset({name}))))
    # Unlike the other three lints, this arm's input comes from its own
    # access scan above only for the ban half; the write pinning below
    # depends on _write_sites — if that matcher breaks, write_lines goes
    # empty and the stray check would pass vacuously.
    assert write_lines, f"no {name} writes found in magicflags.py -- the shared write matcher broke"
    spans = _function_spans(magicflags_tree, write_owners)
    strays = [ln for ln in write_lines if not any(ln in span for span in spans)]
    assert not strays, f"magicflags.py writes {name} outside {sorted(write_owners)} at lines {strays}"
    _assert_owners_live("magicflags.py", magicflags_tree, write_owners, write_lines, f"{name} write")


def test_analyzer_args_channel_has_pinned_writers_and_callers():
    """Lint: ``BuildContext.analyzer_args`` (the ambient channel
    ``file_analyzer.analyze_file`` reads its args/context through) has a
    pinned writer set AND a pinned caller set for ``set_analyzer_args``,
    the single sanctioned way to write it.

    Full removal (threading ``args``/``context`` explicitly into
    ``analyze_file`` across every one of its ~15 production call sites)
    was rejected as unlandable-green at this size; a move out of the two
    lazy ``hunter.py`` constructors would break their documented
    "no particular caller required" contract. This lint pins the current,
    already-minimal shape instead of re-deriving it by inspection.

    Two independent arms:

    1. WRITES of ``context.analyzer_args`` -- ``set_analyzer_args`` itself
       (the sanctioned setter), ``BuildContext.__init__`` (the declaration,
       ``None``), and ``fetch.py``'s ``_fixpoint_scan`` (the save/restore
       ``finally`` block documented in ``src/compiletools/CLAUDE.md``'s
       "Why ``_augmented_headerdeps`` no longer deep-copies args" section --
       untouched by this slice, still the correct mitigation).
    2. CALLS to ``set_analyzer_args`` -- every production constructor/
       function that establishes the channel before a walk that needs it:
       ``HeaderDepsBase.__init__``, ``MagicFlagsBase.__init__``,
       ``FindTargets.__init__`` and
       ``_warn_about_executables_in_library_slots``, ``fetch.py``'s
       ``extract_git_externals`` and ``extract_git_allow_protocols``, and
       ``hunter.py``'s two LAZY sites (``_file_analysis_result`` /
       ``_module_interface_registry``, both guarded by
       ``context.analyzer_args is None``).

    Known limit, by design: the caller arm pins WHO calls
    ``set_analyzer_args``, not WHAT they pass — a pinned caller handed
    the wrong args/context, or a second call added inside an
    already-pinned function, is invisible here (the runtime contract is
    covered by the walks' own tests).

    Exempt files/functions are checked live at both granularities: a
    stale entry fails instead of silently widening the lint.
    """
    write_owners = {
        "file_analyzer.py": frozenset({"set_analyzer_args"}),
        "build_context.py": frozenset({"__init__"}),
        "fetch.py": frozenset({"_fixpoint_scan"}),
    }
    call_owners = {
        "headerdeps.py": frozenset({"__init__"}),
        "magicflags.py": frozenset({"__init__"}),
        "findtargets.py": frozenset({"__init__", "_warn_about_executables_in_library_slots"}),
        "fetch.py": frozenset({"extract_git_externals", "extract_git_allow_protocols"}),
        "hunter.py": frozenset({"_file_analysis_result", "_module_interface_registry"}),
    }

    trees = _production_trees()

    write_sites: dict[str, list[int]] = {}
    for name, tree in trees.items():
        lines = list(_write_sites(tree, frozenset({"analyzer_args"})))
        if lines:
            write_sites[name] = lines

    unexpected_writes = {name: lns for name, lns in write_sites.items() if name not in write_owners}
    assert not unexpected_writes, (
        f"analyzer_args has pinned writers {sorted(write_owners)}; found more: {unexpected_writes}"
    )

    stale_writers = [name for name in write_owners if name not in write_sites]
    assert not stale_writers, f"writer exemption no longer needed, drop it: {stale_writers}"

    for name, owners in write_owners.items():
        spans = _function_spans(trees[name], owners)
        strays = [ln for ln in write_sites[name] if not any(ln in span for span in spans)]
        assert not strays, f"{name} writes analyzer_args outside {sorted(owners)} at lines {strays}"
        _assert_owners_live(name, trees[name], owners, write_sites[name], "analyzer_args write")

    def call_sites(tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "set_analyzer_args") or (
                isinstance(func, ast.Attribute) and func.attr == "set_analyzer_args"
            ):
                yield node.lineno

    calls: dict[str, list[int]] = {}
    for name, tree in trees.items():
        lines = list(call_sites(tree))
        if lines:
            calls[name] = lines

    unexpected_calls = {name: lns for name, lns in calls.items() if name not in call_owners}
    assert not unexpected_calls, (
        f"set_analyzer_args has pinned callers {sorted(call_owners)}; found more: {unexpected_calls}"
    )

    stale_callers = [name for name in call_owners if name not in calls]
    assert not stale_callers, f"caller exemption no longer needed, drop it: {stale_callers}"

    for name, owners in call_owners.items():
        spans = _function_spans(trees[name], owners)
        strays = [ln for ln in calls[name] if not any(ln in span for span in spans)]
        assert not strays, f"{name} calls set_analyzer_args outside {sorted(owners)} at lines {strays}"
        _assert_owners_live(name, trees[name], owners, calls[name], "set_analyzer_args call")


def test_backend_plan_channels_have_pinned_writers():
    """Lint: the C++20-modules planning state on ``BuildBackend``
    (``_module_iface_obj`` / ``_module_iface_pcm`` / ``_module_iface_gcm``
    / ``_module_impl_obj`` / ``_header_unit_artefact`` /
    ``_gcc_header_unit_resolved``) plus ``_graph`` has a pinned writer set
    across the base class and its ``ShakeBackend`` (trace_backend.py)
    subclass.

    A full ``PlanState`` value-object refactor (option a) was rejected:
    reads cross four subclass files (bazel/cmake/ninja/trace), Stage 4
    fixtures plant these attrs on backends directly in test files
    (unscanned by this lint, but a PlanState would force rewriting every
    one), and the class-level ``MappingProxyType`` totality defaults are a
    deliberate, documented design a refactor would have to reproduce for
    zero writer-set reduction -- the writer set is already minimal. This
    lint pins it instead of re-deriving it by inspection.

    Two arms:

    1. Class-scope DECLARATIONS -- the ``MappingProxyType``/``frozenset()``
       defaults are bare ``Name`` targets directly inside the ``ClassDef``
       body (not ``self.<attr>`` stores, so ``_write_sites`` cannot see
       them; ``_class_body_declarations`` is the separate arm for this
       shape). Allowed only in ``build_backend.py``.
    2. Instance-attribute WRITES (``_write_sites``, no self-skip --
       unlike the S3 lint, receiver identity is irrelevant here because
       these attribute names are namespaced enough that an unrelated
       ``self._graph = ...`` would itself be worth catching) pinned to:
       ``build_backend.py``'s ``__init__``, ``_plan_module_prepass``,
       ``_plan_header_unit_prepass``, ``_create_header_unit_precompile_rule``,
       ``_apply_build_only_changed``; ``trace_backend.py``'s ``generate``
       (the ``_graph`` planning write -- ShakeBackend.__init__'s redundant
       re-declaration after ``super().__init__()`` was found by this
       slice's inventory and DELETED, so the subclass has exactly one
       writer); and ``testhelper.py``'s ``ShakeBackendTestContext``
       (``backend._graph = graph``, the test-fixture planter, pinned to
       its function like every other owner).

    Exempt files/functions are checked live at both granularities: a
    stale entry fails instead of silently widening the lint.
    """
    attr_names = frozenset(
        {
            "_module_iface_obj",
            "_module_iface_pcm",
            "_module_iface_gcm",
            "_module_impl_obj",
            "_header_unit_artefact",
            "_gcc_header_unit_resolved",
            "_graph",
        }
    )
    owners = {
        "build_backend.py": frozenset(
            {
                "__init__",
                "_plan_module_prepass",
                "_plan_header_unit_prepass",
                "_create_header_unit_precompile_rule",
                "_apply_build_only_changed",
            }
        ),
        "trace_backend.py": frozenset({"generate"}),
        "testhelper.py": frozenset({"ShakeBackendTestContext"}),
    }

    trees = _production_trees()

    instance_sites: dict[str, list[int]] = {}
    class_sites: dict[str, list[int]] = {}
    for name, tree in trees.items():
        instance_lines = list(_write_sites(tree, attr_names))
        if instance_lines:
            instance_sites[name] = instance_lines
        class_lines = list(_class_body_declarations(tree, attr_names))
        if class_lines:
            class_sites[name] = class_lines

    stray_class_files = {name: lns for name, lns in class_sites.items() if name != "build_backend.py"}
    assert not stray_class_files, (
        f"class-scope declarations of {sorted(attr_names)} only belong in build_backend.py: {stray_class_files}"
    )
    assert class_sites.get("build_backend.py"), (
        "expected class-scope defaults in build_backend.py; none found -- lint's own oracle broke"
    )

    unexpected = {name: lns for name, lns in instance_sites.items() if name not in owners}
    assert not unexpected, (
        f"writer set for {sorted(attr_names)} is pinned to {sorted(owners)}; found more: {unexpected}"
    )

    stale = [name for name in owners if name not in instance_sites]
    assert not stale, f"exemption no longer needed, drop it: {stale}"

    for name, fn_owners in owners.items():
        spans = _function_spans(trees[name], fn_owners)
        strays = [ln for ln in instance_sites[name] if not any(ln in span for span in spans)]
        assert not strays, f"{name} writes {sorted(attr_names)} outside {sorted(fn_owners)} at lines {strays}"
        _assert_owners_live(name, trees[name], fn_owners, instance_sites[name], "plan-channel write")
