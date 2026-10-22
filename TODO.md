# TODO

## Current Objective

Get the 22 failing tests green. Triage done: all 4 root causes are environment mismatches (Termux/Android), no code regressions. Fix = make the tests environment-aware using repo conventions.

## In Progress

- [ ] Group 1 (14 tests): `os.link` missing on Android — add repo's existing `skipif(not hasattr(os, "link"))` guard to TestPurgeCaSiblingsCli, TestPublishedCasEntryFreshening, TestFreshenedEntryStaysUpToDate (hardlink param only if class is parametrized), TestPublishPermissionDiagnostic (owner: worker subagent)

## Next

- [ ] Group 2 (5 tests): TestUndefinedPkgConfigVariables — local pkg-config is freedesktop 0.29.2, not pkgconf; gate class on its premise (pkgconf exits 0 with truncated flag)
- [ ] Group 3 (2 tests): TestEmptyBindirModuleMapper — `g++` is clang on Termux; skipif/monkeypatch so the gcc mapper branch is only tested when g++ is gcc
- [ ] Group 4 (1 test): test_an_oversized_shift_allocates_nothing — 512 MiB RLIMIT_AS below ~10.9 GB baseline VmSize; make cap dynamic (baseline + margin, still below the 1.25 GB regression allocation)
- [ ] Full-suite verification: pytest -n auto

## Blocked

(none)
