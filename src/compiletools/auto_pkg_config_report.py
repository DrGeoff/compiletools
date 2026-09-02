"""ct-auto-pkg-config: report the auto-pkg-config inferences of source files.

Plan 2026-09-02 Stage 7 diagnostic entry point. Parses each requested file
through the production ``magicflags`` pipeline (forcing ``--auto-pkg-config``
on, so the report cannot be silently disabled by conf/env) and prints the raw
``pkgconfig_infer.InferenceResult`` verdicts -- resolved *and* unresolved with
the reason -- rather than just the injected packages.

Styles:

* ``pretty`` (default): one line per header,
  ``<file>: <header> -> <pkg> (via <resolver>)`` or
  ``<file>: <header> unresolved: <reason>``.
* ``json``: ``{"schema": 1, "files": {<file>: [{header, header_path, package,
  resolver, candidates, reason}, ...]}}`` for machine consumption.
* ``magic``: for each file with at least one inferred package, a
  ``// <file>`` comment line then sorted, deduped ``//#PKG-CONFIG=<pkg>``
  lines -- the freeze workflow: paste them into the sources and
  ``--no-auto-pkg-config`` parses produce the identical flags, giving teams
  that share one cache across hosts with different devel packages installed
  host-independent CAS keys (F13). Files with no inferences print nothing.

Exit code is 0 (diagnostic) unless ``--fail-on-ambiguous`` and any verdict is
ambiguous, in which case it is 1.
"""

import dataclasses
import json
import sys
from collections.abc import Iterable

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.pkgconfig_infer
import compiletools.wrappedos

_RESULT_ATTRS = ("header", "header_path", "package", "resolver", "candidates", "reason")


def _is_ambiguous(result) -> bool:
    """True when the verdict is an ambiguity (the same predicate
    ``magicflags._infer_pkg_config`` uses for its strict-mode error)."""
    return result.reason == compiletools.pkgconfig_infer.REASON_AMBIGUOUS or result.reason.startswith(
        compiletools.pkgconfig_infer.REASON_AMBIGUOUS + ": "
    )


def _result_dict(result) -> dict:
    payload = dataclasses.asdict(result)
    if payload["package"] is None and not payload["reason"]:
        # Unresolved with no reason text: mirror the pretty line's fallback.
        payload["reason"] = compiletools.pkgconfig_infer.REASON_NO_OWNER
    return {attr: payload[attr] for attr in _RESULT_ATTRS}


def _emit_pretty(realpath, results: Iterable) -> None:
    for result in results:
        if result.package is not None:
            print(f"{realpath}: {result.header} -> {result.package} (via {result.resolver})")
        else:
            reason = result.reason or compiletools.pkgconfig_infer.REASON_NO_OWNER
            print(f"{realpath}: {result.header} unresolved: {reason}")


def _emit_magic(realpath, results: Iterable) -> None:
    packages = sorted({result.package for result in results if result.package is not None})
    if not packages:
        return
    print(f"// {realpath}")
    for package in packages:
        print(f"//#PKG-CONFIG={package}")


def main(argv=None) -> int:
    # --auto-pkg-config is prepended rather than set post-parse: the flag is
    # read via getattr(self._args, "auto_pkg_config", False) inside _parse, so
    # setting the attribute after apptools.parseargs would be too late, and a
    # conf-file `auto-pkg-config = false` or CT_AUTO_PKG_CONFIG=0 must not be
    # able to turn the report into a silent no-op. The composing parser
    # suppresses the env/conf injection of the flag pair when either form is
    # on the CLI, so the prepend is safe and wins.
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    argv = ["--auto-pkg-config"] + argv

    cap = compiletools.apptools.create_parser(
        "Report the pkg-config packages auto-inference attributes to includes", argv=argv
    )
    compiletools.headerdeps.add_arguments(cap)
    compiletools.magicflags.add_arguments(cap)
    cap.add_argument("filename", nargs="+", help="File/s to report inferences for")
    cap.add_argument(
        "--style",
        choices=["pretty", "json", "magic"],
        default="pretty",
        help="Output style: one diagnostic line per header (pretty, default), a schema-tagged "
        "JSON report, or ready-to-paste //#PKG-CONFIG lines grouped by file (magic).",
    )
    cap.add_argument(
        "--fail-on-ambiguous",
        action="store_true",
        default=False,
        help="Exit 1 when any include is ambiguous (per --pkg-config-errors=warn the "
        "parse itself continues and the verdict is reported).",
    )

    from compiletools.build_context import BuildContext
    from compiletools.simple_preprocessor import MacroVerdictConflictError, verdict_session

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)
    headerdeps = compiletools.headerdeps.create(args, context=context)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=context)

    files: dict[str, list] = {}
    ambiguous = False
    # Same session shape as ct-magicflags: with more than one filename this is
    # a multi-target walk, and a cross-target verdict conflict must surface
    # here exactly as a build over the same files would surface it.
    try:
        with verdict_session(context, mode=getattr(args, "macro_verdict_conflict", "error"), tool="ct-auto-pkg-config"):
            for fname in args.filename:
                realpath = compiletools.wrappedos.realpath(fname)
                magicparser.parse(realpath)
                results = magicparser.last_inference_results.get(realpath, [])
                files[realpath] = list(results)
                ambiguous = ambiguous or any(_is_ambiguous(result) for result in results)
    except MacroVerdictConflictError as err:
        # The message is complete end-user prose (remedies included); matching
        # cake.main's rendering, not a traceback.
        if args.verbose >= 2:
            raise
        print(f"Error: {err}", file=sys.stderr)
        return 1

    if args.style == "json":
        payload = {
            "schema": 1,
            "files": {realpath: [_result_dict(result) for result in results] for realpath, results in files.items()},
        }
        print(json.dumps(payload))
    elif args.style == "magic":
        for realpath, results in files.items():
            _emit_magic(realpath, results)
    else:
        for realpath, results in files.items():
            _emit_pretty(realpath, results)

    if args.fail_on_ambiguous and ambiguous:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
