import os
import subprocess

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


def test_copy_example_workspace_skips_gitignored_build_outputs(tmp_path):
    """Gitignored build outputs inside an example source dir must not be
    copied into the e2e workspace.

    Building an example in-tree (the shipped examples double as live demos)
    leaves gitignored outputs (``bin/``, ``cas-*/``) inside the example
    source dir. Copying them into a pristine workspace poisons the build:
    a stale flat-layout ``bin/snake`` *file* blocks creation of the
    source-mirrored ``bin/snake/`` publish *directory*, failing every
    backend cell with FileExistsError.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("bin/\n")
    # The example lives below the repo root, like the shipped examples do.
    src = repo / "example_src"
    src.mkdir()
    (src / "main.cpp").write_text("int main() { return 0; }\n")
    (src / "sub").mkdir()
    (src / "sub" / "helper.cpp").write_text("int helper() { return 1; }\n")
    # Stale in-tree build outputs, top-level and nested.
    (src / "bin").mkdir()
    (src / "bin" / "snake").write_text("stale flat-layout executable\n")
    (src / "sub" / "bin").mkdir()
    (src / "sub" / "bin" / "stale.o").write_text("stale object\n")

    dst = uth.copy_example_workspace(src, tmp_path / "ws")

    assert (dst / "main.cpp").is_file()
    assert (dst / "sub" / "helper.cpp").is_file()
    assert (dst / ".git" / "HEAD").is_file()
    assert not (dst / "bin").exists(), "gitignored top-level bin/ was copied into the workspace"
    assert not (dst / "sub" / "bin").exists(), "gitignored nested bin/ was copied into the workspace"


def test_copy_example_workspace_copies_everything_outside_a_repo(tmp_path):
    """Without a surrounding git repo there is no ignore information —
    the copy must fall back to copying everything rather than erroring."""
    src = tmp_path / "plain_src"
    (src / "bin").mkdir(parents=True)
    (src / "main.cpp").write_text("int main() { return 0; }\n")
    (src / "bin" / "artefact").write_text("kept: no repo, no ignore rules\n")

    dst = uth.copy_example_workspace(src, tmp_path / "ws")

    assert (dst / "main.cpp").is_file()
    assert (dst / "bin" / "artefact").is_file()


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
