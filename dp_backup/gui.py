"""Tkinter front-end.

Nothing here is imported by the core package, and this module imports tkinter
only when :func:`run_gui` is called, so the tool stays usable on machines with
no Tk installed. Long operations run on a worker thread and report back through
a queue, because Tk widgets may only be touched from the main thread.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from typing import Any, Callable

from .export import ExportError, export_structure
from .hashing import HASH_FULL, HASH_NONE, HASH_PARTIAL
from .logsetup import configure_logging
from .report import format_export_report, format_restore_report, write_report
from .restore import RestoreError, restore_structure
from .structure import StructureError, load_structure

logger = logging.getLogger("dp_backup.gui")

WINDOW_TITLE = "DP Backup Tool"


class TkinterMissing(RuntimeError):
    """Tk is not available in this Python installation."""


def _import_tkinter():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError as exc:
        raise TkinterMissing(
            "Tkinter is not available in this Python installation.\n\n"
            "Install it (on Debian/Ubuntu: 'sudo apt install python3-tk'), or "
            "use the command line instead:\n"
            "    python -m dp_backup export <folder> <structure.json>\n"
            "    python -m dp_backup restore <structure.json> <source> <destination>"
        ) from exc
    return tk, filedialog, messagebox, scrolledtext, ttk


def run_gui() -> int:
    """Open the main window. Returns a process exit code."""
    tk, filedialog, messagebox, scrolledtext, ttk = _import_tkinter()
    log_path = configure_logging()
    app = _Application(tk, filedialog, messagebox, scrolledtext, ttk, log_path)
    app.run()
    return 0


class _Application:
    def __init__(self, tk, filedialog, messagebox, scrolledtext, ttk, log_path: str):
        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.ttk = ttk
        self.log_path = log_path
        self.results: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(560, 380)
        self._build()

    # -- layout ---------------------------------------------------------

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text=WINDOW_TITLE, font=("TkDefaultFont", 14, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Record a folder's structure now, so loose files recovered from a "
                "damaged disk can be put back where they belong later."
            ),
            wraplength=520, justify="left",
        ).pack(anchor="w", pady=(4, 14))

        export_box = ttk.LabelFrame(frame, text="1. Record a structure", padding=10)
        export_box.pack(fill="x", pady=4)
        ttk.Label(
            export_box,
            text="Scan a folder and save its layout to a .json file.",
            wraplength=500, justify="left",
        ).pack(anchor="w")

        self.hash_mode = tk.StringVar(value=HASH_FULL)
        row = ttk.Frame(export_box)
        row.pack(anchor="w", pady=(8, 4))
        ttk.Label(row, text="Fingerprint:").pack(side="left")
        for label, value in (
            ("Exact (recommended)", HASH_FULL),
            ("Fast", HASH_PARTIAL),
            ("None", HASH_NONE),
        ):
            ttk.Radiobutton(
                row, text=label, value=value, variable=self.hash_mode
            ).pack(side="left", padx=(8, 0))
        ttk.Button(
            export_box, text="Choose folder and record...", command=self.on_export
        ).pack(anchor="w", pady=(6, 0))

        restore_box = ttk.LabelFrame(frame, text="2. Put files back", padding=10)
        restore_box.pack(fill="x", pady=10)
        ttk.Label(
            restore_box,
            text=(
                "Rebuild the folder tree from a structure file, using the loose "
                "files you recovered."
            ),
            wraplength=500, justify="left",
        ).pack(anchor="w")

        self.dry_run = tk.BooleanVar(value=True)
        self.verify = tk.BooleanVar(value=False)
        self.allow_ambiguous = tk.BooleanVar(value=False)
        options = ttk.Frame(restore_box)
        options.pack(anchor="w", pady=(8, 4))
        ttk.Checkbutton(
            options, text="Preview only (write nothing)", variable=self.dry_run
        ).pack(anchor="w")
        ttk.Checkbutton(
            options, text="Check each copied file afterwards", variable=self.verify
        ).pack(anchor="w")
        ttk.Checkbutton(
            options,
            text="Guess when several files share a size (may place the wrong file)",
            variable=self.allow_ambiguous,
        ).pack(anchor="w")
        ttk.Button(
            restore_box, text="Choose files and restore...", command=self.on_restore
        ).pack(anchor="w", pady=(6, 0))

        self.status = self.tk.StringVar(
            value=f"Log: {self.log_path}" if self.log_path else "Ready"
        )
        ttk.Separator(frame).pack(fill="x", pady=(8, 6))
        ttk.Label(
            frame, textvariable=self.status, wraplength=520, justify="left"
        ).pack(anchor="w")

    def run(self) -> None:
        self.root.mainloop()

    # -- actions --------------------------------------------------------

    def on_export(self) -> None:
        if self.busy:
            return
        source = self.filedialog.askdirectory(title="Select the folder to record")
        if not source:
            return
        output = self.filedialog.asksaveasfilename(
            title="Save the structure file as",
            defaultextension=".json",
            filetypes=[("Structure files", "*.json"), ("All files", "*.*")],
        )
        if not output:
            return

        mode = self.hash_mode.get()
        if mode == HASH_FULL:
            proceed = self.messagebox.askokcancel(
                WINDOW_TITLE,
                "Every file will be read once to fingerprint it. On a large disk "
                "this can take a while.\n\nContinue?",
            )
            if not proceed:
                return

        def work():
            result = export_structure(source, output, hash_mode=mode)
            return format_export_report(result, os.path.abspath(source))

        self._run_in_background("Recording structure...", work, "Export")

    def on_restore(self) -> None:
        if self.busy:
            return
        structure_path = self.filedialog.askopenfilename(
            title="Select the structure file",
            filetypes=[("Structure files", "*.json"), ("All files", "*.*")],
        )
        if not structure_path:
            return
        source = self.filedialog.askdirectory(
            title="Select the folder holding the recovered files"
        )
        if not source:
            return
        destination = self.filedialog.askdirectory(
            title="Select an empty folder to rebuild the tree in"
        )
        if not destination:
            return

        try:
            structure, problems = load_structure(structure_path)
        except StructureError as exc:
            self.messagebox.showerror(WINDOW_TITLE, str(exc))
            return

        if problems:
            preview = "\n".join(f"  {p}" for p in problems[:10])
            more = f"\n  ... and {len(problems) - 10} more" if len(problems) > 10 else ""
            proceed = self.messagebox.askokcancel(
                WINDOW_TITLE,
                f"The structure file has {len(problems)} unusable entries:\n\n"
                f"{preview}{more}\n\nRestore the usable entries anyway?",
            )
            if not proceed:
                return

        if not structure.entries:
            self.messagebox.showinfo(
                WINDOW_TITLE, "The structure file describes nothing to restore."
            )
            return

        dry_run = self.dry_run.get()
        verify = self.verify.get()
        allow_ambiguous = self.allow_ambiguous.get()

        def work():
            result = restore_structure(
                structure, source, destination,
                dry_run=dry_run, verify=verify, allow_ambiguous=allow_ambiguous,
            )
            result.structure_problems = [str(p) for p in problems]
            return format_restore_report(
                result, os.path.abspath(structure_path),
                os.path.abspath(source), os.path.abspath(destination),
            )

        label = "Previewing..." if dry_run else "Restoring files..."
        self._run_in_background(label, work, "Restore")

    # -- background plumbing --------------------------------------------

    def _run_in_background(
        self, status: str, work: Callable[[], str], title: str
    ) -> None:
        self.busy = True
        self.status.set(status)
        self.root.config(cursor="watch")

        def runner() -> None:
            try:
                self.results.put(("ok", work()))
            except (ExportError, RestoreError, StructureError) as exc:
                self.results.put(("error", str(exc)))
            except Exception:  # noqa: BLE001 - surfaced to the user below
                logger.exception("Unexpected failure in %s", title)
                self.results.put(("error", traceback.format_exc()))

        threading.Thread(target=runner, daemon=True).start()
        self.root.after(120, lambda: self._poll(title))

    def _poll(self, title: str) -> None:
        try:
            kind, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(120, lambda: self._poll(title))
            return

        self.busy = False
        self.root.config(cursor="")
        self.status.set(f"Log: {self.log_path}" if self.log_path else "Ready")
        if kind == "error":
            self.messagebox.showerror(f"{title} failed", payload)
        else:
            self._show_report(f"{title} report", payload)

    def _show_report(self, title: str, text: str) -> None:
        tk, ttk = self.tk, self.ttk
        window = tk.Toplevel(self.root)
        window.title(title)
        window.minsize(680, 460)

        widget = self.scrolledtext.ScrolledText(window, wrap="none", width=96, height=28)
        widget.pack(fill="both", expand=True, padx=8, pady=8)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

        buttons = ttk.Frame(window)
        buttons.pack(fill="x", padx=8, pady=(0, 8))

        def save() -> None:
            path = self.filedialog.asksaveasfilename(
                title="Save the report as", defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                write_report(text, path)
            except OSError as exc:
                self.messagebox.showerror(WINDOW_TITLE, f"Could not save the report: {exc}")

        ttk.Button(buttons, text="Save report...", command=save).pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
