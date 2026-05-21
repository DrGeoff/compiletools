============
ct-cake
============

---------------------------------------------
Swiss army knife for building a C/C++ project
---------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 10.0.6
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cake [compilation args] [--variant=<VARIANT>] [--backend=<BACKEND>] filename.cpp

DESCRIPTION
===========
ct-cake is the Swiss army knife of build tools that combines many of the
compiletools into one uber-tool. For many C/C++ projects you can compile
simply using

    ``ct-cake``

ct-cake will automatically determine the correct source files to generate executables
from and also determine the tests to build and run. It works by spidering over
the source code to determine what implementation files to build, what libraries
to link against and what compiler flags to set. Only build what you
need, and throw out your Makefiles.

By default (``--auto``), ct-cake searches for files that will be the "main" file.
From that set of files, ct-cake then uses the header includes to
determine what implementation (cpp) files are also required to be built and
linked into the final executable/s. Use ``--no-auto`` to disable automatic
target detection and specify files explicitly.

ct-cake will make your life easy if you don't arbitrarily name things.
The main rules are:

   * ct-cake only builds C and C++. Everything can be done just fine with
     other tools, so there's no point reinventing them. Anyway, it's easy to
     embed ct-cake into other toolchains, see the last section.
   * All binaries end up in the bin directory, with the same base name as
     their source filename. You can override this at the command-line, but it's
     against the spirit of the tool.
   * The implementation file for point.hpp should be called point.cpp. ct-cake
     supports common C/C++ extensions for headers (.h, .hpp, .hxx, .hh) and
     implementation files (.cpp, .cxx, .cc, .c). This naming convention allows
     ct-cake to compile files and recursively hunt down their dependencies.
   * If a header or implementation file will not work without being linked
     with a certain flag, add a //#LDFLAGS=myflag directly to the source code.
   * Likewise, if a special compiler option is needed, use //#CXXFLAGS=myflag.
   * Minimise the use of "-I" include flags. They make it hard not only for
     cake to generate dependencies, but also autocomplete tools like Eclipse
     and ctags. You can avoid -I flags by structuring your code in the same way
     you refer to paths in your source code.
   * Currently only varieties of Linux are actively supported because that's
     what the compiletools developers have easy access to. That said, it stands
     a good chance of working on \*BSD and macOS. We are interested in receiving
     patches for other platforms including Windows.

ct-cake works off a "pull" philosophy of building, unlike the "push" model
of most build processes. Often, there is the monolithic build script that
rebuilds everything. Users iterate over changing a file, relinking everything
and then rerunning their binary. A hierarchy of libraries is built up and
then linked in to the final executables. All of this takes a lot of time,
particularly for C++.

With ct-cake, you only pull in what is strictly necessary to what you need to
run right now. Say, you are testing a particular tool in a large project, with
a large base of 2000 library files for string handling, sockets, etc. There
is simply no Makefile (by default ct-cake generates one behind the scenes, but
you can also use ``--backend`` to target Ninja, CMake, Bazel, or Shake).
You might want to create a build.sh for regression testing, but it's not
essential.

The basic workflow is to simply type:

    ``ct-cake``

If there are multiple executables found and you only want to build one specific one:

    ``ct-cake path/to/src/app.cpp``

Only the library cpp files that are needed, directly, or indirectly to create
./bin/app are actually compiled. If you don't #include anything that refers
to a library file, you don't pay for it. Also, only the link options that
are strictly needed to generate the app are included. Its possible to do in
make files, but such fine-level granularity is rarely set up in practice,
because its too error-prone to do manually, or with recursive make goodness.


How it Works
============

ct-cake generates the header dependencies for the "main.cpp"
file you specify at the command line by either examining the "#include" lines in
the source code (or by executing "gcc -MM -MF" if you use the --preprocess flag).
For each header file found in the source file, it looks for
an underlying implementation (c,cpp,cc,cxx,etc) file with the same name, and
adds that implementation file to the build.  ct-cake also reads the entire file
(configurable via --max-file-read-size) for "magic flags" (//#KEY=VALUE)
that indicate needed link and compile flags.  Then it recurses through the
dependencies of the cpp file, and uses this spidering to generate complete
dependency information for the application. This information is collected into a
backend-agnostic build graph, which is then handed to the selected backend
(Make by default) to generate native build files and execute the build.

Magic Comments / Magic Flags
============================

ct-cake works very differently to other build systems, which specify a hierarchy
of link flags and compile options, because ct-cake ties the compiler flags
directly to the source code. If you have compress.hpp that requires "-lzip"
on the link line, add the following comment into the header file:

``//#LDFLAGS=-lzip``

Whenever the header is included (either directly or indirectly), the -lzip
will be automatically added to the link step. If you stop using the header,
for a particular executable, cake will figure that out and not link against it.

If you want to compile a cpp file with a particular optimization enabled,
add, say:

``//#CXXFLAGS=-fstrict-aliasing``

Because the code and build flags are defined so close to each other, its
much easier to tweak the compilation locally.

CPPFLAGS and CXXFLAGS
---------------------

By default, CPPFLAGS and CXXFLAGS are unified into a single deduplicated set.
This means flags specified via ``//#CPPFLAGS=`` or ``--CPPFLAGS`` are also
applied as CXXFLAGS and vice versa. Use ``--separate-flags-CPP-CXX`` to keep
them separate if your build requires different flags for preprocessing and
compilation.

Computed Includes
-----------------

ct-cake's ``direct`` dependency mode (the default) now resolves computed
``#include`` directives where the path is specified via a macro, for example:

.. code-block:: cpp

    #define PLATFORM_HEADER "linux_extra.h"
    #include PLATFORM_HEADER

Previously this required ``--headerdeps=cpp`` to track correctly.

Macro Scope Filter (Cmdline ``-D`` Macros and Cache Keys)
---------------------------------------------------------

ct-cake's per-TU cache keys (``macro_state_hash``) include only the subset
of cmdline ``-D`` macros each TU actually references. The reference check
is implemented in ``cmdline_macro_index.py`` as a **word-boundary byte
scan** over the TU and each of its transitive headers. It does NOT
preprocess the source and it does NOT strip comments or string literals.

This means that if a header documentation comment, a deprecated macro
name in a string literal, or any other textual occurrence of a cmdline
``-D`` macro identifier appears in a transitive header, the scope filter
will include that macro in the includer's ``macro_state_hash``. Cache
keys remain *correct* (false positives are safe — they can only
over-include, never under-include), but cache *reuse* is lost: every TU
that includes such a header gets a per-value cache entry whether or not
its compiled output actually depends on the macro. ``ct-cache-report``
surfaces this as inflated duplicate-variant counts.

The trap is most painful for per-app or per-config macros injected via
``-DAPP_NAME=foo`` / ``-DAPP_NAME=bar`` style cmdline options, where the
intent is "only the one stub TU that calls ``TOSTRING(APP_NAME)`` should
fork per app." A friendly comment in the header that *describes* the
build-time macro is enough to defeat that intent and double the cache
footprint.

The robust fix is to keep the per-app/per-config value out of the
cmdline ``-D`` set entirely. There are two flavours of the
"generated file" pattern, and one is **strictly better** than the
other for build-cache reuse:

* **Generated implementation file (preferred).** A stable, checked-in
  header declares ``extern const char* const name;`` (or similar
  symbol). The build script writes a generated ``.cpp`` that
  *defines* the symbol with the per-build value. Consumers
  ``#include`` the stable header and reference the symbol; the
  linker resolves it. When the value changes:

    * the header is unchanged → ``cas-pchdir`` PCH command hashes
      and every consumer ``.o``'s ``dep_hash`` stay valid → those
      cache entries are hits;
    * only the generated ``.cpp``'s own ``.o`` invalidates;
    * the final link picks up the new symbol value.

  This is the pattern the ``examples-end-to-end/appinfo`` example
  demonstrates and is the recommended approach for any
  string-valued per-build constant.

* **Generated header (works, but worse).** Writing the value into a
  ``#define`` inside a generated ``.h`` and having one accessor TU
  ``#include`` it keeps the cmdline ``-D`` clean (so the macro
  scope filter no longer matches), but the header itself is now in
  the include graph. Every TU that transitively includes it (and
  any PCH that pulls it in) inherits the header's ``content_hash``,
  so a per-value change invalidates every downstream consumer's
  ``dep_hash``. This is fine for project-wide compile-time
  constants whose value rarely changes, but for frequently-bumped
  values (version strings, build timestamps) the impl-file pattern
  is strictly preferable.

ct-cake previously shipped two opt-in convenience hooks for the most
common cases:

* ``--project-version`` / ``--project-version-cmd`` →
  ``-DCT_PROJECT_VERSION="<version>"``
* ``--project-name`` / ``--project-name-cmd`` →
  ``-DCT_PROJECT_NAME="<name>"``

These four flags are **DEPRECATED**. They were ``-D`` injections and
so subject to the macro scope filter trap described above —
text-only mentions of ``CT_PROJECT_NAME`` / ``CT_PROJECT_VERSION``
in any transitive header (including comments documenting the macros
themselves) silently fork every includer's per-TU cache. Switching
to the generated-implementation-file pattern via
``--prebuild-script`` (see ``examples-end-to-end/appinfo``) drops
the cmdline ``-D`` entirely *and* keeps the per-build value out of
the include graph, so it's strictly better on both axes. The flags
still function for backwards compatibility but emit a one-shot
deprecation warning to stderr when used.

Pre-build and Post-build Script Hooks
-------------------------------------

For arbitrary code-gen (the generated-implementation-file pattern
above, pybind11 or SWIG binding generation, ``.proto`` / ``.fbs``
compilation) or post-build side effects (emitting a launcher script
that runs the binary in a known environment, packaging, checksum
manifests), ct-cake supports user-supplied scripts that run around
the build:

* ``--prebuild-script <cmd>`` runs after target discovery but *before*
  the build graph is constructed. Generated files (headers AND
  implementation files) written by a pre-build script are visible to
  the build that follows: Hunter walks ``#include``\ s at build-graph
  time and uses ``os.path.isfile`` for implied-source resolution, so
  a ``.cpp`` generated next to its already-included ``.h`` is picked
  up automatically by ``--auto`` (see ``examples-end-to-end/appinfo``).
  Caveat: ``--auto``'s top-level entry-point scan happens *before*
  pre-build, so a generated ``.cpp`` containing ``main()`` is not
  discovered as an entry point — list those explicitly.
* ``--postbuild-script <cmd>`` runs *after* a successful build, but
  *before* the freshly-built executables are copied to the top-level
  bindir. A non-zero exit code fails the whole invocation.

Both options accept a **shell command string** (executed via ``/bin/sh
-c``), run in the ct-cake invocation cwd, with stdout/stderr inherited
so output appears live. Both abort ct-cake with a non-zero exit on the
first script that fails. Neither runs on ``--clean`` / ``--realclean``.

Each option may be given multiple times. Like other ``action="append"``
options, the entries **accumulate** across all configuration layers
(bundled < system < venv < user < project < cwd < env < CLI) — a
project's ``ct.conf`` listing one script plus a variant ``.conf``
listing another yields both, in declaration order.

Worked example (the generated-implementation-file pattern recommended
above; full runnable version under ``examples-end-to-end/appinfo``)::

    # project ct.conf
    prebuild-script = ./tools/gen_appinfo.sh appinfo.cpp

    # appinfo.hpp (stable, checked in)
    namespace appinfo { extern const char* const version; }

    # tools/gen_appinfo.sh (writes appinfo.cpp adjacent to appinfo.hpp)
    #!/bin/sh
    cat > "$1" <<EOF
    #include "appinfo.hpp"
    namespace appinfo { const char* const version =
        "$(git describe --always --dirty)"; }
    EOF

Any TU may ``#include "appinfo.hpp"`` and read ``appinfo::version``.
The header is stable, so PCH and every consumer ``.o`` stay cached
across version bumps; only the generated ``.cpp``'s own ``.o``
invalidates.

Performance
===========

ct-cake's dependency analysis is fast — one particular (old) example project took
0.04 seconds to build if nothing is out of date, versus 2 seconds for, say,
Boost.Build. The default Make backend is about as fast as a handrolled Makefile
that uses the same lazily generated dependencies; alternative backends like Ninja
can be even faster for large incremental rebuilds.

ct-cake also eliminates the redundant generation of static archive files that
a more hierarchical build process would generate as intermediaries, saving
the cost of running 'ar'.

Note that ct-cake doesn't build all cpp files that you have checked out, only
those strictly needed to build your particular binary, so you only pay for what
you use. This difference alone should see a large improvement on most
projects, especially for incremental rebuilds.

File Locking
------------

ct-cake supports file locking that enables multiple users and build
hosts to share compiled object files. This significantly speeds up builds in
multi-developer and CI/CD environments by reusing object files across builds.

Enable by setting ``file-locking = true`` in your configuration file. This adds
filesystem-aware locking to ensure safe concurrent access (flock on local filesystems,
atomic mkdir on network filesystems). The cache uses content-addressable storage
(files named by hash of source + compiler flags) and includes automatic stale lock
detection for crashed builds.

See the main compiletools README for setup details.

Precompiled Header Caching
---------------------------

ct-cake supports content-addressable caching of precompiled headers (PCH) to
speed up builds that reuse common header files across multiple targets. Mark
headers for precompilation using the ``//#PCH=`` magic flag in your source files:

.. code-block:: cpp

    //#PCH=stdafx.h
    #include "stdafx.h"

The build system compiles PCH-marked headers into ``.gch`` files, cached in
``{git_root}/cas-pchdir/{variant}`` (or a custom path via ``--cas-pchdir``).
The cache key includes compiler, compiler flags, and header path, preventing
collisions across different build configurations.

Enable PCH caching by:

* Setting ``cas-pchdir = {git_root}/cas-pchdir`` in your ``ct.conf.d/ct.conf``
  to enable caching for all builds (default behavior in v8.0+)
* Passing ``--cas-pchdir=/path/to/cache`` on the command line to override
  for a single build

PCH caching is especially effective in multi-developer environments where
the same headers are precompiled many times. Use ``ct-trim-cache --cas-pchdir-only``
to selectively clean aged PCH entries while preserving active builds. Without
``--cas-pchdir``, PCH files fall back to legacy ``.gch`` placement in the object
directory.

C++20 Modules Caching
---------------------

ct-cake supports content-addressable caching of C++20 module BMI artefacts
(clang ``.pcm``, gcc ``.gcm``) at ``{git_root}/cas-pcmdir/{variant}/``. The
layout mirrors ``cas-pchdir`` exactly: one ``{command_hash}/`` directory per
unique compile configuration, holding the BMI plus a sidecar
``manifest.json``. Caching is automatic when ct-cake detects ``import`` /
``export module`` in your sources -- no opt-in flag required beyond
``--cas-pcmdir`` (which defaults to the per-variant location above).

Enable or override the cache location:

* Default (no action needed): ``{git_root}/cas-pcmdir/{variant}/`` per
  worktree.
* Override per-build: ``--cas-pcmdir=/path/to/cache``.
* Override in config: ``cas-pcmdir = ...`` in ``ct.conf.d/ct.conf``.

Use ``ct-trim-cache --cas-pcmdir-only`` to clean aged module-cache
entries; it understands the same bucket / max-age / transitive-staleness
policy as the PCH trim path.

Why single ``command_hash``, not the object cache's three-component path?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Object files use a three-component filename
(``{basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o``)
because there is **no in-band verification of object content at link
time**. The linker links whatever bytes the cache returns, so a hash
collision could cause a silent miscompile. Three independent
hashes -- 168 bits of entropy total -- make collisions statistically
impossible.

PCM and PCH BMIs are handled differently by the compiler. Both GCC and
clang record the compile environment (compiler version, language
standard, ABI flags, target triple, etc.) inside the BMI itself. At
consume time, the compiler verifies the recorded environment against
the consumer's environment and rejects on mismatch. This in-band
verification means a hypothetical 64-bit ``command_hash`` collision
degrades to a slow re-precompile (the compiler rejects the cached BMI,
ct-cake's build re-runs the precompile rule), **never a miscompile**.
PCH has used the single-``command_hash`` + manifest design from day
one for this reason; PCM follows the same pattern.

Per-compiler placement details
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* **clang** writes ``.pcm`` files directly to the cache path via
  ``--precompile -o <pcm_path>``. Importers reference the cached path
  via ``-fmodule-file=<name>=<pcm_path>``.
* **gcc** is steered by a per-makefile mapper file
  (``{dirname(makefilename)}/.module-mapper.txt``) generated each
  build. Each line maps a module name -- or, for header units, the
  resolved absolute system-header path -- to its cache path. gcc's
  ``-fmodule-mapper`` flag is automatically injected into every gcc
  compile command in the build. The per-makefile placement avoids a
  race that ``{cas-objdir}/.module-mapper.txt`` would have when two
  parallel ``ct-cake`` invocations target the same variant with
  different module sets.

Cache keys are workspace-path-independent
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All four CAS keys (per-TU object, PCH, PCM, linker-artefact) hash
*gitroot-relative* paths instead of absolute paths. An identical
translation unit built in ``/scratch/run-1/repo/`` and
``/scratch/run-2/repo/`` therefore produces the same cache key, and
the second build hits the cache instead of recompiling. Cloning a
repo to a new path, renaming a worktree, or relocating a shared
cache directory between hosts no longer invalidates object, PCH,
PCM, or linker-artefact entries.

The canonicalizer applies to path-bearing flag tokens (``-I``,
``-isystem``, ``-iquote``, ``-idirafter``, ``-F``, ``-B``,
``-include``, ``-include-pch``) and to the source path. System
headers, sibling repos, and already-relative paths pass through
unchanged. The compiler still receives the real absolute paths --
canonicalization only affects what the cache hashes.

After gitroot rewriting, cache-key paths additionally run through
``os.path.normpath`` to collapse ``..`` segments, redundant separators,
and ``./`` prefixes. Two conf-driven include paths that resolve to the
same directory but are spelled differently
(``${CONF_DIR}/../shared/src/include`` from a sibling conf vs.
``${CONF_DIR}/shared/src/include`` from the parent) therefore hash to
the same cache key instead of forking ``macro_state_hash`` on
semantically-equivalent ``-I`` tokens. Normalization is purely
lexical (no symlink resolution) so cross-worktree CAS sharing through
symlinked module directories continues to work. The emitted compile
command preserves the original lexical form -- only the hash input is
normalised -- so lexical ``..`` collapse never changes what gcc
resolves through symlinked intermediates.

Linker-artefact caching (cas-exedir)
------------------------------------

ct-cake also caches the *output* of the link step at
``{git_root}/cas-exedir/{variant}/`` (or a custom path via
``--cas-exedir``). The same content-addressable layout houses
all three linker-artefact kinds under a single directory:

* ``<cas-exedir>/<linkkey[:2]>/<exename>_<linkkey>.exe`` for executables
* ``<cas-exedir>/<libkey[:2]>/lib<name>_<libkey>.a`` for static libraries
* ``<cas-exedir>/<libkey[:2]>/lib<name>_<libkey>.so`` for shared libraries

The producer rule (link / ar / link-shared) writes directly to its
CAS path; a downstream ``symlink`` rule then publishes the user-facing
``bin/<variant>/<name>`` (or ``bin/<variant>/lib<name>.{a,so}``) as a
hard link to the cached artefact, with a symlink fallback for
cross-filesystem cases. The hash inputs are the linker identity,
canonicalized LDFLAGS, sorted gitroot-canonical object paths, and
(for executables) the bindir basename — a defensive guard against
RPATH/$ORIGIN linker scripts whose embedded paths could otherwise
silently miscache across bindir choices. The ``ar`` key is simpler:
just the ``ar`` argv prefix and the canonical object set.

The ``--use-mtime`` flag controls whether classical mtime semantics
apply on top of the CAS layer. The default ``--no-use-mtime``
(equivalently ``--use-mtime=False``) is the recommended mode for
shared CI caches: compile, link, ar, and link-shared rules drop their
sources/objects from prerequisites entirely (PCH/BMI artefacts stay as
order-only deps so build ordering is preserved). Existence of the CAS
artefact on disk is the sole rebuild signal, which means a fresh
``git checkout`` (where every source has ``mtime = now``) hits the
cache instead of re-running the producer. ``--use-mtime`` restores
the legacy mtime-based behavior for interactive workflows where
re-touching a source should force a rebuild even when the producer
key would not change.

``--use-mtime`` is honored only by the Make and Ninja backends:
their rule emitters consume the prereq list as a literal mtime
comparison, so they can branch on the flag. The cmake / bazel /
shake / slurm backends use their own change detection
(cmake's out-of-source incremental tracking, bazel's
content-addressable action cache, trace_backend's verifying
traces) and a touched-but-otherwise-unchanged source is invisible
to all of them — they cannot deliver "touch to force rebuild"
semantics regardless of how the flag is set. Passing
``--use-mtime=True`` against one of those backends emits a
stderr warning and is otherwise ignored.

Caveats of ``--use-mtime=False`` mode
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Because the CAS-mode rebuild signal is "does the cached artefact
exist", a few classical Make/Ninja workflows behave differently:

* **Generated headers must exist before headerdeps runs.** Headers
  that are produced by an earlier build step but absent at headerdep
  analysis time get treated as unresolved (``_find_include`` returns
  ``None``) and are NOT included in the ``dep_hash``. Their first
  appearance does change the dep_list and force a recompile (the
  second build picks up the new include resolution). But pipelines
  where the same header is regenerated with different content between
  ct-cake invocations should ensure the header exists at headerdeps
  time (e.g., generate it as a separate ``Makefile`` step that runs
  before ``ct-cake``).

* **Do not use ``make -t`` against a CAS-mode build.** GNU make's
  touch flag creates an empty file at the target path when the
  recipe has no real prerequisites (which CAS-mode rules don't —
  inputs are lifted to order-only). This corrupts cached binaries.
  ``ninja -t restat`` is similarly inappropriate. If you genuinely
  need to mark a build as up-to-date without running it, prefer
  ``--use-mtime=True`` or run ``ct-cake`` directly.

* **System / ``/usr/include`` headers are not in the cache key.**
  A glibc upgrade between CI runs will silently reuse cached objects
  built against the prior glibc. This matches the ccache / sccache
  contract but surprises users coming from full-rebuild CI. If you
  need glibc-version sensitivity in the cache key, fold it into the
  ``compiler_identity`` triple (in practice, an in-place compiler
  swap usually changes ``compiler_identity`` and invalidates the
  cache implicitly).

* **Linker-time environment variables ARE in the link key.**
  ``SOURCE_DATE_EPOCH``, ``LD_LIBRARY_PATH``, ``LIBRARY_PATH``, and
  ``LD_PRELOAD`` participate in the cas-exe payload so two CI runs
  with different values do not share a cached binary that bakes the
  wrong build-id or resolves -lfoo to the wrong libfoo.so.

Migrating from ``--use-mtime=True`` (legacy) to ``--use-mtime=False``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The default flipped to ``--use-mtime=False`` in this release. Pre-existing
scripts that rely on "touch source.cpp; make" producing a rebuild now
see a no-op (the source content didn't change → CAS key is unchanged
→ cached object reused). To audit, run ``ct-trim-cache --dry-run`` and
verify the kept-vs-removed split matches expectations. To restore the
legacy behavior, pass ``--use-mtime=True`` or set ``use_mtime=True``
in the relevant ``ct.conf``.

Cache trimming for cas-exedir uses the shared ``ct-trim-cache`` tool:

* Default: trim all four caches (objdir, pchdir, pcmdir, exedir).
* Selective: ``ct-trim-cache --cas-exedir-only`` for the
  linker-artefact cache only.
* Hard-link safety: any cas entry with ``st_nlink > 1`` is preserved
  on the assumption that a published ``bin/<variant>/<name>`` (or
  ``bin/<variant>/lib<name>.{a,so}``) is still pointing at it.
  Symlinked-fallback bin paths show ``st_nlink == 1`` on the cas
  entry and are NOT protected.
* Lock-aware delete: trim acquires the same ``<path>.lock`` sidecar
  the producer rule uses, so a trim that lands mid-link blocks until
  the link releases the lock instead of clobbering an in-flight
  rename.

Selective build and test
========================

You can instruct ct-cake to only build binaries dependant on a list of
source files using the ``--build-only-changed`` flag. This is helpful for
limiting building and testing in a Continuous Integration pipeline to only
source that has changed from master.

``changed_source=git diff --name-only master | sed "s,^,$(git rev-parse --show-toplevel)/,"
ct-cake --build-only-changed \"$changed_source\"``

Configuration
=============

The compiletools programs require *almost* no configuration. However, it is
still useful to have some shortcut build templates such as 'release',
'profile' etc.

Config files for the ct-* applications are programmatically located using
python-appdirs, which on linux is a wrapper around the XDG specification.  Thus
default locations are /etc/xdg/ct/ and $HOME/.config/ct/.  Configuration parsing
is done using python-configargparse which automatically handles environment
variables, command line arguments, system configs
and user configs.

Specifically, the config files are searched for in the following locations (from
lowest to highest priority):

    * ct/ct.conf.d subdirectory alongside the ct-* executable
    * system config (XDG compliant, so usually /etc/xdg/ct)
    * python virtual environment configs (${python-site-packages}/ct/ct.conf.d)
    * package bundled config (<installed-package>/ct.conf.d)
    * user config (XDG compliant, so usually ~/.config/ct)
    * project config (<gitroot>/ct.conf.d)
    * gitroot directory
    * current working directory
    * environment variables
    * command line arguments

The ct-* applications are aware of two levels of configs.  There is a base level
ct.conf that contains the basic variables that apply no  matter what variant
(i.e, debug/release/etc) is being built.

The second layer of config files are *axis* configs: one per orthogonal
concern (toolchain, optimization, instrumentation). A variant string composes
these axes — ``--variant=gcc,debug,asan`` resolves to ``gcc.conf`` +
``debug.conf`` + ``asan.conf``. Comma, dot, and whitespace are interchangeable
separators (``gcc.debug.asan``, ``gcc debug asan`` are equivalent). The
canonical dotted form is what appears in ``cas-objdir/<variant>/``,
``compile_commands.<variant>.json``, ``bin/<variant>/``, etc.

The ``blank.conf`` file is intentionally empty, inheriting all settings from the
environment or parent configs. ``--variant=blank`` lets environment variables
(CC, CXX, CFLAGS, …) control the build entirely.

To pull additional axes into a custom variant, write a small conf file with
an ``extends = ...`` directive (parents apply first, child's flags layer on
top). To override a specific synthesized composition, drop a literal conf
file with the canonical name (e.g. ``gcc.debug.asan.conf``) anywhere in the
hierarchy — it takes precedence. See ``README.ct-config.rst`` for the full
upgrade guide from the pre-inheritance ``variantaliases`` mechanism.

If any config value is specified in more than one way then the following
hierarchy is used

* command line > environment variables > config file values > defaults

If you need to append values rather than replace values, this can be
done (currently only for environment variables) by specifying
--variable-handling-method append
or equivalently add an environment variable
VARIABLE_HANDLING_METHOD=append

The example /etc/xdg/ct/gcc.release.conf file looks as follows:

.. code-block:: ini

    ID=GNU
    CC=gcc
    CXX=g++
    LD=g++
    CFLAGS=-fPIC -g -Wall -O3 -DNDEBUG -finline-functions -Wno-inline
    CXXFLAGS=-std=c++11 -fPIC -g -Wall -O3 -DNDEBUG -finline-functions -Wno-inline
    LDFLAGS=-fPIC -Wall -Werror -Xlinker --build-id
    TESTPREFIX=timeout 300 valgrind --quiet --error-exitcode=1

CXXFLAGS lists the flags appended to each compilation job. The value in
/etc/xdg/ct/\*.conf
is overridden by the environment variable, which is in return overridden by
the command-line argument --CXXFLAGS=. Likewise, LDFLAGS sets the default
options used for linking.

TESTPREFIX specifies a command prefix to place in front of unit test runs. This
should ideally be a tool like valgrind, gdb or purify that can be configured
to execute the app and return a non-zero exit code on any failure.


Build variants
==============
A variant selects the compiler, optimization level, sanitizers, linker, and
other build-axis settings. Variants are *composed* from one ``.conf`` file per
orthogonal axis (toolchain / linker / optimization / instrumentation / …); you
do not write a conf file per combination. The canonical token order is
ground-truth and ensures two typings of the same set share caches::

    $ ct-cake --variant=release a.cpp                 # one axis
    $ ct-cake --variant=gcc,debug,asan a.cpp          # toolchain + opt + sanitizer
    $ ct-cake --variant=clang,mold,cxx20,release,lto  # everything composes
    $ ct-cake --variant=dev a.cpp                     # opinionated bundle

Comma, dot, and whitespace separators are equivalent. Composed variants are
synthesized at resolution time from the matching axis ``.conf`` files, then
optionally tuned by a literal ``<canonical_name>.conf`` (which layers on top
of the synthesized atoms) or an ``extends = ...`` directive (which replaces
the atom set entirely). Bundled opinionated bundles include ``dev``, ``ci``,
``production``, ``safety``, ``perf``, ``secure``.

See ct-config(1) for the full axis catalogue, ``extends`` semantics, the
canonical-token override hierarchy, and the migration path from the retired
``variantaliases =`` dict syntax. Run ``ct-list-variants`` to enumerate the
axes and bundles available in your installation.

Unit Tests
==========

ct-cake integrates with unit tests in a fairly simple (and perhaps simplistic)
way.

By default, ``ct-cake`` automatically builds all executables and unit tests,
then runs the unit tests. This automatic behavior happens when you run
``ct-cake`` without arguments (equivalent to ``ct-cake --auto``). To disable
automatic test discovery and execution, use ``--no-auto``.

If you would prefer to be explicity, ct-cake allows you to specify multiple
build targets on each line, so the following is valid and useful:

    ``$ ct-cake utilities/*.cpp  # builds specified apps into bin/``

To explicitly specify build targets and unit tests to be generated and run
use the following example.  Unit tests are built and when run must return
an exit code of 0 otherwise this will become a build failure. The flag used
to specify that executables are unit tests is --tests.

    ``$ ct-cake utilities/*.cpp --tests tests/*.cpp``

If the *TESTPREFIX* variable is set, you can automatically check
all unit tests with a code purifying tool. For example:

    ``export TESTPREFIX="valgrind --quiet --error-exitcode=1"``

will cause all unit tests to only pass if they run through valgrind with no
memory errors.

Test execution scheduling (``--serialise-tests``)
-------------------------------------------------

Test execution is part of the build graph, not a phase that runs after the
build finishes. By default (``--no-serialise-tests``), each test runs
**as soon as its own executable is linked**, even while other translation
units are still compiling and other tests are running concurrently. This
is the fastest mode and is what you want for normal development and CI:
``-j`` scheduling fully overlaps compile, link, and test execution.

Use ``--serialise-tests`` to force test executables to run **one at a
time**, in source-sorted order. Compilation and linking still parallelise
under ``-j``; only the test-execution edges are serialised. Reach for it
when:

* tests contend on a shared resource that isn't isolated per process
  (a single test database, a fixed TCP port, a GPU, a file-locked fixture),
* you need deterministic ordering to reproduce a flaky failure in CI, or
* you're working around a known test-isolation bug while the real fix is
  in flight.

The flag is **British-spelled only** — ``--serialise-tests`` /
``--no-serialise-tests`` (no ``--serialize-tests`` alias).

Per-backend mechanism (all behave identically from the user's point of
view; documented here in case you read generated build files):

* **make / ninja / shake / slurm** — the build graph chains each test
  rule on the previous one's output. Under ``--no-use-mtime`` (the CAS
  default) the chain edge goes in ``order_only_deps`` so re-running a
  passed test doesn't cascade re-runs; under ``--use-mtime`` it goes in
  ``inputs`` so marker mtimes gate the next test.
* **bazel** — passes ``--local_test_jobs=1`` to ``bazel test``.
  Compilation still runs at full ``--jobs``.
* **cmake** — chains the test custom-command ``DEPENDS`` so cmake
  serialises execution; the test executables themselves still link in
  parallel.

Common Options
==============

**--auto / --no-auto**
    Enable or disable automatic target detection. ``--auto`` is the default.
    When enabled, ct-cake searches for source files containing exemarkers
    (like ``main(``) and testmarkers (like ``unit_test.hpp``).

**--disable-tests**
    When ``--auto`` is specified, skip automatic building and running of tests.
    Useful when you only want to build executables.

**--disable-exes**
    When ``--auto`` is specified, skip automatic building of executables.
    Useful when you only want to build and run tests.

**--serialise-tests / --no-serialise-tests**
    Force unit tests to run one at a time in source-sorted order. Default
    is off — each test runs as soon as its executable is linked, in parallel
    with other compiles, links, and tests. Enable when tests contend on a
    shared resource (database, port, GPU, file-locked fixture) or to
    reproduce a flaky failure deterministically. British spelling only.
    See *Test execution scheduling* above.

**-o, --output**
    When building a single target, rename the output to this name.
    Example: ``ct-cake main.cpp -o myapp``

**--backend**
    Build system backend to use. Choices: ``make`` (default), ``ninja``,
    ``cmake``, ``bazel``, ``shake``.
    Example: ``ct-cake --backend=ninja``

**--clean**
    Remove all build artifacts.

**--realclean, --real-clean**
    Remove bin/ entirely and selectively clean this build's objects from
    the object CAS. Superset of ``--clean`` -- also removes copied
    executables from the top-level output directory. Unlike ``--clean``,
    only removes object files that belong to the current build, preserving
    other sub-projects' objects in the object CAS.

**-j, --parallel**
    Number of parallel jobs. Defaults to the output of ``ct-jobs``
    (typically the number of CPU cores). Passed to the selected backend.

**--compilation-database / --no-compilation-database**
    Generate a ``compile_commands.json`` file for clang tooling. Enabled by
    default. The file is placed in the git root directory.

**--compilation-database-output**
    Custom output path for the compilation database.

**--timing**
    Collect and report build timing information.  Writes ``timing.json``
    into the per-invocation diagnostics directory (see
    ``--diagnostics-dir``) and prints a summary table after the build.
    Analyze the results with ``ct-timing-report``.

**--diagnostics-dir PATH**
    Parent directory for per-invocation diagnostic artifacts -- the
    ``--timing`` JSON, slurm job logs from the ``slurm`` backend, and
    future per-build reports.  Each ct-cake invocation gets its own
    ``<invocation-id>`` subdirectory under this path so concurrent peers
    sharing a bindir or objdir never collide.  Default:
    ``<bindir>/diagnostics/<invocation-id>/``.  Also settable via the
    ``DIAGNOSTICS_DIR`` environment variable or
    ``diagnostics-dir = <path>`` in any ``ct.conf`` file.  Must NOT be
    set to ``--cas-objdir``, which is a content-addressable cache:
    diagnostic files have no eviction path there and races with peer
    ct-cake invocations clobber the data.
    Example: ``ct-cake --diagnostics-dir=/scratch/ct-diag``

**--cas-objdir PATH**
    Use an object CAS for compiled object files across multiple builds.
    Enables content-addressable object file caching and cross-user
    build sharing. Default: ``{git_root}/cas-objdir``. Requires
    ``file-locking = true`` in ``ct.conf.d/ct.conf`` for safe concurrent access.
    Example: ``ct-cake --cas-objdir=/shared/build/objects``

**--cas-pchdir PATH**
    Use a precompiled header (PCH) CAS. Headers marked with
    the ``//#PCH=`` magic flag are compiled into ``.gch`` files and cached here.
    The cache key includes compiler, flags, and header path, enabling safe reuse
    across builds and developers. Default: ``{git_root}/cas-pchdir/{variant}``.
    Without this flag, PCH files fall back to legacy ``.gch`` placement in the
    object directory. Use ``ct-trim-cache --cas-pchdir-only`` to clean aged entries.
    Example: ``ct-cake --cas-pchdir=/shared/build/pch``

**--cas-pcmdir PATH**
    Use a content-addressable cache for C++20 module BMI artefacts
    (clang ``.pcm``, gcc ``.gcm``). Auto-populated when ct-cake detects
    ``import`` / ``export module`` in your sources. The cache key
    bundles compiler identity, hash-relevant flags, source content,
    and transitive header content into one ``command_hash``. Safety
    against the rare hash-collision case is provided by the
    compiler's BMI verification at consume time -- a collision would
    cause a slow re-precompile, never a miscompile (see the "C++20
    Modules Caching" section above for the full design rationale).
    Default: ``{git_root}/cas-pcmdir/{variant}``. Use
    ``ct-trim-cache --cas-pcmdir-only`` to clean aged entries.
    Example: ``ct-cake --cas-pcmdir=/shared/build/pcm``

**--prepend-PKG-CONFIG-PATH PATH**
    Prepend PATH to ``PKG_CONFIG_PATH`` before any pkg-config invocation.
    Takes highest priority — overrides both ``ct.conf.d/pkgconfig/`` directory
    layers and the existing environment variable. Useful for CI pipelines or
    one-off debugging where you need a specific ``.pc`` file to take precedence.
    May be repeated to prepend multiple directories.
    Example: ``ct-cake --prepend-PKG-CONFIG-PATH=/opt/custom/pkgconfig``

**--append-PKG-CONFIG-PATH PATH**
    Append PATH to ``PKG_CONFIG_PATH`` after any pkg-config invocation.
    Takes lowest priority — only consulted when the package is not found via
    any other mechanism. Useful for fallback paths or system-wide package
    locations as a last resort. May be repeated to append multiple directories.
    Example: ``ct-cake --append-PKG-CONFIG-PATH=/usr/local/lib/pkgconfig``

**--static / --dynamic**
    Build a static or dynamic library instead of an executable.
    Example: ``ct-cake --static mylib.cpp``

Putting it all together - a typical build setup
===============================================

For most simple projects, a build.sh script that looks like the
following is quite useful. You can simply add more cpp to the apps directory to
generate more tools from the project,
or add test scripts to the regression directory to improve
test coverage.

Code generation steps can be added at the beginning of
the build.sh, before cake runs.

.. code-block:: bash

    #!/bin/sh
    set -e
    python fancypythoncodegenerator.py
    ct-cake "$@"


The special *"$@"* marker is the recommended way
of forwarding arguments to an application. You can then
run the build script like this:

    ``$ ./build.sh --variant=release``

or:

    ``$ ./build.sh --variant=release --append-CXXFLAGS=-DSPECIALMODE``

JUnit XML Output
================

Pass ``--test-xml-dir=DIR`` to emit per-test JUnit XML reports for
GitHub Actions and other CI systems::

    ct-cake --auto --variant=gcc.debug --test-xml-dir=test-results

Each test executable produces ``test-results/<variant>/<exe>.xml``.
ct-cake automatically picks the right framework flag based on the
headers each test transitively includes:

==========  ===================================================
Framework   XML argv appended after exe_path
==========  ===================================================
gtest       ``--gtest_output=xml:PATH``
doctest     ``--reporters=junit --out=PATH``
Catch2      ``--reporter junit --out PATH``
==========  ===================================================

Detection trips on whether ``gtest/gtest.h``, ``doctest/doctest.h``
(or bare ``doctest.h``), or ``catch2/catch_all.hpp`` /
``catch2/catch.hpp`` / ``catch.hpp`` appears in each test's
transitive header set. A test that pulls in two framework headers
at once is rejected with an error naming both — disambiguate by
fixing the include paths. A test that matches none runs normally
and produces no XML; a warning is emitted at ``--verbose=1``.

The XML argv is appended *after* the exe_path so prefix tools like
``valgrind --quiet`` (passed via ``--TESTPREFIX``) forward the XML
flag to the child process correctly.

A test whose ``.result`` marker is current but whose XML file has
been deleted (someone ``rm -rf``'d the output dir, or asked for a
different one) is re-run to regenerate the XML. ct-cake does NOT
clean ``DIR/<variant>/`` before running, so stale XML from a removed
test will linger; run ``rm -rf test-results/<variant>/`` for a clean
slate. Most CI systems publish from a fresh checkout, so staleness
doesn't accumulate there in practice.

GitHub Actions usage::

    - run: ct-cake --auto --variant=gcc.debug --test-xml-dir=test-results
    - uses: actions/upload-artifact@v4
      if: always()
      with:
        name: junit
        path: test-results/**/*.xml
    - uses: EnricoMi/publish-unit-test-result-action@v2
      if: always()
      with:
        files: test-results/**/*.xml

References
==========

The content-addressable backend architecture was informed by:

* Andrey Mokhov, Neil Mitchell, Simon Peyton Jones. *Build Systems à la Carte*.
  Proc. ACM Program. Lang., Vol. 2, ICFP, Article 79, September 2018.
  https://doi.org/10.1145/3236774

The non-recursive Makefile generation was informed by:

* Peter Miller. *Recursive Make Considered Harmful*. 2008.
  https://api.semanticscholar.org/CorpusID:54117644

SEE ALSO
========
``compiletools`` (1), ``ct-timing-report`` (1), ``ct-list-variants`` (1), ``ct-config`` (1), ``ct-trim-cache`` (1), ``ct-cas-publish`` (1)
