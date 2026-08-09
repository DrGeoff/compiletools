import subprocess
import sys

import compiletools.apptools
import compiletools.build_apply
import compiletools.utils


class PreProcessor:
    """Make it easy to call the C Pre Processor"""

    def __init__(self, args):
        self.args = args

    @staticmethod
    def add_arguments(cap):
        compiletools.apptools.add_common_arguments(cap)

    def process(self, realpath, extraargs, redirect_stderr_to_stdout=False):
        # args.CPP is an exe-name string (outside the BuildState); the cpp
        # flag tokens come from the stashed state. Never .split() a raw
        # string that may be shlex.join'd -- quoted tokens would become
        # literal-quote argv garbage -- so the two raw strings go through
        # shlex splitting.
        state = compiletools.build_apply.get_build_state(self.args)
        try:
            cmd = (
                compiletools.utils.split_compiler_command(self.args.CPP, slot="CPP")
                + list(state.flags.cpp)
                + compiletools.utils.split_command_cached(extraargs)
            )
        except compiletools.utils.FlagTokenizeError as exc:
            # This is the single choke point every --headerdeps=cpp /
            # --magic=cpp caller (headerdeps.py, magicflags.py x2) reaches,
            # so converting here covers all three call sites at once, the
            # same way magicflags._process_magic_flag's carve-outs convert
            # once for every //# magic key. Unconditional SystemExit(1),
            # not gated on verbosity: this runs inside Hunter's source-
            # expansion walk, which has a deliberately broad
            # ``except Exception`` (hunter.py) that would otherwise catch
            # this RuntimeError subclass and downgrade a malformed --CPP
            # to a per-source warning instead of stopping the build --
            # and it also means standalone callers with no exception
            # handling of their own (ct-headertree, ct-magicflags,
            # ct-filelist mains) get a clean, traceback-free message
            # instead of a raw exception escaping to the top level.
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from None
        if compiletools.utils.is_header(realpath):
            # Use /dev/null as the dummy source file.
            cmd.extend(["-include", realpath, "-x", "c++", "/dev/null"])
        else:
            cmd.append(realpath)

        if self.args.verbose >= 3:
            print(" ".join(cmd))

        try:
            output = subprocess.check_output(
                cmd,
                text=True,
                stderr=subprocess.STDOUT if redirect_stderr_to_stdout else None,
            )
            if self.args.verbose >= 5:
                print(output)
        except OSError as err:
            print(
                f"Failed to preprocess {realpath}. Error={err}",
                file=sys.stderr,
            )
            raise
        except subprocess.CalledProcessError as err:
            print(
                f"Preprocessing failed for {realpath}. Return code={err.returncode}, Output={err.output}",
                file=sys.stderr,
            )
            raise

        return output
