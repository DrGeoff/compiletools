# testprefix Sample

Demonstrates the `TESTPREFIX` setting, which prepends a command in
front of every test invocation when ct-cake runs the test phase.

## Why

Lets you wrap every test in `valgrind` (memory correctness),
`timeout` (runtime bound), `gdb --batch` (auto-postmortem), or any
other instrumentation tool, without modifying the test binaries
themselves. A non-zero exit from the wrapped command is treated as
a build failure.

## Run

```bash
./build.sh
```

The wrapper for this sample is `timeout 5`. After ct-cake builds
`bin/<variant>/test_quick`, it runs:

```
timeout 5 bin/<variant>/test_quick
```

If the wrapped test exceeds 5 seconds, `timeout` kills it and exits
non-zero, which ct-cake reports as a test failure.

## Setting `TESTPREFIX`

Three equivalent ways:

1. Environment variable: `TESTPREFIX="timeout 5" ct-cake --auto`
2. CLI flag: `ct-cake --auto --TESTPREFIX="timeout 5"`
3. In a `ct.conf` (per-project, per-user, or per-variant):
   ```ini
   TESTPREFIX = valgrind --quiet --error-exitcode=1
   ```
