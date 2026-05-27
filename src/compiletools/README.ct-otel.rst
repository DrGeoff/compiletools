========
ct-otel
========

------------------------------------------------------------
Export ct-cake build timing as OpenTelemetry (OTLP) traces
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-26
:Version: 10.0.9
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cake --auto --timing --otel-export [--otel-endpoint URL]
[--otel-service-name NAME] [--otel-protocol grpc|http]
[--otel-resource-attr K=V ...] [--otel-headers K=V,K=V]
[--otel-insecure]

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

The exporter is a pure end-of-build batch step.  No spans are emitted
during the build itself.  Each OTLP request is bounded by a 5-second
per-request timeout, so a slow or unreachable collector adds at most a
handful of seconds (one bounded request per retry) to end-of-build, and
a failed export does not fail the build.

The OpenTelemetry SDK is an optional dependency.  Default installs of
compiletools do not pull it in, and ``--otel-export`` is off by
default.  Install the optional extra to enable::

    pip install 'compiletools[otel]'

Without the extra, ``--otel-export`` raises ``RuntimeError`` with the
install hint.  With the flag off, the SDK is never imported and adds
zero startup cost.

REQUIREMENTS
============

``--otel-export`` requires ``--timing`` (the span tree must exist
before there is anything to export).  Without ``--timing`` the
exporter is a no-op.

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
    Enable end-of-build OTLP export.  Default: off.  Requires
    ``--timing``; passing ``--otel-export`` without ``--timing`` emits
    a stderr warning and exports nothing (no span tree to ship).

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

Standard ``OTEL_*`` environment variables consulted by the
OpenTelemetry SDK itself — ``OTEL_EXPORTER_OTLP_ENDPOINT``,
``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_EXPORTER_OTLP_INSECURE``,
``OTEL_RESOURCE_ATTRIBUTES``, ``OTEL_SERVICE_NAME`` — are honoured for
any field the corresponding ``--otel-*`` flag leaves unset.

SPAN MODEL
==========

The exporter emits one OpenTelemetry span per ``TimingEvent`` in the
``BuildTimer`` tree, preserving parent/child via the SDK's ``Context``
propagation.  Timestamps are wall-clock nanoseconds derived from
BuildTimer's monotonic timeline via the wall-to-monotonic offset
captured at ``BuildTimer.__init__``, so the resulting trace aligns
with the rest of an observability stack.

================ ============================== =====================================================================================================
Level            Span name                       Notable attributes
================ ============================== =====================================================================================================
Root             ``compiletools.build``         Resource: ``service.name``, ``service.namespace=compiletools``, ``host.name``,
                                                ``git.commit.sha``, ``ct.variant``, ``ct.backend``, ``ct.invocation_id``
Phase            ``phase.<name>``               No span attrs (phase carries its name in the span name)
Rule (compile)   ``compile.<basename>``         ``ct.rule_type=compile``, ``ct.target``, ``ct.source``
Rule (link)      ``link.<basename>``            ``ct.rule_type=link``, ``ct.target`` (``ct.source`` omitted for link rules)
Rule (other)     ``<rule_type>.<basename>``     ``ct.rule_type``, ``ct.target``, and ``ct.source`` when present
================ ============================== =====================================================================================================

``ct.invocation_id`` is the basename of the per-invocation diagnostics
directory (``--diagnostics-dir``'s ``<invocation-id>`` segment), so a
span in the collector can be cross-referenced one-to-one with the
``timing.json`` artefact ct-cake writes alongside it.

``git.commit.sha`` is read via ``git rev-parse HEAD`` with a 2-second
timeout; an empty value is dropped from the Resource if git is not
available.

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
    First, confirm ``--timing`` is also on (``--otel-export`` is a no-op
    without a timing tree to export).  Then verify the collector
    endpoint with a curl against the OTLP HTTP receiver path
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
``ct-cake`` (1), ``ct-timing-report`` (1), ``compiletools`` (1)

The OpenTelemetry specification: https://opentelemetry.io/docs/specs/otel/

The OTLP protocol: https://opentelemetry.io/docs/specs/otlp/
