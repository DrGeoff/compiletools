"""Use bumpversion to increment version.py, and README.rst, and ...,
simultaneously.  Also add the flags to git tag and commit everything
in one operation.
"""

import functools
import os
import subprocess

__version__ = "8.0.1"


@functools.cache
def get_package_git_sha():
    """Return the short git SHA of the compiletools source tree, or None."""
    package_dir = os.path.dirname(__file__)
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=package_dir,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
