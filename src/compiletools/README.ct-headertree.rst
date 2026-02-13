=============
ct-headertree
=============

---------------------------------------------------------------------
Create a tree of header dependencies starting at the given C/C++ file
---------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2018-07-26
:Copyright: Copyright (C) 2011-2018 Zomojo Pty Ltd
:Version: 7.0.2
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-headertree [OPTION] filename [filename ...]

DESCRIPTION
===========
Create a tree of header dependencies starting at a given C/C++ file.

OPTIONS
=======
  --style {tree,depth,dot,flat}
                        Output formatting style [env var: STYLE] (default:
                        tree)

  --variant VARIANT     Specifies which variant of the config should be used.
                        Use the config name without the .conf [env var:
                        VARIANT] (default: blank)

  --git-root            Determine the git root then add it to the include
                        paths. Use --no-git-root to turn the feature off. [env
                        var: GIT_ROOT] (default: True)

  --no-git-root         [env var: NO_GIT_ROOT] (default: False)

  --include [INCLUDE [INCLUDE ...]]
                        Extra path(s) to add to the list of include paths [env
                        var: INCLUDE] (default: [])

  --shorten             Strip the git root from the filenames. Use --no-shorten
                        to turn the feature off. [env var: SHORTEN] (default:
                        False)

  --no-shorten          [env var: NO_SHORTEN] (default: False)

  --headerdeps {direct,cpp}
                        Methodology for determining header dependencies [env
                        var: HEADERDEPS] (default: direct)

OUTPUT FORMATS
==============

**tree** (default)
    Shows the header tree with statistics. Each line has 4 numeric columns:

    1. Cumulative count - total headers recursively included by this file
    2. Self count - number of headers directly included by this file
    3. Duplicate count - how many times this file appears in the tree
    4. Parent count - number of unique files that include this file

    Example::

        3 2 1 0 calculator.cpp
        0 0 2 2 ├─add.H
        1 1 1 1 └─calculator.h
        0 0 2 2   └─add.H

**flat**
    Simple newline-delimited list of all headers.

**depth**
    Indented list with ``--`` indicators showing depth.

**dot**
    Graphviz digraph format for visualization.

EXAMPLES
========

ct-headertree myfile.cpp

ct-headertree --style=dot myheader.hpp

ct-headertree --style=flat myfile.cpp


SEE ALSO
========
``compiletools`` (1), ``ct-commandline`` (1), ``ct-config`` (1)
