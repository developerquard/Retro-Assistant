import os

import pytest

from core.fsguard import OutsideWorkspace, resolve_inside


def test_inside_path_is_allowed(tmp_path):
    (tmp_path / "ws").mkdir()
    p = resolve_inside(tmp_path / "ws", "Notes/a.txt")
    assert str(p).startswith(str((tmp_path / "ws").resolve()))


def test_dotdot_is_rejected(tmp_path):
    with pytest.raises(OutsideWorkspace):
        resolve_inside(tmp_path / "ws", "../evil.txt")


def test_absolute_path_is_rejected(tmp_path):
    with pytest.raises(OutsideWorkspace):
        resolve_inside(tmp_path / "ws", "/etc/passwd")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_symlink_out_of_workspace_is_rejected(tmp_path):
    ws = tmp_path / "ws"
    secret = tmp_path / "secret"
    ws.mkdir()
    secret.mkdir()
    try:
        os.symlink(secret, ws / "link")
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(OutsideWorkspace):
        resolve_inside(ws, "link/x.txt")
