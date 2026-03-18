"""Tests for doc module."""

import compiletools.doc


def test_main(capsys):
    rc = compiletools.doc.main(argv=[])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--man" in out
