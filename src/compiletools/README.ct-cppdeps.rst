================
ct-cppdeps
================

------------------------------------------------------------
What 
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2022-06-05
:Copyright: Copyright (C) Geoffery Ericksson
:Version: 6.1.3
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cppdeps [-h] [-c CONFIG_FILE] [--variant VARIANT] [-v] [-q] 
[--version] [-?] [--man] 
[--CTCACHE CTCACHE] [--ID ID] 
[--CPP CPP] [--CC CC] [--CXX CXX] 
[--CPPFLAGS CPPFLAGS] [--CXXFLAGS CXXFLAGS] 
[--CFLAGS CFLAGS] 
[--git-root | --no-git-root] 
[--include INCLUDE] [--pkg-config PKG_CONFIG] 
[--shorten | --no-shorten] 
[--headerdeps {direct,cpp}]
filename [filename ...]

DESCRIPTION
===========
ct-cppdeps generates the header dependencies for the file you specify at the 
command line.  There are two possible methodologys:  

* "--headerdeps=direct" uses a regex to find the "#include" lines in the source
  code. This methodology is fast and is supposed to generate the same results as 
  using the compiler to preprocess the file.  However, its own simple 
  preprocessor does not handle all the complexities of a real C/C++
  preprocessor and so it is possible to generate incorrect results.

* "--headerdeps=cpp" executes "$CPP -MM -MF" which is slower but guarantees correctness.  


For each header file found in the source file, it looks for
an underlying implementation (c,cpp,cc,cxx,etc) file with the same name, and
adds that implementation file to the build.
ct-cppdeps also reads the entire file (configurable via --max-file-read-size)
for special comments that indicate needed link and compile flags.  Then it
recurses through the dependencies of the cpp file, and uses this spidering to
generate complete dependency information for the application. 

EXAMPLES
========

ct-cppdeps somefile1.cpp somefile2.cpp

ct-cppdeps --variant=release somefile.cpp


SEE ALSO
========
``compiletools`` (1), ``ct-findtargets`` (1), ``ct-headertree`` (1), ``ct-config`` (1), ``ct-cake`` (1)


