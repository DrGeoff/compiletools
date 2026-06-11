=========================
ct-debug-pcm-hash-inputs
=========================

----------------------------------------------------------------------------------
Dump the inputs that drive a C++20 module BMI's ``<cas-pcmdir>/<cmd_hash>/`` path
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-31
:Version: 10.1.8
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-debug-pcm-hash-inputs [--stage STAGE] FILE [FILE ...]

DESCRIPTION
===========
``ct-debug-pcm-hash-inputs`` reports, as a single JSON object on stdout,
every input that ``BuildBackend._compute_pcm_command_hash`` feeds into
the BMI cache key for one or more C++20 module sources -- plus the
16-hex ``cmd_hash`` that key collapses to.

The tool exists for one workflow: a user files a report saying "two
back-to-back ``ct-cake`` invocations on unchanged source landed the BMI
at a fresh ``<cas-pcmdir>/<variant>/<cmd_hash>/`` subdir." The
``cmd_hash`` is a pure function of seven inputs; if two runs disagree
on the final hash, exactly one of those inputs must have drifted. This
tool exposes them so ``diff`` does the diagnostic work::

    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run1.json
    # ... second ct-cake invocation, no source changes ...
    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run2.json
    diff /tmp/run1.json /tmp/run2.json

Any non-empty diff names the drifting input directly -- no source
spelunking, no ``pdb`` session.

Output fields
-------------
The seven hash inputs (the same set ``_pcm_command_hash`` consumes):

``compiler_identity``
    ``<realpath>|<size>|<mtime_ns>`` of the resolved CXX binary,
    canonicalised against the gitroot anchor so in-workspace wrapper
    scripts (coverage shims, ``sccache``/``distcc`` wrappers) do not
    leak per-checkout absolute paths into the key.

``cxx_command``
    The CXX command string, canonicalised against the gitroot anchor.

``cxxflags_tokens``
    ``args.flags.hash_relevant("cxx")`` -- the user's CXXFLAGS with
    ``-D``/``-U`` and diagnostic-only flags stripped, then
    canonicalised. The frozen ``Flags`` dataclass is set once per
    parseargs; two back-to-back runs MUST agree on this tuple unless
    something outside compiletools is mutating the env between runs.

``magic_cpp_flags`` / ``magic_cxx_flags``
    Per-file flags lifted by ``Hunter.magicflags(source)`` from
    ``//#`` annotations and pkg-config probes. Filtered through
    ``filter_hash_irrelevant_tokens`` and canonicalised the same way
    the hash function applies them.

``source``
    The canonicalised realpath of the source file.

``transitive_content_hash``
    ``"<source_hash>:<dep_hash>"``. ``source_hash`` is the file's
    content hash from ``global_hash_registry``; ``dep_hash`` is the
    14-hex XOR fold of every transitive header's content hash. For
    a template-only module with no ``#include`` and no ``import``,
    ``dep_hash`` is the constant ``"0" * 14``.

Plus, for triage convenience:

* ``anchor_root`` -- the ``find_git_root()`` value used for
  canonicalisation. Cross-workspace divergence usually shows up here
  first.
* ``deplist`` -- sorted absolute paths of the headers walked into
  ``dep_hash``.
* ``cmd_hash`` -- the final 16-hex truncated sha256 the cache layer
  uses as the ``<cmd_hash>/`` subdir name.

Stage detection
---------------
Default ``stage`` is ``gcc_module_interface`` when CXX resolves to a
gcc, ``clang_module_interface`` when it resolves to a clang. Pass
``--stage`` explicitly to override -- e.g. ``--stage=gcc_header_unit``
when triaging a header-unit cache key. Header-unit auto-detection from
filename is deliberately NOT implemented; the bug-report flow this
tool serves is about named modules, and a heuristic from "is this a
``.cppm`` or a ``.h``" would silently misclassify the boundary case
where a project imports a ``.cppm`` as a header unit.

OPTIONS
=======
``--stage STAGE``
    Override the auto-detected hash stage. One of
    ``gcc_module_interface``, ``clang_module_interface``,
    ``gcc_header_unit``, ``clang_header_unit``. Default picks one of
    the two ``*_module_interface`` stages based on the resolved CXX.

``FILE [FILE ...]``
    One or more module source files to inspect. A bare ``.cppm`` is
    typical. Output is a single JSON object for one file, or a JSON
    array of one object per file for multiple inputs.

In addition, the standard ``apptools.create_parser`` set is registered
(``--variant``, ``--CXX``, ``--CXXFLAGS``, ``--append-CXXFLAGS``, the
``--prepend-*`` / ``--append-*`` accumulators, ``--config``,
``--verbose``, ``--man``, ``--version``, ``-?``). The tool is
read-only and does not modify any CAS dir.

EXIT CODES
==========
0
    Success.
2
    Argument-parsing failure (e.g. an unknown flag, an invalid
    ``--stage`` value).

EXAMPLES
========
**Dump the BMI hash inputs for a module**::

    ct-debug-pcm-hash-inputs rounding.cppm

**Triage a "BMI landed at a new subdir on no-op rebuild" report**::

    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run1.json
    ct-cake  # do whatever the user reports triggers the drift
    ct-debug-pcm-hash-inputs rounding.cppm > /tmp/run2.json
    diff /tmp/run1.json /tmp/run2.json

**Confirm a flag change moves the hash as expected**::

    ct-debug-pcm-hash-inputs rounding.cppm | jq .cmd_hash
    ct-debug-pcm-hash-inputs --append-CXXFLAGS=-O3 rounding.cppm | jq .cmd_hash
    # The two cmd_hashes MUST differ.

**Inspect the header-unit hash for the same source**::

    ct-debug-pcm-hash-inputs --stage=gcc_header_unit some_header.h

SEE ALSO
========
``ct-cache-report`` (1) -- reports occupancy and duplication across the
content-addressable cache directories, including cas-pcmdir bucket
analysis.

``ct-trim-cache`` (1) -- evicts aged ``<cmd_hash>/`` entries from
cas-pcmdir based on per-bucket retention.

``ct-cake`` (1) -- the build orchestrator; its
``--cas-pcmdir`` flag controls where the BMI cache this tool's
``cmd_hash`` indexes into actually lives.
