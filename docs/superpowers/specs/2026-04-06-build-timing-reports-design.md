# Build Timing Reports -- Design Spec

## Problem

When a build is slow, users have no way to know *where* the time went. Is it a single translation unit with an explosion of includes? The linker? Dependency analysis? Without timing data, users guess.

## Solution Overview

A three-layer system:

1. **Data collection** -- `BuildTimer` class instruments build phases and per-file compile/link times
2. **Persistent data** -- JSON timing file (`.ct-timing.json`) written after each build
3. **Viewers** -- inline Rich summary after builds, interactive ncdu-style Textual TUI (`ct-timing-report`), Chrome Trace export for Perfetto

## Architecture

### Layer 1: BuildTimer (`build_timer.py`)

New module. Self-contained (stdlib + rich only, no compiletools imports that would cause cycles).

```python
@dataclass
class TimingEvent:
    name: str            # e.g., "compile", "link", "build_graph"
    category: str        # "phase" | "compile" | "link" | "static_library" | ...
    start_s: float       # time.monotonic()
    end_s: float | None  # None while running
    target: str = ""     # output file for compile/link events
    source: str = ""     # source file for compile events
    children: list[TimingEvent]
    metadata: dict[str, Any]
```

**BuildTimer** class:

- `__init__(enabled=False, variant="", backend="")` -- when `enabled=False`, all methods are no-ops (zero overhead on normal builds)
- `phase(name) -> contextmanager` -- pushes a phase onto a stack, records start/end via `time.monotonic()`
- `record_rule(rule_type, target, source, elapsed_s, start_s=None, end_s=None)` -- appends a rule event to the current phase. Thread-safe (uses `threading.Lock`) for Shake backend's thread pool
- `record_rules_from_ninja_log(log_path, offset=0, graph=None)` -- parses `.ninja_log` entries appended after `offset` bytes
- `to_dict() -> dict` -- serializes to JSON-compatible dict
- `to_json(path)` -- writes to file via `atomic_output_file`
- `to_chrome_trace() -> list[dict]` -- Chrome Trace Event Format
- `summary_table() -> rich.table.Table` -- Rich table for inline printing
- `from_dict(data) -> BuildTimer` -- class method, deserializes from JSON

### Layer 2: Integration Points

**BuildContext** (`build_context.py`): Add `self.timer: BuildTimer | None = None` attribute. Type import under `TYPE_CHECKING`.

**Cake** (`cake.py`):
- `--timing` flag added to argument parser
- `Cake.__init__`: creates `BuildTimer(enabled=args.timing)`, assigns to `self.context.timer`
- `Cake.process`: wraps the overall process in `timer.phase("total")`; on exit (including failures, via `finally`), writes `.ct-timing.json` and prints summary
- `Cake._call_backend`: wraps `build_graph()`, `generate()`, `execute("build")`, `execute("runtests")` in `timer.phase(...)` calls

**NinjaBackend** (`ninja_backend.py`):
- Before calling ninja, record `.ninja_log` file size
- After ninja completes, call `timer.record_rules_from_ninja_log(log, offset=saved_size)`

**ShakeBackend** (`trace_backend.py`):
- In `_execute_rule()`: wrap with `time.monotonic()` bookends, call `timer.record_rule()`
- Thread-safe via the lock in BuildTimer

**MakefileBackend** (`makefile_backend.py`):
- When `--timing` is enabled, `_format_recipe()` wraps each compile/link command with a shell timing preamble that appends a JSONL line to `{objdir}/.ct-make-timing.jsonl`:
  ```
  @_start=$$(date +%s%N); <original_cmd>; _end=$$(date +%s%N); echo "{\"target\":\"$$@\",\"start_ns\":$$_start,\"end_ns\":$$_end}" >> .ct-make-timing.jsonl
  ```
- After `subprocess.check_call()` returns, `BuildTimer.record_rules_from_make_timing(log_path, graph)` parses the JSONL and calls `record_rule()` for each entry
- The JSONL file is cleaned up after parsing
- Falls back gracefully if `date +%s%N` is unavailable (GNU coreutils `date` supports nanoseconds; BusyBox does not, but is not a target platform)

**SlurmBackend** (`trace_backend.py`):
- After `_wait_for_arrays()` completes, a new method `_collect_timing(index_map)` queries `sacct` with `--format=JobID,Elapsed,Start,End,State --parsable2` for each array job ID
- Maps task indices back to `BuildRule` objects via the existing `index_map` dict
- Calls `timer.record_rule()` for each completed task with elapsed time parsed from sacct's `Elapsed` field (format: `HH:MM:SS` or `D-HH:MM:SS`)
- Phase 5 (local link/library rules) is also timed per-rule via `time.monotonic()` wrapping of `_run_local()`

### Layer 3: Viewers

#### Inline Summary

When `--timing` is active, print after build:

```
Build Timing Report (45.2s total)
+-----------------------+----------+--------+
| Phase                 | Time (s) |    %   |
+-----------------------+----------+--------+
| Config resolution     |     0.12 |   0.3% |
| Target discovery      |     0.34 |   0.8% |
| Dependency analysis   |     2.10 |   4.6% |
| Build graph           |     0.85 |   1.9% |
| Generate              |     0.03 |   0.1% |
| Build execution       |    38.50 |  85.2% |
|   Compilation         |    33.30 |  73.7% |
|   Linking             |     5.20 |  11.5% |
| Test execution        |     3.26 |   7.2% |
+-----------------------+----------+--------+
Slowest compilations:
  12.3s  src/foo.cpp (23 includes)
   8.1s  src/bar.cpp (5 includes)
```

#### Interactive TUI (`ct-timing-report`)

New entry point. Uses Textual (optional dependency under `[tui]` extra). Falls back to Rich static table if Textual not installed.

ncdu-style interface:
```
Build: 45.2s total                            [s]ort [q]uit
-------------------------------------------------------------
  38.50s  85.2% ############################  Build execution
    33.30s  73.7% ###########################  Compilation
>    12.30s  27.2% ############               src/foo.cpp
      8.10s  17.9% ########                   src/bar.cpp
      6.90s  15.3% #######                    src/baz.cpp
     5.20s  11.5% #####                       Linking
   3.26s   7.2% ###                           Test execution
   2.10s   4.6% ##                            Dependency analysis
```

Keyboard: arrows/j/k navigate, Enter/right expand, left/backspace collapse, `s` cycle sort, `q` quit, `e` export Chrome Trace, `?` help.

#### Comparison Mode

`ct-timing-report --compare before.json after.json`

Shows delta view with color coding: green for faster, red for slower. Sorted by absolute delta.

#### Chrome Trace Export

`ct-timing-report --chrome-trace output.json`

Converts `.ct-timing.json` to Chrome Trace Event Format for viewing in [Perfetto](https://ui.perfetto.dev/). Parallel compilations show as concurrent spans on different "threads".

## Data Format

File: `{objdir}/.ct-timing.json`

```json
{
  "version": 1,
  "timestamp": "2026-04-06T14:30:00.000000",
  "total_elapsed_s": 45.123,
  "variant": "gcc.debug",
  "backend": "ninja",
  "phases": [
    {
      "name": "config_resolution",
      "elapsed_s": 0.12,
      "rules": []
    },
    {
      "name": "build_execution",
      "elapsed_s": 38.50,
      "rules": [
        {
          "rule_type": "compile",
          "target": "obj/foo.o",
          "source": "src/foo.cpp",
          "elapsed_s": 12.345,
          "start_s": 0.0,
          "end_s": 12.345
        }
      ]
    }
  ]
}
```

`start_s`/`end_s` in rules are relative to the phase start, enabling Chrome trace visualization of parallel compilation.

## Ninja Log Parsing

`.ninja_log` format (tab-separated, appended per build):
```
# ninja log v5
<start_ms>\t<end_ms>\t<mtime_ms>\t<output>\t<hash>
```

Strategy: record `.ninja_log` byte offset before calling ninja. After build, parse only newly-appended lines. Classify outputs by extension (`.o` = compile, executable = link). Match source files via BuildGraph lookup.

## Flag Interaction

`--timing` is independent of `--verbose`. The `--timing` flag enables timing data collection, JSON file output, and the inline summary table. Verbose levels continue to control existing diagnostic output (compiler commands, make trace, etc.) as before. They do not affect timing behavior.

## Dependencies

- **rich** -- already a dependency, used for inline summary table
- **textual** -- new optional dependency under `[tui]` extra. Added to `[dev]` extras too
- No other new dependencies

## New Files

| File | Purpose |
|------|---------|
| `src/compiletools/build_timer.py` | BuildTimer class, TimingEvent, serialization |
| `src/compiletools/timing_report.py` | CLI entry point, TUI app, comparison, Chrome export |
| `src/compiletools/test_build_timer.py` | Unit tests for BuildTimer |
| `src/compiletools/test_timing_report.py` | Unit tests for CLI and viewer |

## Modified Files

| File | Change |
|------|--------|
| `src/compiletools/build_context.py` | Add `timer` attribute |
| `src/compiletools/cake.py` | Create timer, wrap phases, write JSON, print summary, add `--timing` flag |
| `src/compiletools/makefile_backend.py` | Wrap recipes with timing preamble when `--timing`; parse `.ct-make-timing.jsonl` after build |
| `src/compiletools/ninja_backend.py` | Parse `.ninja_log` after build |
| `src/compiletools/trace_backend.py` | Wrap `_execute_rule` with timing in ShakeBackend; add `_collect_timing()` to SlurmBackend; wrap `_run_local()` with timing |
| `pyproject.toml` | Add `ct-timing-report` entry point, `[tui]` optional dep |

## Risks

1. **Thread safety** -- Shake backend calls `record_rule()` from thread pool. Mitigated by `threading.Lock` in BuildTimer.
2. **Ninja log staleness** -- Offset-based parsing avoids stale entries from prior builds.
3. **Circular imports** -- `build_timer.py` imports only stdlib + rich. BuildContext uses TYPE_CHECKING guard.
4. **Build failures** -- Timer writes JSON in `finally` block so timing data is available even for failed builds.
5. **Textual not installed** -- Graceful fallback to Rich static table with install instructions.

## Verification

1. `pytest src/compiletools/test_build_timer.py` -- unit tests for core timer
2. `pytest src/compiletools/test_timing_report.py` -- unit tests for CLI/viewer
3. Manual: `ct-cake --auto --timing` on a sample project, verify `.ct-timing.json` written and summary printed
4. Manual: `ct-timing-report .ct-timing.json` launches TUI (or falls back gracefully)
5. Manual: `ct-timing-report --chrome-trace out.json .ct-timing.json` produces valid Perfetto input
6. Manual: `ct-timing-report --compare run1.json run2.json` shows deltas
