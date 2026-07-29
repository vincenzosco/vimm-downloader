"""
gui.py - tkinter GUI for Vimm Bulk Downloader.

Provides a tabbed interface:
  - Search: Search vimm.net, browse results, add to download queue
  - Downloads: Manage download queue with real-time progress bars
  - Settings: Configure Tor/proxy, output directory, concurrent workers

Requires: Python 3.10+ (tkinter is built-in).
"""

from __future__ import annotations

import os
import re
import sys
import json
import queue
import time
import logging
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import requests

from . import __version__
from .vimm_scraper import (
    validate_vault_url,
    extract_download_url,
    VimmScraperError,
)
from .vimm_search import search_vimm
from .ip_rotator import (
    create_rotator,
    detect_tor,
    ensure_tor_running,
    IPRotator,
    TorRotator,
)
from .tor_manager import TorManager, stop_managed_tor, control_port_reachable, enable_control_port_and_fix_cookie
from .console_list import CONSOLE_TABLE
from .downloader import _format_size

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = Path.home() / ".vimm_bulk_downloader" / "config.json"
VAULT_URL_RE = re.compile(r"^https://vimm\.net/vault/\d+$")

# Color scheme
BG_PRIMARY = "#1a1a2e"
BG_SECONDARY = "#16213e"
BG_TERTIARY = "#0f3460"
ACCENT = "#e94560"
ACCENT_HOVER = "#ff6b81"
TEXT_PRIMARY = "#eaeaea"
TEXT_SECONDARY = "#a0a0b0"
TEXT_ACCENT = "#e94560"
SUCCESS = "#2ecc71"
WARNING = "#f39c12"
ERROR = "#e74c3c"
PROGRESS_BG = "#0f3460"
PROGRESS_FILL = "#e94560"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load saved settings from config file."""
    default = {
        "mode": "tor",
        "tor_socks_port": 9050,
        "tor_control_port": 9051,
        "proxy_file": "",
        "proxy_use_default": False,
        "output_dir": str(Path.cwd() / "downloads"),
        "workers": 3,
        "download_format": "auto",
        "window_geometry": "1100x720",
    }
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                return {**default, **json.load(f)}
    except Exception:
        pass
    return default


def save_config(config: dict):
    """Persist settings to config file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def shorten_url(url: str, max_len: int = 60) -> str:
    """Truncate a URL for display."""
    if len(url) <= max_len:
        return url
    return url[:max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Styled widgets
# ---------------------------------------------------------------------------

class StyledFrame(ttk.Frame):
    """Frame base with consistent styling."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(padding=10)


class SearchResultsTable(ttk.Frame):
    """A table for displaying search results with selectable rows."""

    def __init__(self, master, on_select=None, on_double_click=None, **kwargs):
        super().__init__(master, **kwargs)
        self.on_select = on_select
        self.on_double_click = on_double_click
        self._selected_index: Optional[int] = None
        self._results: list = []

        # --- Treeview ---
        columns = ("#", "System", "Title", "Region", "Version", "Rating")
        self.tree = ttk.Treeview(self, columns=columns, show="headings",
                                  selectmode="browse", height=16)

        self.tree.heading("#", text="#")
        self.tree.heading("System", text="System")
        self.tree.heading("Title", text="Title")
        self.tree.heading("Region", text="Region")
        self.tree.heading("Version", text="Version")
        self.tree.heading("Rating", text="Rating")

        self.tree.column("#", width=40, anchor="center")
        self.tree.column("System", width=120)
        self.tree.column("Title", width=350, minwidth=200)
        self.tree.column("Region", width=100)
        self.tree.column("Version", width=80, anchor="center")
        self.tree.column("Rating", width=80, anchor="center")

        # Scrollbars
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Bindings
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)

    def set_results(self, results: list):
        """Populate the table with search results."""
        self._results = results
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(results, 1):
            system = r.system if r.system else "-"
            rating = r.rating if r.rating else "-"
            self.tree.insert("", "end", iid=str(i - 1),
                             values=(i, system, r.title, r.region, r.version, rating))

    def get_selected_result(self) -> Optional[dict]:
        """Return the selected search result data, or None."""
        sel = self.tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self._results):
            r = self._results[idx]
            return {"title": r.title, "vault_url": r.vault_url,
                    "system": r.system, "region": r.region,
                    "version": r.version}
        return None

    def _on_select(self, event):
        if self.on_select:
            self.on_select(self.get_selected_result())

    def _on_double_click(self, event):
        if self.on_double_click:
            self.on_double_click(self.get_selected_result())


class DownloadCard(ttk.Frame):
    """A single download item with progress bar and status."""

    def __init__(self, master, vault_url: str, filename: str, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(padding=(8, 4))
        self.vault_url = vault_url
        self.filename = filename
        self.status = "queued"  # queued | running | done | failed
        self.bytes_downloaded = 0
        self.total_bytes: Optional[int] = None

        # Row 0: filename and status
        top = ttk.Frame(self)
        top.pack(fill="x")

        self._name_label = ttk.Label(top, text=filename, font=("Segoe UI", 10, "bold"),
                                      foreground=TEXT_PRIMARY)
        self._name_label.pack(side="left")

        self._status_label = ttk.Label(top, text="Queued", font=("Segoe UI", 9),
                                        foreground=TEXT_SECONDARY)
        self._status_label.pack(side="right")

        # Row 1: Progress bar and percentage
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(2, 0))

        self._progress = ttk.Progressbar(bottom, length=400, mode="determinate")
        self._progress.pack(side="left", fill="x", expand=True)

        self._pct_label = ttk.Label(bottom, text="0%", width=6,
                                     font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self._pct_label.pack(side="left", padx=(6, 0))

        self._speed_label = ttk.Label(bottom, text="", width=14,
                                       font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self._speed_label.pack(side="right")

    def update_progress(self, current: int, total: Optional[int],
                        speed: str = "", status_text: str = ""):
        """Update the progress bar and labels."""
        self.bytes_downloaded = current
        self.total_bytes = total

        if total and total > 0:
            pct = min(100.0, current / total * 100)
            self._progress["value"] = pct
            self._pct_label["text"] = f"{pct:.1f}%"
        else:
            self._progress["value"] = 0
            self._progress["mode"] = "indeterminate"
            self._pct_label["text"] = "..."

        if speed:
            self._speed_label["text"] = speed

        if status_text:
            self._status_label["text"] = status_text

    def mark_done(self, elapsed: float):
        """Mark download as complete."""
        self.status = "done"
        self._progress["value"] = 100
        self._pct_label["text"] = "100%"
        self._status_label["text"] = f"Done ({elapsed:.1f}s)"
        self._status_label["foreground"] = SUCCESS

    def mark_failed(self, reason: str = "Failed"):
        """Mark download as failed."""
        self.status = "failed"
        self._status_label["text"] = f"ERR {reason}"
        self._status_label["foreground"] = ERROR

    def mark_running(self):
        """Mark download as in progress."""
        self.status = "running"
        self._status_label["text"] = "Downloading..."
        self._status_label["foreground"] = ACCENT


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class VimmBulkGUI:
    """tkinter GUI for Vimm Bulk Downloader."""

    def __init__(self):
        self.config = load_config()
        self.root = tk.Tk()
        self.root.title(f"Vimm Bulk Downloader v{__version__}")
        self.root.geometry(self.config.get("window_geometry", "1100x720"))
        self.root.minsize(900, 600)

        # --- State ---
        self.download_queue: list[dict] = []      # vault_url, download_url, filename, card
        self.active_downloads: set = set()
        self._stop_flag = False
        self._progress_queue: queue.Queue = queue.Queue()
        self._rotator: Optional[IPRotator] = None

        # --- Styling ---
        self._setup_styles()

        # --- Build UI ---
        self._build_ui()

        # --- Poll progress queue ---
        self._poll_progress()

        # --- Bind close ---
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # =======================================================================
    # Style
    # =======================================================================

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Colors
        style.configure(".", background=BG_PRIMARY, foreground=TEXT_PRIMARY,
                         fieldbackground=BG_SECONDARY, font=("Segoe UI", 10))

        style.configure("TFrame", background=BG_PRIMARY)
        style.configure("TLabel", background=BG_PRIMARY, foreground=TEXT_PRIMARY)
        style.configure("TButton", background=BG_TERTIARY, foreground=TEXT_PRIMARY,
                         borderwidth=1, focusthickness=3, padding=(12, 6))
        style.map("TButton",
                  background=[("active", ACCENT), ("pressed", ACCENT_HOVER)],
                  foreground=[("active", "white")])

        style.configure("Accent.TButton", background=ACCENT, foreground="white",
                         font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("pressed", "#c0392b")])

        style.configure("TEntry", fieldbackground=BG_SECONDARY, foreground=TEXT_PRIMARY,
                         insertcolor=TEXT_PRIMARY, borderwidth=1)
        style.configure("TCombobox", fieldbackground=BG_SECONDARY, foreground=TEXT_PRIMARY,
                         arrowcolor=TEXT_PRIMARY)

        style.configure("Treeview", background=BG_SECONDARY, foreground=TEXT_PRIMARY,
                         fieldbackground=BG_SECONDARY, borderwidth=0, rowheight=28)
        style.map("Treeview", background=[("selected", BG_TERTIARY)],
                  foreground=[("selected", "white")])
        style.configure("Treeview.Heading", background=BG_TERTIARY,
                         foreground=TEXT_PRIMARY, relief="flat", padding=(6, 4))

        style.configure("TNotebook", background=BG_PRIMARY, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_SECONDARY, foreground=TEXT_PRIMARY,
                         padding=(16, 6), font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", BG_TERTIARY), ("active", BG_TERTIARY)],
                  foreground=[("selected", "white")])

        style.configure("TProgressbar", background=PROGRESS_FILL,
                         troughcolor=PROGRESS_BG, thickness=16, borderwidth=0)
        style.configure("Horizontal.TProgressbar", background=PROGRESS_FILL,
                         troughcolor=PROGRESS_BG)

        style.configure("TSeparator", background=BG_TERTIARY)

    # =======================================================================
    # Build UI
    # =======================================================================

    def _build_ui(self):
        # Main container
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        # Header
        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=f"Vimm Bulk Downloader",
                  font=("Segoe UI", 16, "bold"), foreground=ACCENT).pack(side="left")
        ttk.Label(header, text=f"v{__version__}",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).pack(side="left", padx=(6, 0))

        # Notebook (tabs)
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)

        self._build_search_tab()
        self._build_downloads_tab()
        self._build_settings_tab()

    # -----------------------------------------------------------------------
    # Tab 1: Search
    # -----------------------------------------------------------------------

    def _build_search_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Search")

        # --- Controls row ---
        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 8))

        ttk.Label(controls, text="Console:", font=("Segoe UI", 9),
                  foreground=TEXT_SECONDARY).pack(side="left")
        self.search_console_var = tk.StringVar(value="all")
        self.search_console_combo = ttk.Combobox(
            controls, textvariable=self.search_console_var, width=20, state="readonly")
        console_names = [
            f"{entry['Platform'].split(' (')[0]}"
            for entry in CONSOLE_TABLE
        ]
        self.search_console_combo["values"] = [
            f"{entry['Platform']}" for entry in CONSOLE_TABLE
        ]
        self.search_console_combo.current(0)
        self.search_console_combo.pack(side="left", padx=(4, 12))

        ttk.Label(controls, text="Query:", font=("Segoe UI", 9),
                  foreground=TEXT_SECONDARY).pack(side="left")
        self.search_entry = ttk.Entry(controls, width=35, font=("Segoe UI", 10))
        self.search_entry.pack(side="left", padx=(4, 8))
        self.search_entry.bind("<Return>", lambda e: self._do_search())

        self.search_btn = ttk.Button(controls, text="Search",
                                      style="Accent.TButton",
                                      command=self._do_search)
        self.search_btn.pack(side="left")

        # --- Results table ---
        self.results_table = SearchResultsTable(
            tab,
            on_select=self._on_result_select,
            on_double_click=self._on_result_add,
        )
        self.results_table.pack(fill="both", expand=True, pady=(0, 8))

        # --- Bottom bar: selection info + actions ---
        bottom = ttk.Frame(tab)
        bottom.pack(fill="x")

        self.sel_info = ttk.Label(bottom, text="No selection",
                                   font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self.sel_info.pack(side="left")

        self.add_btn = ttk.Button(bottom, text="+ Add Selected to Queue",
                                   state="disabled", command=self._add_selected)
        self.add_btn.pack(side="right", padx=(6, 0))

        self.add_all_btn = ttk.Button(bottom, text="+ Add All to Queue",
                                       command=self._add_all_results)
        self.add_all_btn.pack(side="right")

        self.status_label = ttk.Label(tab, text="Ready",
                                       font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self.status_label.pack(anchor="w")

    def _do_search(self):
        """Run a search in a background thread."""
        console_raw = self.search_console_var.get()
        # Extract the console name from the display string (before the " (")
        console_name = console_raw.split(" (")[0].strip().lower()
        query = self.search_entry.get().strip()

        if not query:
            messagebox.showwarning("Empty Query", "Please enter a search query.")
            return

        self.search_btn["state"] = "disabled"
        self.search_btn["text"] = "Searching..."
        self.status_label["text"] = f"Searching for '{query}' on {console_name}..."

        def search_thread():
            try:
                resp = search_vimm(console_name, query)
                self.root.after(0, self._on_search_done, resp)
            except Exception as e:
                self.root.after(0, self._on_search_error, str(e))

        threading.Thread(target=search_thread, daemon=True).start()

    def _on_search_done(self, resp):
        """Handle search results on main thread."""
        self.search_btn["state"] = "normal"
        self.search_btn["text"] = "Search"

        if resp.error:
            self.status_label["text"] = f"Search error: {resp.error}"
            return

        if resp.total == 0:
            self.status_label["text"] = f"No results for '{resp.query}'."
            self.results_table.set_results([])
            return

        self.results_table.set_results(resp.results)
        self.status_label["text"] = f"Found {resp.total} result(s) for '{resp.query}'."

    def _on_search_error(self, error: str):
        self.search_btn["state"] = "normal"
        self.search_btn["text"] = "Search"
        self.status_label["text"] = f"Search failed: {error}"

    def _on_result_select(self, result: Optional[dict]):
        """Update selection info and button state."""
        if result:
            self.sel_info["text"] = f"Selected: {result['title']}  ({shorten_url(result['vault_url'], 50)})"
            self.add_btn["state"] = "normal"
        else:
            self.sel_info["text"] = "No selection"
            self.add_btn["state"] = "disabled"

    def _on_result_add(self, result: Optional[dict]):
        """Double-click: add to queue."""
        if result:
            self._add_to_queue(result["vault_url"], result["title"])
            self.notebook.select(1)  # Switch to Downloads tab

    def _add_selected(self):
        """Add selected search result to download queue."""
        result = self.results_table.get_selected_result()
        if result:
            self._add_to_queue(result["vault_url"], result["title"])

    def _add_all_results(self):
        """Add all search results to download queue."""
        if not hasattr(self.results_table, '_results') or not self.results_table._results:
            return
        count = 0
        for r in self.results_table._results:
            if self._add_to_queue(r.vault_url, r.title, silent=True):
                count += 1
        self._refresh_queue_display()
        self.status_label["text"] = f"Added {count} game(s) to download queue."

    def _add_to_queue(self, vault_url: str, title: str, silent: bool = False) -> bool:
        """Add a game to the download queue if not already present."""
        # Check for duplicates
        for item in self.download_queue:
            if item["vault_url"] == vault_url:
                return False

        filename = re.sub(r'[^\w\-_.() ]', '_', title).strip()
        if not filename:
            vault_id = vault_url.rstrip("/").split("/")[-1]
            filename = f"vimm_vault_{vault_id}"

        self.download_queue.append({
            "vault_url": vault_url,
            "download_url": None,  # resolved later
            "filename": filename,
            "title": title,
            "format": self.format_var.get(),  # selected format
        })
        if not silent:
            self._refresh_queue_display()
            self.status_label["text"] = f"Added '{title}' to queue."
        return True

    # -----------------------------------------------------------------------
    # Tab 2: Downloads
    # -----------------------------------------------------------------------

    def _build_downloads_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Downloads")

        # --- Top bar ---
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Download Queue",
                  font=("Segoe UI", 13, "bold")).pack(side="left")

        self.queue_count_label = ttk.Label(top, text="0 items",
                                            font=("Segoe UI", 9),
                                            foreground=TEXT_SECONDARY)
        self.queue_count_label.pack(side="left", padx=(8, 0))

        # Action buttons
        self.start_all_btn = ttk.Button(top, text="Start All",
                                         style="Accent.TButton",
                                         command=self._start_all_downloads)
        self.start_all_btn.pack(side="right", padx=(4, 0))

        self.clear_btn = ttk.Button(top, text="Clear Done",
                                     command=self._clear_done)
        self.clear_btn.pack(side="right", padx=(4, 0))

        self.download_format_btn = ttk.Button(top, text="+ URL",
                                               command=self._add_url_dialog)
        self.download_format_btn.pack(side="right", padx=(4, 0))

        # --- Format selector + URL entry row ---
        format_url_frame = ttk.Frame(tab)
        format_url_frame.pack(fill="x", pady=(0, 8))

        # Download format selector
        ttk.Label(format_url_frame, text="Format:",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).pack(side="left")
        self.format_var = tk.StringVar(value=self.config.get("download_format", "auto"))
        self.format_combo = ttk.Combobox(
            format_url_frame,
            textvariable=self.format_var,
            values=["auto", "iso", "wbfs", "rvz", "zip", "7z"],
            width=8,
            state="readonly",
        )
        self.format_combo.pack(side="left", padx=(4, 12))

        # URL entry
        self.dl_url_var = tk.StringVar()
        self.dl_url_entry = ttk.Entry(format_url_frame, textvariable=self.dl_url_var,
                                       font=("Segoe UI", 10))
        self.dl_url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.dl_url_btn = ttk.Button(format_url_frame, text="Add URL",
                                      command=self._add_url_from_entry)
        self.dl_url_btn.pack(side="right")
        self.dl_url_entry.bind("<Return>", lambda e: self._add_url_from_entry())

        # --- Scrollable download list ---
        canvas = tk.Canvas(tab, background=BG_PRIMARY, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        self.dl_container = ttk.Frame(canvas)
        self.dl_container.bind("<Configure>",
                                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.dl_container, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Bind mousewheel (scoped to canvas only via enter/leave)
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _bind_mw(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")
        def _unbind_mw(event):
            canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _bind_mw)
        canvas.bind("<Leave>", _unbind_mw)

        self._dl_canvas = canvas

        # --- Status bar ---
        self.dl_status = ttk.Label(tab, text="Ready",
                                    font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self.dl_status.pack(fill="x", pady=(4, 0))

    def _add_url_dialog(self):
        """Dialog to manually enter vault URLs."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Add URLs")
        dialog.geometry("500x300")
        dialog.configure(bg=BG_PRIMARY)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="Enter vault URLs (one per line):",
                  font=("Segoe UI", 10)).pack(pady=(10, 5), padx=10, anchor="w")

        text = tk.Text(dialog, bg=BG_SECONDARY, fg=TEXT_PRIMARY,
                        insertbackground=TEXT_PRIMARY,
                        font=("Segoe UI", 10), bd=0, highlightthickness=0)
        text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def add_urls():
            count = 0
            for line in text.get("1.0", "end").strip().splitlines():
                url = line.strip()
                if url and VAULT_URL_RE.match(url):
                    vault_id = url.rstrip("/").split("/")[-1]
                    if self._add_to_queue(url, f"vault_{vault_id}", silent=True):
                        count += 1
            self._refresh_queue_display()
            self.dl_status["text"] = f"Added {count} URL(s) to queue."
            dialog.destroy()

        ttk.Button(dialog, text="Add to Queue", style="Accent.TButton",
                    command=add_urls).pack(pady=(0, 10))

    def _add_url_from_entry(self):
        """Add a single URL from the entry field."""
        url = self.dl_url_var.get().strip()
        if not url:
            return
        if not VAULT_URL_RE.match(url):
            messagebox.showwarning("Invalid URL",
                                   "URL must match: https://vimm.net/vault/XXXX")
            return
        vault_id = url.rstrip("/").split("/")[-1]
        self._add_to_queue(url, f"vault_{vault_id}")
        self.dl_url_var.set("")

    def _refresh_queue_display(self):
        """Rebuild the download card list."""
        for w in self.dl_container.winfo_children():
            w.destroy()

        if not self.download_queue:
            ttk.Label(self.dl_container,
                      text="No downloads in queue.\nAdd games from the Search tab or paste URLs above.",
                      font=("Segoe UI", 11), foreground=TEXT_SECONDARY,
                      justify="center").pack(pady=40)
            self.queue_count_label["text"] = "0 items"
            return

        for item in self.download_queue:
            card = DownloadCard(self.dl_container,
                                 vault_url=item["vault_url"],
                                 filename=item["filename"])
            card.pack(fill="x", pady=2)
            if "card" not in item:
                item["card"] = card

        self.queue_count_label["text"] = f"{len(self.download_queue)} items"

    def _start_all_downloads(self):
        """Start the download queue in background threads."""
        if not self.download_queue:
            messagebox.showinfo("Empty Queue", "Add some games to the queue first.")
            return

        # Filter to items without download URLs yet (or queued items)
        pending = [item for item in self.download_queue
                   if item.get("card") and item["card"].status == "queued"]
        if not pending:
            messagebox.showinfo("All Done", "All items have been downloaded already.")
            return

        self._stop_flag = False
        self.start_all_btn["state"] = "disabled"
        self.dl_status["text"] = f"Starting {len(pending)} download(s)..."

        # Start in a separate thread so we don't block the UI
        def download_thread():
            self._run_downloads(pending)

        threading.Thread(target=download_thread, daemon=True).start()

    def _run_downloads(self, pending: list[dict]):
        """Run downloads with progress tracking."""

        # Phase 1: Resolve download URLs
        session = requests.Session()
        for item in pending:
            if self._stop_flag:
                break
            card = item.get("card")
            if not card:
                continue
            # Queue UI updates instead of touching widgets directly
            self._progress_queue.put({
                "card": card, "running": True, "status_text": "Resolving...",
            })

            try:
                dl_url = extract_download_url(
                    item["vault_url"],
                    session=session,
                    prefer_primary=True,
                )
                item["download_url"] = dl_url
            except VimmScraperError as e:
                self._progress_queue.put({
                    "card": card, "failed": True, "reason": str(e),
                })
                continue

        session.close()

        # Phase 2: Ensure Tor is running if needed
        ctrl_port = self.config["tor_control_port"]
        if self.config["mode"] == "tor":
            ok = ensure_tor_running(
                socks_port=self.config["tor_socks_port"],
                control_port=ctrl_port,
                interactive_install=False,
            )
            if not ok:
                def _on_tor_auto_fail():
                    self.dl_status["text"] = "Tor failed to start. Check Settings tab."
                    self.start_all_btn["state"] = "normal"
                self.root.after(0, _on_tor_auto_fail)
                return
            # Check ControlPort and offer to enable it (on main thread)
            if not control_port_reachable(ctrl_port):
                def _prompt_enable():
                    do_enable = messagebox.askyesno(
                        "Enable ControlPort?",
                        "Tor SOCKS is working but the ControlPort is not reachable.\n"
                        "IP rotation requires the ControlPort to be enabled.\n\n"
                        "Enable it now? (requires sudo password)",
                    )
                    if do_enable:
                        def _enable_thread():
                            ok = enable_control_port_and_fix_cookie(
                                port=ctrl_port,
                                socks_port=self.config["tor_socks_port"],
                            )
                            if ok:
                                self.root.after(0, lambda: self.dl_status.__setitem__(
                                    "text", "ControlPort enabled! Tor is ready."
                                ))
                            else:
                                self.root.after(0, lambda: self.dl_status.__setitem__(
                                    "text", "Could not enable ControlPort. Download will still work."
                                ))
                        threading.Thread(target=_enable_thread, daemon=True).start()
                self.root.after(0, _prompt_enable)

        # Phase 3: Download with IP rotation
        try:
            # Determine proxy list: "default" if checkbox is checked
            proxy_list_arg = "default" if self.config.get("proxy_use_default") else None
            proxy_file_arg = self.config.get("proxy_file") or None

            rotator = create_rotator(
                mode=self.config["mode"],
                proxy_file=proxy_file_arg,
                proxy_list=proxy_list_arg,
                tor_socks_port=self.config["tor_socks_port"],
                tor_control_port=self.config["tor_control_port"],
            )
        except (ValueError, FileNotFoundError) as e:
            for item in pending:
                card = item.get("card")
                if card:
                    self._progress_queue.put({
                        "card": card, "failed": True, "reason": str(e),
                    })
            self.root.after(0, self._on_downloads_complete)
            return

        # Use the user-configured worker count (no limit for Tor mode)
        effective_workers = min(self.config["workers"], len(pending))

        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = []
            for item in pending:
                if self._stop_flag:
                    break
                if not item.get("download_url"):
                    continue
                future = pool.submit(
                    self._download_single, item, rotator
                )
                futures.append(future)

            for future in futures:
                future.result()

        self.root.after(0, self._on_downloads_complete)

    def _download_single(self, item: dict, rotator: IPRotator):
        """Download a single file with progress updates."""
        card = item.get("card")
        if not card or not item.get("download_url"):
            return

        dl_url = item["download_url"]
        output_dir = Path(self.config.get("output_dir", "downloads"))
        output_dir.mkdir(parents=True, exist_ok=True)

        vault_id = item["vault_url"].rstrip("/").split("/")[-1]
        # Use the user's chosen format as fallback extension
        chosen_fmt = item.get("format", "auto")
        fallback_ext = f".{chosen_fmt}" if chosen_fmt != "auto" else ".zip"
        output_path = output_dir / f"{item['filename']}{fallback_ext}"

        # Rotate IP
        rotator.rotate()

        # Create session with proxy
        session = requests.Session()
        proxies = rotator.get_proxies()
        if proxies:
            session.proxies.update(proxies)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
            "Referer": "https://vimm.net/vault/",
            "Cookie": "counted=1",
        })

        def _q(**kw):
            """Shortcut to queue a card update."""
            self._progress_queue.put({"card": card, **kw})

        try:
            # HEAD request for filename
            try:
                head = session.head(dl_url, timeout=15)
                cd = head.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    fname = cd.split("filename=")[-1].strip('" ')
                    if fname:
                        output_path = output_dir / fname
            except Exception:
                pass

            # Check for existing file
            if output_path.exists():
                _q(done=True, elapsed=0)
                return

            # Stream download
            resp = session.get(dl_url, stream=True, timeout=60)
            resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", "0")) or None
            written = 0
            start_time = time.time()
            last_update = 0.0

            if total:
                _q(set_total=total, set_value=0, mode="determinate")
            else:
                _q(set_total=None, set_value=0, mode="indeterminate")

            with open(output_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=128 * 1024):
                    if self._stop_flag:
                        fh.close()
                        os.remove(output_path)
                        card.status = "queued"
                        _q(stopped=True)
                        return

                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)

                        # Update progress (throttled)
                        now = time.time()
                        if now - last_update > 0.15:  # ~6 updates/sec
                            last_update = now
                            elapsed = now - start_time
                            speed = _format_size(int(written / elapsed)) + "/s" if elapsed > 0 else ""
                            pct = (written / total * 100) if total else 0
                            _q(current=written, total=total, pct=pct, speed=speed)

            elapsed = time.time() - start_time
            _q(done=True, elapsed=elapsed, filename=str(output_path.name), bytes=written)

        except Exception as e:
            _q(failed=True, reason=str(e)[:60])
            if output_path.exists():
                os.remove(output_path)
        finally:
            session.close()

    def _poll_progress(self):
        """Periodically check the progress queue and update UI on the main thread."""
        try:
            while True:
                msg = self._progress_queue.get_nowait()
                card = msg.get("card")
                if not card:
                    continue

                try:
                    if msg.get("done"):
                        card.mark_done(msg.get("elapsed", 0))
                    elif msg.get("failed"):
                        card.mark_failed(msg.get("reason", "Failed"))
                    elif msg.get("running"):
                        card.mark_running()
                        card.update_progress(0, None, status_text=msg.get("status_text", ""))
                    elif msg.get("stopped"):
                        card.status = "queued"
                        card._status_label["text"] = "Stopped"
                        card._status_label["foreground"] = TEXT_SECONDARY
                        card._progress["value"] = 0
                        card._progress["mode"] = "determinate"
                    elif "mode" in msg:
                        card._progress["mode"] = msg["mode"]
                        if "set_value" in msg:
                            card._progress["value"] = msg["set_value"]
                        if "set_total" in msg:
                            card.total_bytes = msg["set_total"]
                    else:
                        card.update_progress(
                            msg.get("current", 0), msg.get("total"),
                            speed=msg.get("speed", ""),
                        )
                except tk.TclError:
                    # Card widget was destroyed (e.g. queue refresh), skip gracefully
                    pass
        except queue.Empty:
            pass

        self.root.after(50, self._poll_progress)

    def _on_downloads_complete(self):
        """Called when all downloads finish."""
        self.start_all_btn["state"] = "normal"
        self.dl_status["text"] = "All downloads complete!"
        self._refresh_queue_display()

    def _clear_done(self):
        """Remove completed items from the queue."""
        self.download_queue = [
            item for item in self.download_queue
            if not item.get("card") or item["card"].status in ("queued", "running")
        ]
        self._refresh_queue_display()

    # -----------------------------------------------------------------------
    # Tab 3: Settings
    # -----------------------------------------------------------------------

    def _build_settings_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Settings")

        # --- Mode ---
        mode_frame = ttk.LabelFrame(tab, text="IP Rotation", padding=12)
        mode_frame.pack(fill="x", pady=(0, 12))

        self.mode_var = tk.StringVar(value=self.config.get("mode", "tor"))
        ttk.Radiobutton(mode_frame, text="Tor (free, needs Tor daemon)",
                         variable=self.mode_var, value="tor",
                         command=self._toggle_mode).pack(anchor="w", pady=2)
        ttk.Radiobutton(mode_frame, text="Proxy list",
                         variable=self.mode_var, value="proxy",
                         command=self._toggle_mode).pack(anchor="w", pady=2)

        # Tor settings
        self.tor_frame = ttk.Frame(mode_frame)
        self.tor_frame.pack(fill="x", pady=(8, 0))

        ttk.Label(self.tor_frame, text="SOCKS Port:",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).grid(row=0, column=0, sticky="w")
        self.tor_socks_var = tk.StringVar(value=str(self.config.get("tor_socks_port", 9050)))
        ttk.Entry(self.tor_frame, textvariable=self.tor_socks_var,
                  width=10).grid(row=0, column=1, padx=(4, 16), sticky="w")

        ttk.Label(self.tor_frame, text="Control Port:",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).grid(row=0, column=2, sticky="w")
        self.tor_ctrl_var = tk.StringVar(value=str(self.config.get("tor_control_port", 9051)))
        ttk.Entry(self.tor_frame, textvariable=self.tor_ctrl_var,
                  width=10).grid(row=0, column=3, padx=(4, 0), sticky="w")

        self.tor_check_btn = ttk.Button(self.tor_frame, text="Test Tor",
                                         command=self._test_tor)
        self.tor_check_btn.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # Proxy settings
        self.proxy_frame = ttk.Frame(mode_frame)
        self.proxy_frame.pack(fill="x", pady=(8, 0))

        # Default proxy pool checkbox
        self.proxy_default_var = tk.BooleanVar(value=self.config.get("proxy_use_default", False))
        self.proxy_default_cb = ttk.Checkbutton(
            self.proxy_frame,
            text="Use default free proxy pool (Proxifly CDN — SOCKS5, updated every 5 min)",
            variable=self.proxy_default_var,
            command=self._toggle_proxy_source,
        )
        self.proxy_default_cb.pack(anchor="w", pady=(0, 4))

        proxy_file_row = ttk.Frame(self.proxy_frame)
        proxy_file_row.pack(fill="x")

        ttk.Label(proxy_file_row, text="Proxy file (optional):",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).pack(side="left")
        self.proxy_path_var = tk.StringVar(value=self.config.get("proxy_file", ""))
        self.proxy_path_entry = ttk.Entry(proxy_file_row, textvariable=self.proxy_path_var,
                                          width=40)
        self.proxy_path_entry.pack(side="left", padx=(4, 6))
        self.proxy_browse_btn = ttk.Button(proxy_file_row, text="Browse...",
                                            command=self._browse_proxy)
        self.proxy_browse_btn.pack(side="left")

        # Apply initial proxy source toggle
        self._toggle_proxy_source()

        # --- Output ---
        out_frame = ttk.LabelFrame(tab, text="Output", padding=12)
        out_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(out_frame, text="Download directory:",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).pack(anchor="w")
        dir_row = ttk.Frame(out_frame)
        dir_row.pack(fill="x", pady=(4, 0))
        self.output_dir_var = tk.StringVar(value=self.config.get("output_dir", ""))
        ttk.Entry(dir_row, textvariable=self.output_dir_var,
                  width=50).pack(side="left", padx=(0, 6))
        ttk.Button(dir_row, text="Browse...",
                    command=self._browse_output).pack(side="left")

        # --- Performance ---
        perf_frame = ttk.LabelFrame(tab, text="Performance", padding=12)
        perf_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(perf_frame, text="Max concurrent downloads:",
                  font=("Segoe UI", 9), foreground=TEXT_SECONDARY).pack(anchor="w")
        self.workers_var = tk.StringVar(value=str(self.config.get("workers", 3)))
        workers_spin = ttk.Spinbox(perf_frame, from_=1, to=40,
                                    textvariable=self.workers_var, width=8)
        workers_spin.pack(anchor="w", pady=(4, 0))
        ttk.Label(perf_frame,
                  text="Enough proxies needed — each concurrent download needs a different IP.",
                  font=("Segoe UI", 8), foreground=TEXT_SECONDARY).pack(anchor="w", pady=(0, 0))

        # --- Buttons ---
        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_frame, text="Save Settings",
                    style="Accent.TButton",
                    command=self._save_settings).pack(side="left")
        ttk.Button(btn_frame, text="Open Output Folder",
                    command=self._open_output).pack(side="left", padx=(8, 0))

        self.settings_status = ttk.Label(tab, text="",
                                          font=("Segoe UI", 9), foreground=TEXT_SECONDARY)
        self.settings_status.pack(anchor="w", pady=(8, 0))

        # Apply initial visibility
        self._toggle_mode()

    def _toggle_mode(self):
        """Show/hide Tor vs proxy settings based on mode."""
        is_tor = self.mode_var.get() == "tor"
        for w in self.tor_frame.winfo_children():
            # Only widgets that support state: Entry, Button, Spinbox
            if isinstance(w, (ttk.Entry, ttk.Button, ttk.Spinbox)):
                try:
                    w.configure(state="normal" if is_tor else "disabled")
                except tk.TclError:
                    pass
        for w in self.proxy_frame.winfo_children():
            if isinstance(w, (ttk.Entry, ttk.Button, ttk.Checkbutton)):
                try:
                    w.configure(state="disabled" if is_tor else "normal")
                except tk.TclError:
                    pass
        self._toggle_proxy_source()

    def _toggle_proxy_source(self):
        """Enable/disable the proxy file path entry based on checkbox."""
        use_default = self.proxy_default_var.get()
        state = "disabled" if use_default else "normal"
        try:
            self.proxy_path_entry.configure(state=state)
            self.proxy_browse_btn.configure(state=state)
        except tk.TclError:
            pass

    def _browse_proxy(self):
        path = filedialog.askopenfilename(
            title="Select Proxy File",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.proxy_path_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(
            title="Select Download Directory"
        )
        if path:
            self.output_dir_var.set(path)

    def _test_tor(self):
        """Test Tor connectivity.  Auto-start / install if needed."""
        self.tor_check_btn["state"] = "disabled"
        self.tor_check_btn["text"] = "Setting up Tor..."
        self.settings_status["text"] = "Checking Tor..."

        def test():
            socks_port = int(self.tor_socks_var.get())
            ctrl_port = int(self.tor_ctrl_var.get())

            if detect_tor():
                # Check ControlPort and offer to enable it
                if not control_port_reachable(ctrl_port):
                    def _prompt_enable():
                        do_enable = messagebox.askyesno(
                            "Enable ControlPort?",
                            "Tor SOCKS is working but the ControlPort is not reachable.\n"
                            "IP rotation requires the ControlPort to be enabled.\n\n"
                            "Enable it now? (requires sudo password)",
                        )
                        if do_enable:
                            def _run_enable():
                                ok = enable_control_port_and_fix_cookie(
                                    port=ctrl_port,
                                    socks_port=socks_port,
                                )
                                self.root.after(0, lambda: self._on_tor_test(ok))
                            threading.Thread(target=_run_enable, daemon=True).start()
                        else:
                            self.root.after(0, lambda: self._on_tor_test(True))
                    self.root.after(0, _prompt_enable)
                    return
                self.root.after(0, lambda: self._on_tor_test(True))
                return

            # Try to install & start Tor automatically
            result = ensure_tor_running(
                socks_port=socks_port,
                control_port=ctrl_port,
                interactive_install=False,
            )
            self.root.after(0, lambda: self._on_tor_test(result))

        threading.Thread(target=test, daemon=True).start()

    def _on_tor_test(self, success: bool):
        self.tor_check_btn["state"] = "normal"
        if success:
            self.tor_check_btn["text"] = "Tor is working!"
            self.settings_status["text"] = "Tor detected and working."
            self.settings_status["foreground"] = SUCCESS
        else:
            self.tor_check_btn["text"] = "Tor not available"
            self.settings_status["text"] = (
                "Tor not reachable.  Install it manually:\n"
                "  brew install tor"
            )
            self.settings_status["foreground"] = ERROR

    def _on_tor_test_error(self, error: str):
        self.tor_check_btn["state"] = "normal"
        self.tor_check_btn["text"] = "Error"
        self.settings_status["text"] = f"Tor test error: {error}"

    def _save_settings(self):
        """Save current settings to config file."""
        try:
            self.config["mode"] = self.mode_var.get()
            self.config["tor_socks_port"] = int(self.tor_socks_var.get())
            self.config["tor_control_port"] = int(self.tor_ctrl_var.get())
            self.config["proxy_file"] = self.proxy_path_var.get()
            self.config["proxy_use_default"] = self.proxy_default_var.get()
            self.config["output_dir"] = self.output_dir_var.get()
            self.config["workers"] = int(self.workers_var.get())
            self.config["download_format"] = self.format_var.get()
            save_config(self.config)
            self.settings_status["text"] = "Settings saved successfully!"
            self.settings_status["foreground"] = SUCCESS
        except ValueError as e:
            self.settings_status["text"] = f"Invalid value: {e}"
            self.settings_status["foreground"] = ERROR

    def _open_output(self):
        """Open the output directory in file manager."""
        path = self.config.get("output_dir", "downloads")
        if os.path.isdir(path):
            if sys.platform == "darwin":
                os.system(f"open '{path}'")
            elif sys.platform == "win32":
                os.startfile(path)
            else:
                os.system(f"xdg-open '{path}'")

    # =======================================================================
    # Lifecycle
    # =======================================================================

    def _on_close(self):
        """Save window geometry, stop Tor if we started it, and exit."""
        self._stop_flag = True
        # Wait briefly for downloads to stop
        time.sleep(0.3)
        stop_managed_tor()
        try:
            self.config["window_geometry"] = self.root.geometry()
            save_config(self.config)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        """Start the GUI main loop."""
        self.root.mainloop()


def main():
    """Launch the GUI."""
    app = VimmBulkGUI()
    app.run()


if __name__ == "__main__":
    main()
