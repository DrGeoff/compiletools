# AGENTS.md

Guidance for any coding agent (Codex, Cursor, Gemini, etc.) working in the `compiletools` repository.

## Canonical docs — read these first

This file is intentionally thin. The authoritative, maintained guidance lives in:

- **`CLAUDE.md`** (repo root) — orientation layer: overview, worktree/venv rules, build & test commands, build flow, magic detection, the `args.flags` invariant, CAS layers, locking invariants, variant resolution, and config-file priority.
- **`src/compiletools/CLAUDE.md`** (auto-loaded for source edits) — the deep architecture rationale behind the invariants summarized above.
- Module **docstrings** — code-level details.

Treat `CLAUDE.md` as the single source of truth. Where this file and `CLAUDE.md` ever disagree, `CLAUDE.md` wins; do not re-summarize its invariants here (a second copy drifts silently — there is no test guarding agreement between the two).

## Quick-reference commands

```bash
uv pip install -e ".[dev]"     # dev deps; run inside this worktree's own venv
ct-check-venv                  # verify you're importing THIS worktree's source
prek install                   # one-time per checkout
pytest -n auto                 # full parallel test run
ruff check src/compiletools/   # lint
ruff format src/compiletools/  # format
pyright src/compiletools/      # type-check
prek run --all-files           # all pre-commit hooks
```

Build/cache tools: `ct-cake` (build), `ct-cache-report`, `ct-trim-cache`, `ct-cleanup-locks`.

## The one gotcha worth repeating

Each git worktree under `compiletools/` needs **its own venv**. Editable installs record the install path, so a venv created in `master/` keeps importing `compiletools` from `master/src/` even after you `cd` into another worktree — silently exercising the wrong code. Run `ct-check-venv` to confirm. Everything else — CAS keys, locking, variant composition, `wrappedos` skip cases, C++20 module naming, test conventions — is documented in `CLAUDE.md` and `src/compiletools/CLAUDE.md`.
