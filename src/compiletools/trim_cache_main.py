#!/usr/bin/env python3
"""CLI tool for trimming stale entries from shared build caches.

This tool scans cas-objdir and cas-pchdir for content-addressable entries
that no longer match the current git state, and removes the oldest stale entries
while preserving a configurable safety margin.

Usage:
    ct-trim-cache [--dry-run] [--cas-objdir PATH] [--cas-pchdir PATH] [options]

The tool will:
1. Load current file hashes from the git repository
2. Scan cas-objdir for .o files whose file hash no longer matches any tracked source
3. Scan cas-pchdir for command-hash directories with old precompiled headers
4. Remove the oldest non-current entries, keeping at least --keep-count per basename

Note on shared / multi-user caches: object currency is relative to the invoking
checkout's git HEAD. On a shared pool used by multiple branches or users, objects
built from other checkouts will appear non-current here. Use ``--max-age`` as the
primary eviction control on shared pools to limit removal by age rather than by
checkout-relative currency.
"""

import json
import os
import sys

import compiletools.apptools
import compiletools.configutils
import compiletools.jobs
import compiletools.trim_cache


def add_arguments(cap):
    """Add trim-cache specific arguments."""
    cap.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be removed without actually removing files",
    )
    cap.add_argument(
        "--max-age",
        type=int,
        default=None,
        help=(
            "Only remove non-current files older than this many days "
            "(default: no age limit). 'Older' means 'written more than N days ago' "
            "(mtime), NOT 'not accessed in N days' — atime is unreliable on "
            "noatime-mounted filesystems, so a hot-but-old cache entry will "
            "still be evicted. On a shared multi-branch or multi-user pool, "
            "this is the recommended primary eviction control: it removes only "
            "entries that have not been rebuilt recently, regardless of which "
            "checkout considers them current."
        ),
    )
    cap.add_argument(
        "--keep-count",
        type=int,
        default=1,
        help="Keep at least this many non-current files per basename/header (default: 1)",
    )
    cap.add_argument(
        "--max-size",
        type=str,
        default=None,
        help=(
            "Optional per-pool TOTAL size budget. Accepts a plain integer (bytes) "
            "or a 1024-based suffix K/M/G/T (case-insensitive, optional trailing "
            "'B'): e.g. '10G', '512M', '500MB', '1024'. After the normal trim, if "
            "a pool still exceeds this size, the OLDEST rebuildable (non-current, "
            "non-hard-linked) entries are evicted until it fits — this is the only "
            "control that can go below --keep-count, and it NEVER evicts a current "
            "object or a published (hard-linked) artefact. If those protected "
            "entries alone exceed the budget, the overflow is reported "
            "(budget_unmet_bytes) but never violated. Default: no budget."
        ),
    )
    cap.add_argument(
        "--cas-objdir-only",
        action="store_true",
        default=False,
        help="Only trim the object CAS, skip PCH",
    )
    cap.add_argument(
        "--cas-pchdir-only",
        action="store_true",
        default=False,
        help="Only trim the PCH CAS, skip objects",
    )
    cap.add_argument(
        "--cas-pcmdir-only",
        action="store_true",
        default=False,
        help="Only trim the C++20 module CAS (cas-pcmdir), skip objects and PCH",
    )
    cap.add_argument(
        "--cas-exedir-only",
        action="store_true",
        default=False,
        help="Only trim the executable CAS (cas-exedir), skip objects, PCH, and PCM",
    )
    cap.add_argument(
        "--json",
        action="store_true",
        default=False,
        help=(
            "Emit a single JSON object to stdout with raw integer byte counts and "
            "file counts per cache; route all human/progress text to stderr so "
            "stdout stays pure JSON for machine consumption."
        ),
    )
    cap.add_argument(
        "--list-resolvable",
        action="store_true",
        default=False,
        help=(
            "READ-ONLY: list cache cells (per-variant <pool>/<variant>/ dirs) that are "
            "ACTIVE — resolvable AND a canonicalization fixed point — against this "
            "checkout's conf hierarchy.  Prints bare sorted variant names to stdout "
            "(all human/progress text goes to stderr) so the output can be piped "
            "directly: ct-trim-cache --list-resolvable | while read v; do ...; done. "
            "Deletes nothing.  Complement of --list-unresolvable (NON_CANONICAL / "
            "UNRESOLVABLE / UNKNOWN cells are excluded).  With --json emits the same "
            "per-cache record structure as --list-unresolvable (mode: list-resolvable). "
            "Honours a single --cas-*-only flag to scope to one cache.  Mutually "
            "exclusive with --list-unresolvable and --purge-unresolvable."
        ),
    )
    cap.add_argument(
        "--list-unresolvable",
        action="store_true",
        default=False,
        help=(
            "READ-ONLY: list cache cells (per-variant <pool>/<variant>/ dirs) whose "
            "variant name no longer resolves against this checkout's conf hierarchy. "
            "Such cells are unreachable by the normal variant-driven trim. Deletes "
            "nothing. NOTE: 'unresolvable from this checkout' is NOT a durable orphan "
            "signal on a shared pool — a cell unresolvable here may be another "
            "checkout's or branch's live cache; the reported age helps tell them apart."
        ),
    )
    cap.add_argument(
        "--purge-unresolvable",
        action="store_true",
        default=False,
        help=(
            "DESTRUCTIVE: purge cache cells (per-variant <pool>/<variant>/ dirs) whose "
            "variant name no longer resolves against this checkout's conf hierarchy AND "
            "whose newest file is older than --max-age (COLD). Also reclaims NON_CANONICAL "
            "cells — resolvable but not a canonicalization fixed point (e.g. doubled-token "
            "directories like gcc.gcc.debug.debug) that a current build will never address — "
            "under the same COLD --max-age gate. REQUIRES --max-age > 0 — "
            "a warm unresolvable or non-canonical cell is most likely another live checkout's "
            "cache and is SPARED; zero or negative values are rejected. Removal is leaf-level "
            "and lock-safe; a cell whose artefacts a peer build is mid-write to is deferred "
            "to the next run, not hard-failed. Mutually exclusive with "
            "--list-unresolvable. A single --cas-*-only flag scopes the purge to that "
            "one cache. Honours --dry-run."
        ),
    )
    cap.add_argument(
        "--all-variants",
        action="store_true",
        default=False,
        help=(
            "Trim EVERY RESOLVABLE cell in the pool (the same set --list-resolvable "
            "prints), not just the single --variant cell. Cells are trimmed "
            "sequentially; a failure in one cell is isolated (reported but not fatal — "
            "other cells still run). Intra-cell -j parallelism is preserved. With "
            "--json emits a single aggregate object (mode: all-variants) whose "
            "'variants' list has one entry per swept cell plus an 'errors' list for "
            "any isolated failures. Mutually exclusive with --list-resolvable / "
            "--list-unresolvable / --purge-unresolvable. Honours --dry-run, --max-age, "
            "--max-size, --keep-count, and a single --cas-*-only scope flag. On shared "
            "multi-user or multi-branch pools prefer --max-age as the primary eviction "
            "control; without it, objects from other checkouts appear non-current and "
            "will be evicted down to --keep-count per basename."
        ),
    )


def main(argv=None):
    """Main entry point for ct-trim-cache.

    Returns:
        int: Exit code (0 = success, 1 = failure)
    """
    args = None
    try:
        cap = compiletools.apptools.create_parser("Trim stale entries from shared build caches", argv=argv)

        add_arguments(cap)
        # --parallel / -j: governs scan fan-out on high-latency filesystems
        # (GPFS/NFS/Lustre). Default honours CPU affinity / cgroups / slurm.
        compiletools.jobs.add_arguments(cap)

        variant = compiletools.configutils.extract_variant(argv=argv)
        compiletools.apptools.add_base_arguments(cap, argv=argv, variant=variant)
        compiletools.apptools.add_output_directory_arguments(cap, variant)

        args = cap.parse_args(args=argv)
        compiletools.apptools.resolve_cas_directory_arguments(args)
        args.verbose -= args.quiet

        # Parse --max-size once here (str → int bytes) and stash the result as
        # args.max_size_bytes; CacheTrimmer.__init__ reads that already-parsed
        # attribute. None means "no budget".
        if args.max_size is not None:
            try:
                args.max_size_bytes = compiletools.trim_cache._parse_size(args.max_size)
            except ValueError as exc:
                print(f"Error: invalid --max-size: {exc}", file=sys.stderr)
                return 1
        else:
            args.max_size_bytes = None

        only_flags = sum(
            bool(getattr(args, name))
            for name in ("cas_objdir_only", "cas_pchdir_only", "cas_pcmdir_only", "cas_exedir_only")
        )
        if only_flags > 1:
            print(
                "Error: --cas-objdir-only / --cas-pchdir-only / --cas-pcmdir-only / "
                "--cas-exedir-only are mutually exclusive (pick at most one)",
                file=sys.stderr,
            )
            return 1

        # --list-resolvable / --list-unresolvable / --purge-unresolvable are the
        # three standalone pool-level modes. They are MUTUALLY EXCLUSIVE WITH
        # EACH OTHER. A single --cas-*-only flag is NOT forbidden here — it
        # scopes any pool mode to the one selected cache (handled by the
        # selection logic inside the respective _cells functions).
        pool_modes = sum(
            bool(getattr(args, name)) for name in ("list_resolvable", "list_unresolvable", "purge_unresolvable")
        )
        if pool_modes > 1:
            print(
                "Error: --list-resolvable / --list-unresolvable / --purge-unresolvable "
                "are mutually exclusive (pick one)",
                file=sys.stderr,
            )
            return 1

        if args.all_variants and (args.list_resolvable or args.list_unresolvable or args.purge_unresolvable):
            print(
                "Error: --all-variants cannot be combined with --list-resolvable / "
                "--list-unresolvable / --purge-unresolvable",
                file=sys.stderr,
            )
            return 1

        # --list-resolvable is a standalone READ-ONLY mode: print the active
        # cell names and return without touching the normal trim path.
        if args.list_resolvable:
            result = compiletools.trim_cache.list_resolvable_cells(args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                compiletools.trim_cache.print_resolvable_report(result)
            return 0

        # --list-unresolvable is a standalone READ-ONLY mode: run the orphan
        # listing and return without touching the normal trim path.
        if args.list_unresolvable:
            result = compiletools.trim_cache.list_unresolvable_cells(args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                compiletools.trim_cache.print_unresolvable_report(result)
            return 0

        # --purge-unresolvable is a standalone DESTRUCTIVE pool-level mode. It
        # HARD-ERRORS without --max-age (there is no safe age cutoff to tell a
        # dead variant from another checkout's live cache without one), then
        # runs the purge and returns without the normal cell-level trim.
        if args.purge_unresolvable:
            if args.max_age is None or args.max_age <= 0:
                print(
                    f"Error: --purge-unresolvable requires --max-age > 0 (a positive "
                    f"number of days; only cells whose newest file is older than that "
                    f"are purged — a warm cell may be another checkout's live cache); "
                    f"got {args.max_age!r}",
                    file=sys.stderr,
                )
                return 1
            result = compiletools.trim_cache.purge_unresolvable_cells(args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                compiletools.trim_cache.print_purge_report(result)
            return 0

        # ``--cas-X-only`` flags select a single cache; with none set we
        # trim all four. Each cache runs unless any *other* "only" flag is on.
        do_objdir = not (args.cas_pchdir_only or args.cas_pcmdir_only or args.cas_exedir_only)
        do_pchdir = not (args.cas_objdir_only or args.cas_pcmdir_only or args.cas_exedir_only)
        do_pcmdir = not (args.cas_objdir_only or args.cas_pchdir_only or args.cas_exedir_only)
        do_exedir = not (args.cas_objdir_only or args.cas_pchdir_only or args.cas_pcmdir_only)

        # --all-variants: sweep every RESOLVABLE cell in the pool, not just the
        # single --variant cell. Per-cell errors are isolated (one bad cell is
        # reported, other cells still run). current_hashes is loaded once and
        # reused across all cells (it is variant-independent).
        if args.all_variants:
            caches = compiletools.trim_cache._active_cache_sections(args)
            variant = args.variant
            per_variant_dirs: dict = {}  # vname -> {section: <pool>/<vname>}
            enumerated: dict = {}
            for section, kind, cas_dir, active in caches:
                if not active or not cas_dir:
                    continue
                try:
                    pool = compiletools.trim_cache.cell_pool_root(cas_dir, variant)
                except ValueError as exc:
                    print(
                        f"warning: skipping {section} for --all-variants: {exc}",
                        file=sys.stderr,
                    )
                    continue
                key = (pool, kind)
                if key not in enumerated:
                    enumerated[key] = compiletools.trim_cache.enumerate_cells(pool, kind)
                for cell in enumerated[key]:
                    if cell["label"] != compiletools.trim_cache._CELL_RESOLVABLE:
                        continue
                    per_variant_dirs.setdefault(cell["name"], {})[section] = os.path.join(pool, cell["name"])

            current_hashes_av: set = set()
            if do_objdir and per_variant_dirs:
                from compiletools.build_context import BuildContext
                from compiletools.global_hash_registry import load_hashes

                context = BuildContext()
                load_hashes(verbose=args.verbose, context=context)
                current_hashes_av = compiletools.trim_cache.build_current_hash_set(context)
                if args.verbose >= 1:
                    _human_av = sys.stderr if args.json else sys.stdout
                    print(
                        f"Loaded {len(current_hashes_av)} current file hashes from git",
                        file=_human_av,
                    )

            agg: dict = {"schema": 1, "mode": "all-variants", "variants": [], "errors": []}
            any_failed_av = False
            for vname in sorted(per_variant_dirs):
                dirs = per_variant_dirs[vname]
                try:
                    res = compiletools.trim_cache.trim_one_variant(
                        args,
                        current_hashes=current_hashes_av,
                        cas_objdir=dirs.get("objdir"),
                        cas_pchdir=dirs.get("pchdir"),
                        cas_pcmdir=dirs.get("pcmdir"),
                        cas_exedir=dirs.get("exedir"),
                        do_objdir=do_objdir and "objdir" in dirs,
                        do_pchdir=do_pchdir and "pchdir" in dirs,
                        do_pcmdir=do_pcmdir and "pcmdir" in dirs,
                        do_exedir=do_exedir and "exedir" in dirs,
                        variant_label=vname,
                    )
                except Exception as exc:  # per-cell isolation
                    any_failed_av = True
                    agg["errors"].append({"variant": vname, "error": str(exc)})
                    print(f"Error trimming variant {vname!r}: {exc}", file=sys.stderr)
                    continue
                trimmer_av = res["trimmer"]
                entry = trimmer_av.summary_json(
                    objdir_stats=res["objdir"],
                    pchdir_stats=res["pchdir"],
                    pcmdir_stats=res["pcmdir"],
                    exedir_stats=res["exedir"],
                )
                entry["variant"] = vname
                agg["variants"].append(entry)
                per_cell_failed = sum((res[s] or {}).get("failed", 0) for s in ("objdir", "pchdir", "pcmdir", "exedir"))
                if per_cell_failed:
                    any_failed_av = True

            if args.json:
                print(json.dumps(agg, indent=2))
            else:
                for entry in agg["variants"]:
                    print(f"=== {entry['variant']} ===", file=sys.stderr)
            return 1 if (any_failed_av or agg["errors"]) else 0

        # Object cache currency check is the only consumer of the
        # tracked-files set. PCM, PCH, and exe-cache use sidecar
        # manifests / hard-link refcounts / bucketing instead.
        current_hashes = set()
        if do_objdir:
            from compiletools.build_context import BuildContext
            from compiletools.global_hash_registry import load_hashes

            context = BuildContext()
            load_hashes(verbose=args.verbose, context=context)
            current_hashes = compiletools.trim_cache.build_current_hash_set(context)

            if args.verbose >= 1:
                _human = sys.stderr if args.json else sys.stdout
                print(f"Loaded {len(current_hashes)} current file hashes from git", file=_human)

        res = compiletools.trim_cache.trim_one_variant(
            args,
            current_hashes=current_hashes,
            cas_objdir=args.cas_objdir,
            cas_pchdir=args.cas_pchdir,
            cas_pcmdir=args.cas_pcmdir,
            cas_exedir=args.cas_exedir,
            do_objdir=do_objdir,
            do_pchdir=do_pchdir,
            do_pcmdir=do_pcmdir,
            do_exedir=do_exedir,
            variant_label=args.variant,
        )
        trimmer = res["trimmer"]
        objdir_stats = res["objdir"]
        pchdir_stats = res["pchdir"]
        pcmdir_stats = res["pcmdir"]
        exedir_stats = res["exedir"]

        if args.json:
            print(
                json.dumps(
                    trimmer.summary_json(
                        objdir_stats=objdir_stats,
                        pchdir_stats=pchdir_stats,
                        pcmdir_stats=pcmdir_stats,
                        exedir_stats=exedir_stats,
                    ),
                    indent=2,
                )
            )
        else:
            trimmer.print_summary(objdir_stats, pchdir_stats, pcmdir_stats, exedir_stats)

        any_failed = (
            (objdir_stats or {}).get("failed", 0)
            + (pchdir_stats or {}).get("failed", 0)
            + (pcmdir_stats or {}).get("failed", 0)
            + (exedir_stats or {}).get("failed", 0)
        )
        return 1 if any_failed else 0

    except OSError as ioe:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {ioe.strerror}: {ioe.filename}", file=sys.stderr)
            return 1
        else:
            raise
    except Exception as err:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
