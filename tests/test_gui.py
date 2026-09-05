"""The GUI layer must stay optional and must not leak into the core."""

import subprocess
import sys

import pytest


def test_importing_the_core_does_not_pull_in_tkinter():
    """The library has to work on a machine with no Tk installed."""
    code = (
        "import sys;"
        "import dp_backup.cli, dp_backup.export, dp_backup.restore, dp_backup.gui;"
        "print('tkinter' in sys.modules)"
    )
    output = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "False"


def test_run_gui_explains_itself_when_tk_is_missing(monkeypatch):
    import dp_backup.gui as gui

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def no_tkinter(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("No module named 'tkinter'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_tkinter)
    with pytest.raises(gui.TkinterMissing) as exc:
        gui.run_gui()
    assert "python -m dp_backup" in str(exc.value)


def test_launcher_dispatches_to_the_cli():
    output = subprocess.run(
        [sys.executable, "DP_Backup_Tool.py", "--version"],
        capture_output=True, text=True, check=True,
    )
    assert "DP Backup Tool" in output.stdout
