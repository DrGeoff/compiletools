# TODO

## Current Objective

(none)

## In Progress

(none)

## Next

(none)

## Blocked

(none)

## Done

- [x] Get the 22 failing tests green. Triage: all 4 root causes were environment mismatches (Termux/Android), no code regressions. Fix = make the tests environment-aware using repo conventions.
  - [x] Group 1 (14 tests): `os.link` missing on Android — `skipif(not hasattr(os, "link"))` guards on TestPurgeCaSiblingsCli, TestPublishedCasEntryFreshening, TestFreshenedEntryStaysUpToDate (hardlink param), TestPublishPermissionDiagnostic
  - [x] Group 2 (5 tests): TestUndefinedPkgConfigVariables — gated on its premise (local pkg-config silently expands undefined `.pc` variables; freedesktop 0.29.2 errors instead)
  - [x] Group 3 (2 tests): TestEmptyBindirModuleMapper — skipped unless `compiler_kind("g++")` is really gcc (Termux ships clang as g++)
  - [x] Group 4 (1 test): test_an_oversized_shift_allocates_nothing — skipped when the import itself exceeds the 512 MiB RLIMIT_AS cap (child prints IMPORT_OK before evaluating)
  - [x] Full-suite verification: `scripts/ct-pytest-monitor` (NOT `pytest -n auto` — unusable on Termux): 5943 passed, 408 skipped, 3 xfailed, 0 failed
