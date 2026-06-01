# export-template cmd_hash drift fixture

Fixture for `test_gcc_module_cmd_hash_stable_across_invocations.py`.

## The claim under test

A 2026-05-28 bug report against compiletools 10.1.1 + gcc-16 claimed
that a template-only named-module interface unit written as

```cpp
export module myproj.util.rounding;
namespace myproj {
export template <typename T> inline T roundUp(T v, T m) { /* … */ }
export template <typename T> inline T roundDown(T v, T m) { /* … */ }
}
```

(each function carries its own `export`, call this *form 1*) caused
`ct-cake` to land the gcc BMI under a fresh `<cas-pcmdir>/<cmd_hash>/`
subdir on some back-to-back invocations, while the *form 2* rewrite

```cpp
export module myproj.util.rounding;
export namespace myproj {
template <typename T> inline T roundUp(T v, T m) { /* … */ }
template <typename T> inline T roundDown(T v, T m) { /* … */ }
}
```

(whole-namespace export) hashed stably across the same set of
invocations. The report further claimed the form-1 drift cascaded into
the consumer object cache.

Source-code review of `_pcm_command_hash` (`build_backend.py`) showed
all hash inputs as deterministic functions of source bytes, compiler
identity, and frozen `args.flags`, and the `FileAnalyzer` does not
distinguish the two syntactic forms — both yield the same
`module_exports = ("myproj.util.rounding",)` with no other module
fields touched. So the reported drift, if real, would have to come
from somewhere outside the documented hash inputs.

This fixture lets the regression test run the claim end-to-end on
both forms and confirm the absence of drift.

## File layout

```
export_template_cmd_hash/
├── form1/
│   ├── ct.conf
│   ├── rounding.cppm   # `export template <T>` per declaration
│   ├── app0.cpp        # ct-exemarker, import rounding, exercises both templates
│   ├── app1.cpp        # ct-exemarker, same shape
│   └── app2.cpp        # ct-exemarker, same shape
└── form2/
    ├── ct.conf
    ├── rounding.cppm   # `export namespace { template <T> … }`
    ├── app0.cpp
    ├── app1.cpp
    └── app2.cpp
```

Three independent executables per form give the test fanout (4 .o
files per form: 3 consumer apps + 1 module interface) — small enough
to keep the suite fast, wide enough that a cascade triggered by a
drifting BMI cmd_hash would be visible as multiple new consumer .o
files in `cas-objdir`.

## What the regression test asserts

For each form, across three back-to-back `ct-cake` subprocess
invocations with varying `PYTHONHASHSEED`:

1. `cas-pcmdir/<variant>/` contains exactly one `<cmd_hash>/` subdir
   for the module. More than one means BMI cmd_hash drift.
2. The set of `.o` filenames in `cas-objdir/<variant>/**/*.o` is
   identical between invocations 1, 2, and 3. A growing set means a
   consumer-side cascade — which is what the report described as "217
   brand-new files per touch."

Both assertions run under both `--use-mtime=False` (CAS-only, default)
and `--use-mtime=True` (legacy mtime-driven) on the make backend, so
the test pins down behaviour in both rebuild regimes.

## Why not in `examples-end-to-end/`

This fixture pairs two sibling subdirs (`form1/`, `form2/`) under a
single example name and is consumed by one bespoke regression test,
not by the cross-backend `ct-cake` matrix. Each form on its own would
look like a fine end-to-end sample; what makes the fixture useful is
the *pair*, and that lives in `examples-features/` by convention.
