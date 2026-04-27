import subprocess
import sys

import compiletools.apptools
import compiletools.utils


class PreProcessor:
    """Make it easy to call the C Pre Processor"""

    def __init__(self, args):
        self.args = args

    @staticmethod
    def add_arguments(cap):
        compiletools.apptools.add_common_arguments(cap)

    def process(self, realpath, extraargs, redirect_stderr_to_stdout=False):
        cmd = self.args.CPP.split() + self.args.CPPFLAGS.split() + extraargs.split()
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
