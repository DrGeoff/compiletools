import pytest

import compiletools.testhelper as uth


def test_skip_if_bazel_env_error_skips_on_certificate():
    with pytest.raises(pytest.skip.Exception):
        uth.skip_if_bazel_env_error("ERROR: TLS Certificate verification failed")


def test_skip_if_bazel_env_error_skips_on_operation_not_supported():
    with pytest.raises(pytest.skip.Exception):
        uth.skip_if_bazel_env_error("link: Operation not supported")


def test_skip_if_bazel_env_error_noop_on_unrelated_stderr():
    uth.skip_if_bazel_env_error("undefined reference to `foo'")
    uth.skip_if_bazel_env_error("")
