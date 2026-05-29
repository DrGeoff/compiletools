import os

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


@uth.requires_functional_compiler
def test_build_real_backend_extra_argv_overrides_win(tmp_path, monkeypatch):
    """Caller-supplied ``extra_argv`` for a `--cas-*dir` flag must beat the
    helper's pinned defaults.

    Without ordering the helper defaults *before* ``extra_argv``, argparse's
    last-occurrence-wins behaviour silently clobbers a caller's
    ``--cas-pchdir /custom`` with the helper-appended default.
    """
    from compiletools.makefile_backend import MakefileBackend

    monkeypatch.chdir(tmp_path)
    uth.reset()
    try:
        src = tmp_path / "main.cpp"
        src.write_text("int main() { return 0; }\n")

        custom_pchdir = tmp_path / "my-custom-pchdir"

        backend, _graph = uth.build_real_backend(
            MakefileBackend,
            tmp_path,
            [src],
            extra_argv=["--cas-pchdir", str(custom_pchdir)],
        )

        # The resolver appends the variant suffix, so an exact equality check
        # would be too strict — assert the custom path is the parent.
        resolved = backend.args.cas_pchdir
        assert resolved.startswith(str(custom_pchdir)), (
            f"caller's --cas-pchdir override was clobbered: "
            f"got {resolved!r}, expected to start with {str(custom_pchdir)!r}"
        )
        # And make sure it is NOT the helper-default cas-pchdir under tmp_path.
        helper_default = os.path.join(str(tmp_path), "cas-pchdir")
        assert not resolved.startswith(helper_default), (
            f"caller override silently lost — resolved to helper default {resolved!r}"
        )
    finally:
        uth.reset()
