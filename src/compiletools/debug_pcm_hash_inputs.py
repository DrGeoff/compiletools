"""Dump the inputs that feed ``_pcm_command_hash`` for a given source file.

Diagnostic helper for cmd_hash-drift reports against C++20 module
precompiles. When two back-to-back ct-cake invocations land a BMI at
different ``<cas-pcmdir>/<cmd_hash>/`` subdirs on byte-identical source,
running this tool in each invocation and diffing the JSON output names
the volatile input directly — closing the loop the bug reporter cannot
easily close from outside the build.

Output is a single JSON object on stdout with the six fields that
``_pcm_command_hash`` consumes for the given stage:

* ``compiler_identity`` — ``realpath|size|mtime_ns`` of ``args.CXX``,
  canonicalised against the gitroot anchor.
* ``cxx_command`` — canonicalised ``args.CXX``.
* ``cxxflags_tokens`` — ``args.flags.hash_relevant("cxx")`` (frozen
  per-build, set once at parseargs end).
* ``magic_cpp_flags`` / ``magic_cxx_flags`` — per-file flags from
  ``hunter.magicflags(source)`` after the same canonicalisation the
  hash function applies.
* ``transitive_content_hash`` — ``"<source_hash>:<dep_hash>"``.
* ``source`` — canonicalised realpath of the source file.

Plus the resulting ``cmd_hash`` itself so the caller can confirm the
two outputs they're diffing actually disagree on the final hash.

A pure JSON output makes the typical diagnostic flow trivial::

    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run1.json
    # ... second ct-cake invocation, no source changes ...
    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run2.json
    diff /tmp/run1.json /tmp/run2.json   # any diff names the drifting input
"""

from __future__ import annotations

import json
import sys

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
from compiletools import wrappedos
from compiletools.build_backend import _pcm_command_hash


def _detect_stage(cxx: str | None, source_path: str) -> str:
    """Return the ``stage`` value ``_pcm_command_hash`` expects for this source.

    Picks the named-module-interface variant matching the active
    compiler. Header-unit stages are not covered here: the bug-report
    diagnostic flow that drives this tool is about named modules; if
    a future caller needs a header-unit dump, add a ``--stage`` flag
    rather than auto-detecting from filename heuristics.
    """
    kind = compiletools.apptools.compiler_kind(cxx) or "gcc"
    if kind == "clang":
        return "clang_module_interface"
    return "gcc_module_interface"


def _gather_inputs(args, hunter, source_filename: str, stage: str) -> dict:
    """Compute the same six inputs ``BuildBackend._compute_pcm_command_hash``
    passes to ``_pcm_command_hash``, plus the final hash.

    Mirrors ``build_backend._compute_pcm_command_hash`` deliberately:
    we want the diagnostic to show *exactly* what the production path
    sees, so any future change to the hash inputs needs to be reflected
    here too. ``test_debug_pcm_hash_inputs_matches_production_path``
    is the drift guard.
    """
    import stringzilla as sz

    import compiletools.global_hash_registry as ghr
    import compiletools.namer
    from compiletools.apptools import (
        canonicalize_for_cache_key,
        canonicalize_path_for_cache_key,
        compiler_identity,
        filter_hash_irrelevant_tokens,
    )
    from compiletools.git_utils import find_git_root

    anchor_root = find_git_root() or ""

    deplist = hunter.header_dependencies(source_filename)
    namer = compiletools.namer.Namer(args, context=hunter.context)
    dep_hash = namer.compute_dep_hash(deplist)
    try:
        source_hash = ghr.get_file_hash(source_filename, hunter.context)
    except (FileNotFoundError, OSError):
        source_hash = ""
    transitive_content_hash = f"{source_hash}:{dep_hash}"

    magicflags = hunter.magicflags(source_filename)
    magic_cpp = magicflags.get(sz.Str("CPPFLAGS"), [])
    magic_cxx = magicflags.get(sz.Str("CXXFLAGS"), [])

    cxxflags_tokens = list(args.flags.hash_relevant("cxx"))
    source_realpath = wrappedos.realpath(source_filename)

    cxx_command_canon = canonicalize_path_for_cache_key(args.CXX, anchor_root)
    cxxflags_canon = canonicalize_for_cache_key(list(cxxflags_tokens), anchor_root)
    magic_cpp_canon = canonicalize_for_cache_key(
        filter_hash_irrelevant_tokens([str(f) for f in magic_cpp]),
        anchor_root,
    )
    magic_cxx_canon = canonicalize_for_cache_key(
        filter_hash_irrelevant_tokens([str(f) for f in magic_cxx]),
        anchor_root,
    )
    source_canon = canonicalize_path_for_cache_key(source_realpath, anchor_root)
    compiler_identity_str = compiler_identity(args.CXX, anchor_root=anchor_root)

    cmd_hash = _pcm_command_hash(
        args,
        source_path=source_realpath,
        transitive_content_hash=transitive_content_hash,
        cxxflags_tokens=cxxflags_tokens,
        magic_cpp_flags=magic_cpp,
        magic_cxx_flags=magic_cxx,
        extra_flags=[],
        stage=stage,
        anchor_root=anchor_root,
    )

    return {
        "source": source_canon,
        "stage": stage,
        "anchor_root": anchor_root,
        "compiler_identity": compiler_identity_str,
        "cxx_command": cxx_command_canon,
        "cxxflags_tokens": cxxflags_canon,
        "magic_cpp_flags": magic_cpp_canon,
        "magic_cxx_flags": magic_cxx_canon,
        "transitive_content_hash": transitive_content_hash,
        "source_hash": source_hash,
        "dep_hash": dep_hash,
        "deplist": sorted(str(d) for d in deplist),
        "cmd_hash": cmd_hash,
    }


def main(argv: list[str] | None = None) -> int:
    cap = compiletools.apptools.create_parser(
        "Dump the six _pcm_command_hash inputs for a C++20 module source. "
        "Run twice across back-to-back ct-cake invocations and diff the output "
        "to identify which input drifted when a BMI lands under a new "
        "<cas-pcmdir>/<cmd_hash>/ subdir on unchanged source.",
        argv=argv,
    )
    compiletools.headerdeps.add_arguments(cap)
    compiletools.magicflags.add_arguments(cap)
    cap.add_argument(
        "filename",
        nargs="+",
        help="Module source file(s) to inspect (.cppm or .cpp with `export module`).",
    )
    cap.add_argument(
        "--stage",
        default=None,
        help=(
            "Override the auto-detected hash stage. Default picks "
            "{gcc,clang}_module_interface based on the resolved CXX. "
            "Pass {gcc,clang}_header_unit to dump header-unit inputs."
        ),
    )

    from compiletools.build_context import BuildContext

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)
    headerdeps = compiletools.headerdeps.create(args, context=context)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=context)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=context)

    out = []
    for fname in args.filename:
        stage = args.stage or _detect_stage(getattr(args, "CXX", None), fname)
        out.append(_gather_inputs(args, hunter, fname, stage))

    json.dump(out if len(out) > 1 else out[0], sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
