import importlib
import sys
from pathlib import Path
from typing import Any, List

import compiletools
from compiletools.testhelper import samplesdir


def test_filelist_lists_sample_files_and_imports_dependencies(capsys: Any) -> None:
    """ct-filelist should import dependencies and list known sample files."""

    importlib.reload(compiletools)
    sys.modules.pop("compiletools.filelist", None)

    module = importlib.import_module("compiletools.filelist")

    for attr in ("hunter", "headerdeps", "magicflags"):
        assert hasattr(compiletools, attr), f"compiletools.{attr} should be imported"

    sample_dir = Path(samplesdir()) / "simple"
    sample_main = sample_dir / "helloworld_cpp.cpp"

    exit_code = module.main(["--tests", str(sample_main)])
    assert exit_code == 0

    captured = capsys.readouterr()
    output_lines: List[str] = [line for line in captured.out.splitlines() if line]

    expected_files = sorted(
        str((sample_dir / name).resolve())
        for name in ("helloworld_c.c", "helloworld_cpp.cpp", "test_cflags.c")
    )

    assert output_lines == expected_files
