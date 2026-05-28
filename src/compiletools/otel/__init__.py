"""OpenTelemetry export surface for compiletools.

Public symbols are re-exported from submodules once they land:

- ``export_buildtimer`` (from ``traces``).

Future PRs will add ``export_cache_metrics`` / ``export_ccache_metrics``
(from ``metrics``) and ``derive_build_aggregates`` (from ``aggregates``).

``_connection`` is private to the subpackage. Tests may import it
directly; no other production caller should.
"""

from compiletools.otel.traces import export_buildtimer

__all__ = ["export_buildtimer"]
