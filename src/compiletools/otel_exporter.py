"""End-of-build OpenTelemetry (OTLP) exporter for BuildTimer.

Walks the in-memory :class:`~compiletools.build_timer.BuildTimer` tree
once the build has finished and ships one OTel span per
``TimingEvent`` to an OTLP collector.  Timestamps are converted from
BuildTimer's monotonic clock to wall-clock nanoseconds using the
offset captured at ``BuildTimer.__init__``, so spans align with
everything else in the user's observability stack.

The OpenTelemetry SDK is imported lazily.  Install the optional
``otel`` extra (``pip install 'compiletools[otel]'``) to enable; with
the flag off and the extra missing, this module's import is free.
"""

from __future__ import annotations

from compiletools.otel._connection import (
    _OTLP_HTTP_TRACES_PATH,  # noqa: F401
    _build_processor,  # noqa: F401
    _ensure_http_traces_path,  # noqa: F401
    _invocation_id_from_diag_dir,  # noqa: F401
)
from compiletools.otel._connection import (
    DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS as _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS,  # noqa: F401
)
from compiletools.otel._connection import (
    MISSING_EXTRA_HINT as _MISSING_EXTRA_HINT,  # noqa: F401
)
from compiletools.otel._connection import (
    build_resource as _build_resource,  # noqa: F401
)
from compiletools.otel._connection import (
    get_git_commit_sha as _get_git_commit_sha,  # noqa: F401
)
from compiletools.otel._connection import (
    parse_kv_pairs as _parse_kv_pairs,  # noqa: F401
)
from compiletools.otel._connection import (
    resolve_gitroot as _resolve_gitroot,  # noqa: F401
)
from compiletools.otel.traces import (
    _emit_event,  # noqa: F401
    _rule_span_name,  # noqa: F401
    _to_wall_ns,  # noqa: F401
    export_buildtimer,  # noqa: F401
)
