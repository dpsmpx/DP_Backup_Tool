#!/usr/bin/env python3
# DP Backup Tool - backup and restore a directory tree's structure.
# Copyright (C) 2025 DP Backup Tool contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Launcher for DP Backup Tool.

Run it with no arguments (or double-click it) for the graphical interface;
pass arguments to use the command line:

    python DP_Backup_Tool.py export  <folder> <structure.json>
    python DP_Backup_Tool.py restore <structure.json> <source> <destination>
"""

from __future__ import annotations

import os
import sys

# Allow running straight from a checkout, without installing the package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if len(sys.argv) > 1:
        from dp_backup.cli import main as cli_main

        return cli_main()

    from dp_backup.gui import TkinterMissing, run_gui

    try:
        return run_gui()
    except TkinterMissing as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
