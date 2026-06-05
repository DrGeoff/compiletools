=========================
ct-compilation-database
=========================

--------------------------------------------------------------------------------
Generate compile_commands.json for clang tooling and IDE integration
--------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 10.1.7
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-compilation-database [-h] [-c CONFIG_FILE] [--variant VARIANT] [-v] [-q]
                        [--version] [--compilation-database-output OUTPUT]
                        [--relative-paths] [--file-locking]
                        [filename ...]

DESCRIPTION
===========
ct-compilation-database generates a compilation database (compile_commands.json)
for C/C++ projects. This JSON file contains the exact compiler commands used to
build each source file, enabling integration with modern development tools.

The compilation database format is used by:

* **Language servers**: clangd, ccls for IDE features (autocomplete, go-to-definition)
* **Static analyzers**: clang-tidy, clang-format, cppcheck
* **Code indexers**: rtags, ycmd
* **Refactoring tools**: clang-rename, clang-include-fixer

ct-compilation-database is automatically invoked by ct-cake when building projects.
You can also run it standalone to regenerate the compilation database without
rebuilding.

The tool uses the same dependency analysis as ct-cake to ensure the compilation
database reflects the actual build configuration, including:

* Automatic detection of source files
* Magic flags from source comments
* Variant-specific compiler settings
* Include paths and preprocessor definitions

OUTPUT FORMAT
=============
The generated compile_commands.json follows the JSON Compilation Database
specification. Each entry contains:

.. code-block:: json

    {
      "directory": "/path/to/project",
      "command": "g++ -std=c++11 -Wall -c main.cpp",
      "file": "main.cpp"
    }

PER-VARIANT DATABASES AND THE compile_commands.json SYMLINK
============================================================
By default ct-compilation-database writes one database per variant at
``<gitroot>/compile_commands.<variant>.json`` (e.g.
``compile_commands.gcc.debug.json``, ``compile_commands.clang.release.json``)
and atomically retargets a sibling ``<gitroot>/compile_commands.json``
symlink at whichever variant ran most recently. The bare
``compile_commands.json`` is what clangd, clang-tidy, and VSCode actually
open — the JSON Compilation Database spec allows multiple entries per
source file, but consumers (clangd in particular) pick one and ignore the
rest, so multi-variant DBs must live in separate files plus a switcher.

The symlink target is written as a relative basename so the tree is
portable across renames and copies.

To switch the active variant for tooling, run a build (or a
ct-compilation-database invocation) with ``--variant=<other>`` (or
``VARIANT=<other>``); the symlink follows the most recent run.

If you pass ``--compilation-database-output=<path>``, the literal path is
honored verbatim and **no symlink is touched** — preserves backward
compatibility for scripts that pin a specific filename.

OPTIONS
=======
--compilation-database-output OUTPUT
    Output filename for compilation database. Honored verbatim; setting
    this also disables the ``compile_commands.json`` symlink update so
    scripts that pin a specific path aren't surprised by a sibling rewrite.
    Default (when unset): ``<gitroot>/compile_commands.<variant>.json``
    plus a ``<gitroot>/compile_commands.json`` symlink pointing at it.

--relative-paths
    Use relative paths instead of absolute paths in the database.
    Useful for portable compilation databases.

--file-locking / --no-file-locking
    Enable file locking for concurrent compilation database writes.
    Useful in multi-user environments with shared build caches.
    Default: disabled.

EXAMPLES
========

Generate compilation database for current project::

    ct-compilation-database

Generate with specific output location::

    ct-compilation-database --compilation-database-output build/compile_commands.json

Generate with relative paths for portability::

    ct-compilation-database --relative-paths

Generate for specific source files (disables auto-detection)::

    ct-compilation-database --no-auto src/main.cpp src/utils.cpp

INTEGRATION WITH ct-cake
=========================
ct-cake automatically generates compile_commands.json by default. To control this
behavior, use these flags:

--compilation-database / --no-compilation-database
    Enable or disable automatic generation (default: enabled)

--compilation-database-output OUTPUT
    Customize output location

--compilation-database-relative-paths
    Use relative paths

Example ct-cake usage::

    ct-cake --no-compilation-database    # Disable generation
    ct-cake --compilation-database-output .compile_db.json


VSCODE INTEGRATION
==================
To enable IntelliSense in VSCode using the generated compilation database,
create or update ``.vscode/c_cpp_properties.json``:

.. code-block:: json

    {
        "configurations": [
            {
                "name": "Linux",
                "compileCommands": "${workspaceFolder}/compile_commands.json",
                "compilerPath": "/path/to/bin/g++"
            }
        ],
        "version": 4
    }

Replace ``Linux`` with your platform (``Mac`` for macOS, ``Win32`` for Windows)
and ``/path/to/bin/g++`` with your actual compiler path (e.g.,
``/usr/bin/g++`` or ``/usr/bin/clang++``).

SEE ALSO
========
``compiletools`` (1), ``ct-cake`` (1), ``ct-config`` (1)

REFERENCES
==========
* JSON Compilation Database: https://clang.llvm.org/docs/JSONCompilationDatabase.html
* clangd: https://clangd.llvm.org/
