================
ct-cache-report
================

----------------------------------------------------------------------------------
Summarize occupancy and detect duplication across the CAS directories
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-06-11
:Version: 10.2.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cache-report [--cas-objdir PATH] [--cas-pchdir PATH] [--cas-pcmdir PATH] [--cas-exedir PATH] [--top N] [--all-variants] [--json] [--otel-export [--otel-endpoint URL] [--otel-protocol grpc|http] ...]

DESCRIPTION
===========
``ct-cache-report`` walks one or more content-addressable cache
directories and reports their occupancy plus any duplication caused by
cache-key pollution.

Scope follows the rule used by ``ct-trim-cache``: a no-args invocation
operates on the four variant-default CAS directories
(``{git_root}/cas-{obj,pch,pcm,exe}dir/{variant}``), reporting on
whichever ones exist on disk. Naming any of ``--cas-objdir``,
``--cas-pchdir``, ``--cas-pcmdir``, ``--cas-exedir`` explicitly scopes
the scan to just those caches.

The tool is read-only: it never deletes, renames, or rewrites cache
entries. Pair it with ``ct-trim-cache`` when you actually want to
reclaim space.

Why duplication happens
-----------------------
Each CAS hashes a different identity into the cache key:

* **cas-objdir** -- compiler + flags + source content + transitive
  header content + macro state. ``-D`` flags that the source doesn't
  actually consult still influence ``macro_state_hash`` and create
  bit-identical duplicates.

* **cas-pchdir** -- compiler + flags + header realpath + transitive
  header content. Different command-line flag sets (e.g. via cwd-driven
  ``CXXFLAGS`` overrides) produce different ``command_hash`` directories
  for the same header.

* **cas-pcmdir** -- compiler + flags + module/header source content +
  transitive header content. Same key-pollution shape as PCH.

* **cas-exedir** -- linker identity + LDFLAGS + objects + canonical
  bindir + a small set of environment variables (``SOURCE_DATE_EPOCH``,
  ``LIBRARY_PATH``, ``LD_LIBRARY_PATH``, ``LD_PRELOAD``) + ``ar``
  identity. Spurious LDFLAGS variation or environment-variable churn
  between builds produces multiple ``link_key`` variants for the same
  linker artefact.

Two cache entries that share the underlying source/header/module/output
but differ in a hash component are bit-identical duplicates from this
kind of pollution. Eliminating the pollution shrinks the cache and
raises hit rates on the next clean build.

Object cache report
-------------------
For ``cas-objdir``, the report groups entries by ``(file_hash,
dep_hash)``. Two entries that share that pair but differ in
``macro_state_hash`` are bit-identical duplicates spawned by
command-line ``-D`` macro pollution of the cache key. The summary
shows total entries, total bytes, the number of duplicated groups,
the variant-count range, and total wasted bytes (sum-min per group).
The top-N section lists basenames in descending order of waste.

PCH cache report
----------------
For ``cas-pchdir``, the report groups ``<command_hash>/`` directories
by their manifest's ``header_realpath``. Multiple ``command_hash``
directories pointing at the same header realpath are PCH duplicates
from compiler-flag or environment pollution. Manifest-less or corrupt
entries are tagged ``<unknown:<cmd_hash>>`` so unrelated orphans don't
collapse into one fake duplicate group.

PCM cache report
----------------
For ``cas-pcmdir``, the report groups ``<command_hash>/`` directories
by their manifest's ``bucket_key`` -- the source realpath for named
modules, or the verbatim ``<vector>`` / ``"foo.h"`` token for header
units. Each duplicate group counts variants of the same module or
header unit produced under different compile configurations. The
``stage`` marker (``clang_module_interface`` / ``gcc_module_interface``
/ ``clang_header_unit`` / ``gcc_header_unit``) is captured per entry
for diagnostics but does not partition the bucket key, since
``bucket_key`` already disambiguates by shape (path vs token).
Manifest-less or corrupt entries are tagged ``<unknown:<cmd_hash>>``.

Linker-artefact cache report
----------------------------
For ``cas-exedir``, the report groups
``<basename>_<linkkey><suffix>`` artefacts by ``(source_realpath,
suffix)`` from the per-entry ``.manifest`` sidecar (with fall-back to
``(basename, suffix)`` for legacy entries). Suffix is part of the key
so ``libfoo.a`` and ``libfoo.so`` -- which legitimately coexist for
the same source -- are not flagged as duplicates of each other.
Multiple ``link_key`` variants in one bucket are duplicates from
LDFLAGS or environment-variable pollution of the link key.

OPTIONS
=======
``--cas-objdir PATH``
    Path to the cas-objdir to scan (default: the variant's cas-objdir
    under the git root). Naming any explicit ``--cas-*dir`` flag scopes
    the scan to just the named caches.

``--cas-pchdir PATH``
    Path to the cas-pchdir to scan (default: the variant's cas-pchdir).

``--cas-pcmdir PATH``
    Path to the cas-pcmdir (C++20 modules cache) to scan (default: the
    variant's cas-pcmdir).

``--cas-exedir PATH``
    Path to the cas-exedir (linker-artefact cache) to scan (default:
    the variant's cas-exedir).

``--top N``
    Show the top N most-duplicated entries per cache. Default: 10.

``--all-variants``
    Report **every RESOLVABLE cell** in the pool, not just the single
    ``--variant`` cell. For each in-scope cache the pool root is derived
    from the variant-suffixed ``--cas-*dir`` path; the resolvable variant
    names are enumerated (the same set ``ct-trim-cache --list-resolvable``
    prints) and reported in sorted order. Per-cell errors are isolated — a
    failure in one cell is recorded but never aborts the others. The same
    scope rules apply: with no explicit ``--cas-*dir`` flag, the
    variant-default caches present on disk are swept; naming a ``--cas-*dir``
    flag scopes the sweep to those caches. Exits nonzero if any cell errored.
    With ``--json`` the document is the aggregate described under
    `Whole-pool aggregate (--all-variants)`_. Cannot be combined with
    ``--otel-export`` (a hard error): metric export covers only the single
    ``--variant`` cell, so run ``ct-cache-report --otel-export`` once per
    variant instead.

``--json``
    Emit JSON instead of human-readable text. The JSON schema is
    described below.

``--otel-export`` / ``--no-otel-export``
    Also ship the CAS-health gauges to an OTLP collector as
    OpenTelemetry metrics. Default: off (text/JSON output only).
    Unlike ``ct-cake``'s ``--otel-export``, there is no ``--timing``
    coupling here -- ``ct-cache-report`` is a one-shot scan, so the
    metric snapshot is emitted directly from the report it just built.
    See the OPENTELEMETRY METRIC EXPORT section below for the metric
    set and the optional install extra.

``--otel-endpoint URL``
    OTLP collector endpoint URL. Defaults to
    ``$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``, then
    ``$OTEL_EXPORTER_OTLP_ENDPOINT``, as picked up by the SDK.

``--otel-protocol grpc|http``
    OTLP transport. Default: ``grpc``.

``--otel-service-name NAME``
    OTel ``service.name`` resource attribute. Default:
    ``compiletools``. Also honours ``OTEL_SERVICE_NAME``.

``--otel-resource-attr K=V``
    Extra OTel resource attribute as ``K=V`` (repeatable, or
    comma-separated). Merged on top of ``OTEL_RESOURCE_ATTRIBUTES``.

``--otel-headers K=V,K=V``
    OTLP exporter headers as ``K=V,K=V`` (e.g. for auth proxies).

``--otel-insecure`` / ``--no-otel-insecure``
    Disable / force TLS on the OTLP gRPC connection. If neither is
    passed, the SDK infers from the endpoint URL scheme (``http://``
    -> insecure, ``https://`` -> secure).

``--otel-metrics-as-spans`` / ``--no-otel-metrics-as-spans``
    When the collector accepts only traces (no metrics endpoint),
    flatten the gauge values into a single short-lived span instead of
    emitting OTLP metrics. Default: off.

JSON OUTPUT
===========
With ``--json``, the report is emitted as a single JSON document.

Combined schema (default)
-------------------------
Used whenever more than one cache is requested, or when any cache
other than ``--cas-objdir`` is requested. Caches that were not
requested are present as ``null`` so consumers can rely on a stable
key set::

    {
      "cas-objdir-report": { ... } | null,
      "cas-pchdir-report": { ... } | null,
      "cas-pcmdir-report": { ... } | null,
      "cas-exedir-report": { ... } | null
    }

Each non-null sub-report carries kebab-case fields describing the
scan: ``total-entries``, ``total-bytes``, ``unique-*-count``,
``duplicated-groups-count``, ``wasted-bytes``, plus a ``top-*`` array
of the worst N offenders.

Flat objdir-only schema (legacy)
--------------------------------
Preserved for back-compat: when ONLY ``--cas-objdir`` is supplied with
``--json``, the document contains the objdir fields at the top level
(``cas-objdir``, ``total-entries``, etc.) instead of being wrapped
under ``cas-objdir-report``. Any combination involving another cache
flag triggers the combined schema above.

Whole-pool aggregate (--all-variants)
-------------------------------------
With ``--all-variants --json`` the document is a versioned aggregate
that wraps one combined-schema report per resolvable cell::

    {
      "schema": 1,
      "mode": "all-variants",
      "variants": [
        {
          "variant": "<name>",
          "cas-objdir-report": { ... } | null,
          "cas-pchdir-report": { ... } | null,
          "cas-pcmdir-report": { ... } | null,
          "cas-exedir-report": { ... } | null
        },
        ...
      ],
      "errors": [ { "variant": "<name>", "error": "<message>" }, ... ]
    }

Each ``variants`` entry carries the same four ``cas-*-report`` keys as
the combined schema (a cache that is out of scope, or has no cell for
that variant, is ``null``) plus the cell's ``"variant"`` name. The
``errors`` list holds one record per cell whose report raised an
isolated failure; the process exits nonzero when it is non-empty. The
flat objdir-only schema is never used under ``--all-variants``.

OPENTELEMETRY METRIC EXPORT
===========================
With ``--otel-export``, the same CAS-health figures rendered as text or
JSON are also emitted as OpenTelemetry (OTLP) metrics, then the process
flushes and exits. There is no daemon and no periodic re-export: the
natural deployment is a cron job or post-build hook per CAS-bearing host
so dashboards have a current picture of cache health without paying for
continuous scraping.

Five gauges are emitted, each tagged with a ``cas_kind`` attribute
(``obj`` / ``pch`` / ``pcm`` / ``exe``) so one query can break down by
cache:

* ``ct.cas.total_bytes`` -- total bytes occupied by the cache.
* ``ct.cas.total_entries`` -- total cache entries scanned.
* ``ct.cas.unique_buckets`` -- distinct logical artefacts (the
  per-cache grouping key: ``(file_hash, dep_hash)`` for objdir, header
  realpath for pchdir, ``bucket_key`` for pcmdir,
  ``(source_realpath, suffix)`` for exedir).
* ``ct.cas.wasted_bytes`` -- bytes attributable to key-pollution
  duplication (sum-min per group).
* ``ct.cas.duplicate_groups`` -- number of duplicated groups.

A cas directory that was scanned but empty contributes one observation
per gauge with value 0 ("I scanned, found nothing"); a cas directory
that was not scanned contributes no observations at all. The same
scope rules as the text/JSON report apply -- only the caches requested
(or, with no explicit ``--cas-*dir`` flag, the variant-default caches
present on disk) are observed.

With ``--otel-metrics-as-spans``, the gauge values are instead
flattened onto attributes of a single ``ct.cache.snapshot`` span
(attribute shape ``ct.cas.<metric_stem>.<cas_kind>``, e.g.
``ct.cas.total_bytes.obj``); no metric pipeline is built. This is the
fallback for collectors that accept traces but not metrics.

The OpenTelemetry SDK is an optional dependency. Install the ``otel``
extra to enable export::

    pip install 'compiletools[otel]'

Without the extra, ``--otel-export`` raises ``RuntimeError`` naming the
missing extra. Default behaviour is off: absent ``--otel-export``, no
SDK is imported and no metrics are emitted.

EXIT CODES
==========
0
    Success (including the no-args case where no cache directories
    exist on disk -- the report is empty but the run is well-formed).
1
    With ``--all-variants``, one or more cells raised an isolated report
    failure (each is recorded in the aggregate's ``errors`` list; the
    remaining cells still report).
2
    Argument-parsing failure (e.g. an unknown flag).

EXAMPLES
========
**Report on every variant-default CAS that exists**::

    ct-cache-report

**Scan a single specific cache**::

    ct-cache-report --cas-objdir=$(git rev-parse --show-toplevel)/cas-objdir/blank

**Switch variant**::

    ct-cache-report --variant=gcc.release

**All four caches, JSON for downstream tooling**::

    ct-cache-report --json | jq '.["cas-objdir-report"]."wasted-bytes"'

**Report every resolvable cell in the pool (whole-pool sweep)**::

    ct-cache-report --all-variants --json \
        | jq '.variants[] | {variant, wasted: .["cas-objdir-report"]."wasted-bytes"}'

**Show only the top-3 worst offenders**::

    ct-cache-report --cas-objdir=cas-objdir/blank --top 3

**Emit CAS-health gauges to an OTLP collector (cron-friendly)**::

    ct-cache-report --otel-export \
        --otel-endpoint=http://otel-collector.internal:4317 \
        --otel-resource-attr host=$(hostname)

SEE ALSO
========
``ct-trim-cache`` (1) -- removes the duplicates this tool reports.
``ct-otel`` (1) -- documents the same shared ``--otel-*`` flags for
``ct-cake``'s build-span export, plus collector-setup recipes.
``ct-cas-publish`` (1) -- writes the ``.manifest`` sidecars that the
``cas-exedir`` report uses to bucket by source identity.
``ct-cake`` (1) -- the build orchestrator; its
``--cas-{obj,pch,pcm,exe}dir`` flags determine where the caches
``ct-cache-report`` reads actually live.
