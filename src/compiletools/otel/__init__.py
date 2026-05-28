"""OpenTelemetry export surface for compiletools.

Public symbols are re-exported from submodules:

- ``export_buildtimer`` (from ``traces``) -- ships the in-memory
  BuildTimer span tree as OTLP spans at the end of a build.
- ``export_cache_metrics`` (from ``metrics``) -- ships CAS-health gauges
  for ``ct-cache-report --otel-export``.
- ``export_ccache_metrics`` (from ``metrics``) -- ships parsed ccache
  stats counts as OTLP metrics. Used by ct-cake when ``--ccache-statslog``
  is set together with ``--otel-export``.

Future PRs will add ``derive_build_aggregates`` (from ``aggregates``, P5).

``_connection`` is private to the subpackage. Tests may import it
directly; no other production caller should.
"""

from compiletools.otel.metrics import export_cache_metrics, export_ccache_metrics
from compiletools.otel.traces import export_buildtimer

__all__ = ["export_buildtimer", "export_cache_metrics", "export_ccache_metrics"]
