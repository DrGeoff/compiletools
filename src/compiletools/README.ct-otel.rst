========
ct-otel
========

------------------------------------------------------------------------
Export ct-cake build timing as OpenTelemetry (OTLP) traces and metrics
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-27
:Version: 10.1.7
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cake --auto --timing --otel-export [--otel-endpoint URL]
[--otel-service-name NAME] [--otel-protocol grpc|http]
[--otel-resource-attr K=V ...] [--otel-headers K=V,K=V]
[--otel-insecure] [--otel-metrics-as-spans] [--ccache-statslog PATH|auto]

ct-cache-report --otel-export [--otel-endpoint URL] [...]

DESCRIPTION
===========

When ``ct-cake --timing`` is on, compiletools already collects a
hierarchical span tree of build phases and per-rule events on a single
monotonic clock (see ``ct-timing-report`` (1)).  Adding
``--otel-export`` walks that finished tree at end-of-build and ships
one OpenTelemetry span per event to an OTLP collector — the same data
that would otherwise only land in ``timing.json``, but in a format any
OTel-aware backend (Tempo, Jaeger, Honeycomb, an internal OTel
collector, a ClickHouse pipeline, ...) can ingest.

``--otel-export`` ships **OTLP metrics** alongside the traces:

- Cross-layer cache aggregates — the headline "what fraction of TUs
  did caching save?" numbers — are lifted onto the root build span as
  ``ct.build.*`` attributes (see `CROSS-LAYER CACHE AGGREGATES`_).
- ``ct-cake --ccache-statslog`` parses the build's ccache event log
  and, when ``--otel-export`` is also on, ships it as ``ct.ccache.*``
  metrics (see `CCACHE STATS METRICS`_).
- ``ct-cache-report --otel-export`` emits ``ct.cas.*`` CAS-health
  gauges from a content-addressable-store scan (see
  `CAS-HEALTH GAUGES`_).

The exporter is a pure end-of-build batch step.  No spans are emitted
during the build itself.  Each OTLP request carries a 5-second
per-request timeout, but the OpenTelemetry SDK does not propagate that
bound to the underlying network call, and the exporter retries with
backoff — so against a slow or unreachable collector the end-of-build
flush can take longer than the nominal 5-second budget (ct-cake prints
a stderr warning when it does).  Either way, a failed or timed-out
export does not fail the build.

The OpenTelemetry SDK is an optional dependency.  Default installs of
compiletools do not pull it in, and ``--otel-export`` is off by
default.  Install the optional extra to enable::

    pip install 'compiletools[otel]'

Without the extra, ``--otel-export`` raises ``RuntimeError`` with the
install hint.  With the flag off, the SDK is never imported and adds
zero startup cost.

REQUIREMENTS
============

``--otel-export`` implies ``--timing``: passing ``--otel-export``
without ``--timing`` automatically enables timing collection so the
span tree exists for the exporter to ship.  Passing
``--otel-export --no-timing`` together is rejected at parse time with
a clear error — the two requests are internally contradictory
(exporting an empty span tree is unambiguously a mistake).

The optional extra installs four packages:

- ``opentelemetry-api>=1.27``
- ``opentelemetry-sdk>=1.27``
- ``opentelemetry-exporter-otlp-proto-grpc>=1.27``
- ``opentelemetry-exporter-otlp-proto-http>=1.27``

Both gRPC and HTTP exporters are installed so ``--otel-protocol=http``
works without a second install step.

CONFIGURATION
=============

All flags flow through the standard ct-cake configuration hierarchy
(bundled < system < venv < user < project < cwd < env < CLI), so any
value below can equivalently be set in any ``ct.conf`` file or as an
environment variable matching the configargparse uppercase form
(e.g. ``OTEL_EXPORT=true``).  Five flags
— ``--otel-endpoint``, ``--otel-headers``, ``--otel-insecure``,
``--otel-service-name``, ``--otel-resource-attr`` — are
CLI/config-only on the configargparse side (no env-var pickup): the
OpenTelemetry SDK has its own env-var precedence chain for these
values (including trace-specific overrides such as
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``), and ct-cake defers to the
SDK as the env-var authority so that precedence is honoured intact.

**--otel-export / --no-otel-export**
    Enable end-of-build OTLP export.  Default: off.  Implies
    ``--timing`` (the span tree must exist before there is anything to
    export, so ``--otel-export`` automatically turns timing on).
    ``--otel-export --no-timing`` together is rejected at parse time
    as internally contradictory.

**--otel-endpoint URL**
    OTLP collector endpoint.  Default: unset, in which case the
    OpenTelemetry SDK consults the standard
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``
    environment variables.  Examples: ``http://localhost:4317`` (gRPC
    default port), ``http://localhost:4318`` (HTTP default port),
    ``https://api.honeycomb.io``.

**--otel-service-name NAME**
    Value for the ``service.name`` resource attribute.  Default:
    ``compiletools``.  Also honours ``OTEL_SERVICE_NAME`` (standard
    OpenTelemetry env var).

**--otel-protocol grpc|http**
    OTLP wire protocol.  Default: ``grpc``.  Use ``http`` to talk to
    collectors that only expose the HTTP/protobuf receiver (port
    4318), or when gRPC is blocked at a corporate proxy.

**--otel-resource-attr K=V**
    Additional resource attributes to attach to every span's
    ``Resource``.  Repeatable; comma-separated lists are also
    accepted.  CLI values override anything from
    ``OTEL_RESOURCE_ATTRIBUTES`` (the standard env var the SDK
    consults).  Built-in attributes (see `SPAN MODEL`_) win over
    user-supplied keys with the same name.
    Example: ``--otel-resource-attr deployment.environment=ci
    --otel-resource-attr team=platform``.

**--otel-headers K=V,K=V**
    Extra headers attached to every OTLP request.  Comma-separated.
    Typically used for vendor auth keys (e.g. ``x-honeycomb-team=...``)
    or proxy auth.

**--otel-insecure / --no-otel-insecure**
    gRPC only.  Skip TLS verification when talking to the collector.
    Use for a local collector on ``http://localhost:4317``.  If
    neither flag is passed, the OpenTelemetry SDK infers from the
    endpoint URL scheme (``http://`` -> insecure, ``https://`` ->
    secure); pass ``--otel-insecure`` / ``--no-otel-insecure`` only
    to override that inference.

**--otel-metrics-as-spans / --no-otel-metrics-as-spans**
    For collectors that accept traces but expose no metrics endpoint.
    When set, the metric sets described under `CCACHE STATS METRICS`_
    and `CAS-HEALTH GAUGES`_ are flattened onto the attributes of a
    single short-lived span each (``ct.ccache.snapshot`` /
    ``ct.cache.snapshot``) instead of being shipped as OTLP metrics —
    no metrics pipeline is built.  Default: off.  Has no effect on the
    trace export or the ``ct.build.*`` root-span aggregates (those are
    span attributes regardless).

**--ccache-statslog PATH|auto**
    ``ct-cake`` only.  Capture ccache per-call events for this build by
    exporting ``CCACHE_STATSLOG=<path>`` into the build subprocess
    environment; ccache appends one event name per cache lookup to that
    file for the duration of the build.  See `CCACHE STATS METRICS`_ for
    values, lifecycle, and the ``--otel-export`` interaction.

Standard ``OTEL_*`` environment variables consulted by the
OpenTelemetry SDK itself — ``OTEL_EXPORTER_OTLP_ENDPOINT``,
``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_EXPORTER_OTLP_INSECURE``,
``OTEL_RESOURCE_ATTRIBUTES``, ``OTEL_SERVICE_NAME`` — are honoured for
any field the corresponding ``--otel-*`` flag leaves unset.

.. note::

    Like every ct.conf-driven knob, ``otel-endpoint`` / ``otel-headers``
    from a project's ``ct.conf`` should be reviewed before running
    ct-cake in repos you don't own — they steer where build metadata
    (hostname, git SHA, file paths) is shipped.

SPAN MODEL
==========

The exporter emits one OpenTelemetry span per ``TimingEvent`` in the
``BuildTimer`` tree, preserving parent/child via the SDK's ``Context``
propagation.  Timestamps are wall-clock nanoseconds derived from
BuildTimer's monotonic timeline via the wall-to-monotonic offset
captured at ``BuildTimer.__init__``, so the resulting trace aligns
with the rest of an observability stack.

================ ================================ =====================================================================================================
Level            Span name                        Notable attributes
================ ================================ =====================================================================================================
Root             ``compiletools.build``           Resource: ``service.name``, ``service.namespace=compiletools``, ``host.name``,
                                                  ``service.instance.id``, ``git.commit.sha``, ``ct.variant``, ``ct.backend``,
                                                  ``ct.invocation_id``; plus ``ct.build.*`` cross-layer cache aggregates (see below)
Phase            ``phase.<name>``                 No span attrs (phase carries its name in the span name)
Rule (compile)   ``compile.<dir>/<basename>``     ``ct.rule_type=compile``, ``ct.target``, ``ct.source``
Rule (link)      ``link.<dir>/<basename>``        ``ct.rule_type=link``, ``ct.target`` (``ct.source`` omitted for link rules)
Rule (other)     ``<rule_type>.<dir>/<basename>`` ``ct.rule_type``, ``ct.target``, and ``ct.source`` when present
================ ================================ =====================================================================================================

Rule span names are qualified by the basename of the parent directory
of the rule's source (or its target, for rules without a source) —
``compile.src/foo.cpp`` rather than ``compile.foo.cpp`` — so two
translation units sharing a filename (``src/util.cpp`` vs
``tests/util.cpp``) stay distinct in the trace UI.  The unqualified
``<rule_type>.<basename>`` form appears only when the source or target
has no directory component.  ``ct.target`` / ``ct.source`` remain the
canonical, fully-qualified query keys.

Rule events that never recorded a real start time (an internal
``start_s == 0.0`` sentinel — e.g. a Slurm job whose ``sacct``
timestamps could not be parsed) are omitted from the trace so a
spurious 1970 span cannot drag the timeline to the epoch; their elapsed
time is still present in ``timing.json``.

``ct.invocation_id`` is the basename of the per-invocation diagnostics
directory (``--diagnostics-dir``'s ``<invocation-id>`` segment), so a
span in the collector can be cross-referenced one-to-one with the
``timing.json`` artefact ct-cake writes alongside it.

``git.commit.sha`` is read via ``git rev-parse HEAD`` with a 2-second
timeout; an empty value is dropped from the Resource if git is not
available.

``service.instance.id`` is ``<host.name>:<pid>`` — the standard OTel
convention attribute disambiguating concurrent emitters on the same
host (parallel ct-cake invocations, or a cron-driven
``ct-cache-report`` colliding with a manual run).  It is attached to
every Resource the exporter builds (traces and metrics alike).  The
same set of Resource attributes — including ``service.instance.id`` and
any ``--otel-resource-attr`` values — is attached to the metric
exporters described below; metrics additionally carry
``ct.invocation_id`` set to the build root span's trace_id so a backend
that indexes on trace_id joins the ccache metrics natively against the
build's spans.

CAS-attribute coverage scope (CAS attributes on rule spans)
-----------------------------------------------------------

Per-rule ``cas.hit`` / ``cas.kind`` / ``cas.bytes_reused`` attributes
require the build backend to emit per-rule outcomes that the exporter
can ingest.  Today's coverage:

================================================  ==============================
Backend / path                                    ``cas.*`` on spans?
================================================  ==============================
``trace`` backend (Shake / Slurm)                 yes — in-process via
                                                  ``BuildTimer.record_rule``
``ninja`` / ``make`` via ``ct-lock-helper``       yes — lockdir / fcntl / cifs
(strategies for NFS / Lustre, GPFS, CIFS / SMB)   strategies write to
                                                  ``CT_RULE_OUTCOMES_LOG``
``ninja`` / ``make`` via native ``flock(1)``      **no** — the local-filesystem
fast-path (local filesystem with util-linux       fast-path bypasses
``flock`` available)                              ``ct-lock-helper`` for speed;
                                                  ``cas.*`` keys are absent
``cmake`` / ``bazel``                             no — backends are outside
                                                  compiletools' lock wrapper
================================================  ==============================

The ``ct.backend`` resource attribute lets dashboards filter to
backends that emit ``cas.*`` reliably without scraping span attrs.
``cas.kind`` for the ``ct-lock-helper`` writer is currently best-effort
``"obj"`` (compile) or ``"exe"`` (link/archive) — that layer cannot
distinguish static-library from executable.  The ``trace`` backend has
the rule-type metadata and tags ``lib``/``pcm``/``pch`` correctly.

METRICS MODEL
=============

Beyond the span tree, ``--otel-export`` ships three metric families.
All metric export is snapshot-and-exit: each entry point builds a fresh
``MeterProvider``, records one observation per instrument,
force-flushes, and shuts down.  There is no long-running daemon and no
periodic re-export.  Under ``--otel-metrics-as-spans`` the ccache and
CAS-health families are emitted as attribute-bearing spans instead (the
``ct.build.*`` aggregates are always span attributes regardless).  Like
the trace path, a failed or timed-out metric flush warns on stderr but
never fails the build.

CROSS-LAYER CACHE AGGREGATES
----------------------------

compiletools has two nested cache layers: the per-rule object/PCH/PCM
CAS (a CAS hit means the compiler never ran) and ccache (a CAS *miss*
can still be a ccache hit, because the lock wrapper invokes the real
compiler under ccache).  At end-of-build, ``ct-cake`` joins the
per-rule CAS-hit metadata with the build-wide ccache event counts into
a single set of root-build-span attributes — the "what fraction of TUs
did caching save?" headline — so dashboards do not re-derive the join
per query.  These are span attributes on ``compiletools.build`` (not
OTLP metrics), and they are also written into ``timing.json`` so
offline tooling sees the same numbers.

=================================== =====================================================================
Root-span attribute                 Meaning
=================================== =====================================================================
``ct.build.cas_avoided_count``      Compile rules with ``cas.hit == True`` (compiler never ran).
``ct.build.ccache_avoided_count``   ccache ``direct_cache_hit`` + ``preprocessed_cache_hit`` events.
``ct.build.recompiled_count``       Compile rules neither CAS- nor ccache-saved (best-effort; floored
                                    at zero — ccache attribution is build-wide, not per-rule).
``ct.build.compile_avoided_rate``   ``(cas + ccache) / total`` compile rules, clamped to ``[0, 1]``;
                                    ``0.0`` when there are no compile rules.
``ct.build.aggregate_warning``      ``"ccache_overcount"`` — present ONLY when the statslog reported
                                    more ccache hits than there were CAS-miss compile rules to
                                    attribute to (e.g. a stale log reused across builds).  Absence is
                                    a reliable "numbers are well-formed" signal.
=================================== =====================================================================

The four counts/rate are always emitted (even as zeros) so a dashboard
can tell "build did not aggregate" from "build aggregated to zero".
With no per-rule CAS data (cmake/bazel backends) ``cas_avoided_count`` is 0;
with no ``--ccache-statslog`` ``ccache_avoided_count`` is 0 and the
rate reflects CAS-only savings; with both absent ``recompiled_count``
equals the total compile-rule count.

Each compile rule's span additionally carries
``ct.rule.cache_layer``: ``"cas"`` when that rule's CAS short-circuit
fired, otherwise ``"other"`` for ninja/make builds (ccache's statslog
is a build-wide event stream with no per-target binding, so a precise
``"ccache"`` vs ``"compiled"`` split is reported only at the root-span
level, not per rule).

CCACHE STATS METRICS
--------------------

``ct-cake --ccache-statslog`` exports ``CCACHE_STATSLOG=<path>`` into
the build subprocess environment.  ccache then appends one event name
per cache lookup (``direct_cache_hit``, ``preprocessed_cache_hit``,
``cache_miss``, plus secondary ``local_storage_*`` / ``remote_storage_*``
events) to that file for the duration of the build.  After the build,
ct-cake parses the file and prints a one-line summary
(``ccache: cacheable=... hits=... misses=... hit_rate=...%``) to the
build log regardless of whether OTLP export is on.

Values:

**--ccache-statslog auto** (or the flag with no value)
    Allocate the log at ``<diagnostics-dir>/ccache.statslog`` (alongside
    ``timing.json``; see ``--diagnostics-dir`` in ``ct-cake`` (1)).  The
    file is removed after the post-build ingest.

**--ccache-statslog PATH**
    Use an explicit path (made absolute relative to the invocation cwd).
    The file's lifecycle is the caller's responsibility — ct-cake does
    not remove it.

When combined with ``--otel-export``, the parsed counts ship as OTLP
metrics:

============================= ========= =================================================================
Metric                        Kind      Notes
============================= ========= =================================================================
``ct.ccache.events``          counter   One observation per distinct event name, tagged
                                        ``ccache_event=<name>``, with that event's total count for the
                                        build.  Zero-count events are skipped.
``ct.ccache.hit_rate``        gauge     Local hit ratio in ``[0, 1]``:
                                        ``(direct + preprocessed) / cacheable``.
``ct.ccache.remote_hit_rate`` gauge     Remote-backend hit ratio in ``[0, 1]``:
                                        ``remote_storage_hit / (remote_hit + remote_miss)``.
============================= ========= =================================================================

The headline ccache numbers are additionally lifted onto the root build
span as ``ct.ccache.*`` attributes.

``--ccache-statslog`` is **allowed without** ``--otel-export`` — the
statslog file and the one-line summary are useful on their own.  In that
mode no metrics are shipped, and at verbosity ``-v`` (or higher) ct-cake
prints ``Note: --ccache-statslog set without --otel-export; statslog
written but no metrics shipped.`` to stderr.

CAS-HEALTH GAUGES
-----------------

``ct-cache-report --otel-export`` scans the content-addressable stores
and emits five gauges per scanned store, tagged ``cas_kind`` (one of
``obj`` / ``pch`` / ``pcm`` / ``exe``).  The natural deployment is a
cron or post-build hook per CAS-bearing host.

=========================== ===========================================================================
Gauge                       Meaning
=========================== ===========================================================================
``ct.cas.total_bytes``      Total on-disk size of the store.
``ct.cas.total_entries``    Number of cache entries.
``ct.cas.unique_buckets``   Distinct logical keys (src+deps for obj, header for pch, bucket for
                            pcm/exe) — collapsed to one canonical metric across store kinds.
``ct.cas.wasted_bytes``     Bytes attributable to duplicate / superseded entries.
``ct.cas.duplicate_groups`` Number of groups containing more than one entry for the same key.
=========================== ===========================================================================

A store directory that was not scanned contributes no rows; a scanned
but empty store contributes one zero-valued row per gauge — "I scanned,
found nothing" is signal distinct from "I didn't scan".  If none of the
four stores were scanned the export is a silent no-op.

EXAMPLES
========

Local OpenTelemetry Collector
-----------------------------

Run the upstream collector with the HTTP receiver exposed::

    docker run --rm -p 4317:4317 -p 4318:4318 \
        -v "$PWD/otel-collector-config.yaml:/etc/otelcol/config.yaml" \
        otel/opentelemetry-collector:latest

A minimal ``otel-collector-config.yaml`` that prints spans to the
collector's stdout::

    receivers:
      otlp:
        protocols:
          grpc:
          http:
    exporters:
      debug:
        verbosity: detailed
    service:
      pipelines:
        traces:
          receivers: [otlp]
          exporters: [debug]

Build and export::

    ct-cake --auto --timing --otel-export \
        --otel-protocol=http \
        --otel-endpoint=http://localhost:4318 \
        --otel-insecure

(The ``--otel-insecure`` flag is gRPC-only but harmless under
``http``.)

Grafana Tempo
-------------

Tempo's default OTLP gRPC receiver listens on ``:4317``::

    ct-cake --auto --timing --otel-export \
        --otel-endpoint=http://tempo.internal:4317 \
        --otel-resource-attr deployment.environment=ci \
        --otel-resource-attr ci.job=$CI_JOB_ID

Jaeger
------

Jaeger 1.35+ accepts OTLP directly::

    ct-cake --auto --timing --otel-export \
        --otel-endpoint=http://jaeger.internal:4317

Honeycomb
---------

Honeycomb requires an API key in the request headers::

    ct-cake --auto --timing --otel-export \
        --otel-protocol=http \
        --otel-endpoint=https://api.honeycomb.io \
        --otel-headers="x-honeycomb-team=$HONEYCOMB_API_KEY" \
        --otel-service-name=ci-builds

ccache metrics and CAS-health gauges
------------------------------------

Ship ccache stats alongside the build trace and metrics::

    ct-cake --auto --timing --otel-export \
        --ccache-statslog=auto \
        --otel-endpoint=http://otel-collector.internal:4317

Snapshot CAS-store health on a cron, to the same collector::

    ct-cache-report --otel-export \
        --otel-endpoint=http://otel-collector.internal:4317

Against a trace-only collector, flatten the metric families into spans::

    ct-cake --auto --timing --otel-export \
        --ccache-statslog=auto --otel-metrics-as-spans \
        --otel-endpoint=http://tempo.internal:4317

CI integration via configargparse env vars
------------------------------------------

The most ergonomic pattern for CI: set the OTLP destination in the
environment once and leave the ct-cake command line clean::

    export OTEL_EXPORT=true
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.ci.internal:4317
    export OTEL_RESOURCE_ATTRIBUTES="ci.pipeline=$CI_PIPELINE_ID,ci.job=$CI_JOB_ID"
    ct-cake --auto --timing

(``OTEL_EXPORT`` is the configargparse uppercased form of
``--otel-export``; ``OTEL_EXPORTER_OTLP_ENDPOINT`` and
``OTEL_RESOURCE_ATTRIBUTES`` are OpenTelemetry-SDK standard env vars
read directly by the SDK — ct-cake does not pick them up into
``--otel-endpoint``, leaving the SDK's full precedence chain in
charge.)

Per-project default via ct.conf
-------------------------------

In ``<gitroot>/ct.conf.d/ct.conf``::

    otel-export = true
    otel-endpoint = http://otel-collector.internal:4317
    otel-service-name = myproject-builds
    otel-resource-attr = team=platform
    otel-resource-attr = component=build

Each ct-cake invocation in the project then exports automatically; a
developer can still disable it on the CLI with ``--no-otel-export``.

TROUBLESHOOTING
===============

``RuntimeError: compiletools OTel export requested but the opentelemetry SDK is not installed``
    The ``otel`` extra is not installed in the active venv.  Install
    with ``pip install 'compiletools[otel]'`` (or
    ``uv pip install 'compiletools[otel]'``).

Export is silent — no spans show up in the collector
    ``--otel-export`` implies ``--timing``, so an empty span tree from a
    "forgot ``--timing``" misconfiguration is not possible.  Verify the collector endpoint with a curl
    against the OTLP HTTP receiver path
    (``http://<host>:4318/v1/traces``).  Run ct-cake at ``-vv`` to see
    the resolved CLI values and confirm the flags actually took effect
    in the configargparse hierarchy.

gRPC TLS errors against a localhost collector
    Add ``--otel-insecure`` (gRPC only).  The default is to verify
    TLS, which a plain-HTTP local collector cannot satisfy.

Span timestamps look implausible (e.g. 1970)
    Should not happen — timestamps are derived from
    ``BuildTimer._wall_to_monotonic_offset``, which captures both
    clocks at the same instant in ``BuildTimer.__init__``.  File an
    issue with the affected ``timing.json`` attached.

SEE ALSO
========
``ct-cake`` (1), ``ct-cache-report`` (1), ``ct-timing-report`` (1), ``ct-config`` (1), ``compiletools`` (1)

The OpenTelemetry specification: https://opentelemetry.io/docs/specs/otel/

The OTLP protocol: https://opentelemetry.io/docs/specs/otlp/
