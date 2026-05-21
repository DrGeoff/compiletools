================
ct-cache-report
================

----------------------------------------------------------------------------------
Summarize occupancy and detect duplication across the CAS directories
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-10
:Version: 10.0.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cache-report [--cas-objdir PATH] [--cas-pchdir PATH] [--cas-pcmdir PATH] [--cas-exedir PATH] [--top N] [--json]

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

``--json``
    Emit JSON instead of human-readable text. The JSON schema is
    described below.

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

EXIT CODES
==========
0
    Success (including the no-args case where no cache directories
    exist on disk -- the report is empty but the run is well-formed).
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

**Show only the top-3 worst offenders**::

    ct-cache-report --cas-objdir=cas-objdir/blank --top 3

SEE ALSO
========
``ct-trim-cache`` (1) -- removes the duplicates this tool reports.
``ct-cas-publish`` (1) -- writes the ``.manifest`` sidecars that the
``cas-exedir`` report uses to bucket by source identity.
``ct-cake`` (1) -- the build orchestrator; its
``--cas-{obj,pch,pcm,exe}dir`` flags determine where the caches
``ct-cache-report`` reads actually live.
