# vim: set filetype=python:
"""Makefile generation — delegates to MakefileBackend.

This module previously contained the MakefileCreator class which has been
replaced by MakefileBackend in makefile_backend.py. The main() function
is kept as a convenience entry point.
"""

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.namer
from compiletools.makefile_backend import MakefileBackend


def main(argv=None):
    """Generate a Makefile for the given source files.

    Delegates to MakefileBackend for all generation logic.
    """
    cap = compiletools.apptools.create_parser(
        "Create a Makefile that will compile the given source file into an executable (or library)", argv=argv
    )
    # General arguments
    compiletools.apptools.add_target_arguments_ex(cap)
    compiletools.apptools.add_link_arguments(cap)
    compiletools.namer.Namer.add_arguments(cap)
    compiletools.hunter.add_arguments(cap)

    # Make-specific arguments
    MakefileBackend.add_arguments(cap)

    args = compiletools.apptools.parseargs(cap, argv)

    try:
        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        backend = MakefileBackend(args=args, hunter=hunter)
        graph = backend.build_graph()
        backend.generate(graph)

    except OSError as ioe:
        if args.verbose < 2:
            print(f"Error processing {ioe.filename}: {ioe.strerror}")
            return 1
        else:
            raise
    except Exception as err:
        if args.verbose < 2:
            print(err)
            return 1
        else:
            raise
    return 0
