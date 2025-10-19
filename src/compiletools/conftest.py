"""Shared pytest configuration for compiletools tests.

This conftest.py provides session-wide fixtures that are automatically
applied to all tests in src/compiletools/ and subdirectories.
"""

import os
import sys
import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_lock_helpers_in_path():
    """Ensure ct-lock-helper and ct-lock-helper-py are available in PATH.

    This fixture runs once per test session and is automatically applied to
    all tests (autouse=True). It's required for tests that use the
    --shared-objects flag, which needs ct-lock-helper for file locking.

    The fixture:
    1. Checks if ct-lock-helper and ct-lock-helper-py are in PATH
    2. If not, tries to locate them in the repository root
    3. Adds repo root to PATH if helpers are found there
    4. Does not skip tests if helpers are missing - individual tests will
       fail with clear error messages from makefile.py or other code

    This centralized approach prevents:
    - Tests failing mysteriously based on developer environment
    - Duplicate fixture code in multiple test files
    - Forgetting to add fixture when writing new tests with --shared-objects
    """
    import shutil

    # Find repository root (conftest.py is at src/compiletools/conftest.py)
    conftest_dir = os.path.dirname(__file__)
    repo_root = os.path.dirname(os.path.dirname(conftest_dir))

    helpers_to_check = ['ct-lock-helper', 'ct-lock-helper-py']
    helpers_found = []

    # Check each helper and add scripts/ directory to PATH if any are found there
    scripts_dir = os.path.join(repo_root, 'scripts')
    path_modified = False
    for helper in helpers_to_check:
        if not shutil.which(helper):
            helper_path = os.path.join(scripts_dir, helper)
            if os.path.exists(helper_path):
                helpers_found.append(helper)
                if not path_modified:
                    # Only modify PATH once, even if multiple helpers found
                    os.environ['PATH'] = scripts_dir + os.pathsep + os.environ.get('PATH', '')
                    path_modified = True

    if helpers_found:
        print(f"\nAdded {scripts_dir} to PATH for: {', '.join(helpers_found)}", file=sys.stderr)
