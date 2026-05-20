import pytest

import compiletools.testhelper as uth


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("ERROR: TLS Certificate verification failed", id="tls-certificate"),
        pytest.param("link: Operation not supported", id="operation-not-supported"),
    ],
)
def test_skip_if_bazel_env_error_skips_on_environment_failures(stderr):
    with pytest.raises(pytest.skip.Exception):
        uth.skip_if_bazel_env_error(stderr)


def test_skip_if_bazel_env_error_noop_on_unrelated_stderr():
    uth.skip_if_bazel_env_error("undefined reference to `foo'")
    uth.skip_if_bazel_env_error("")
