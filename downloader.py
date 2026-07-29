"""
downloader.py - Concurrent download orchestrator with IP rotation.

Manages multiple download workers, each using a different IP (via the
configured IPRotator backend), so that vimm.net's 1-download-per-IP
limit is bypassed.

Displays real-time progress bars with Rich for each concurrent download.
"""

import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from colorama import Fore, Style, init as colorama_init

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
    SpinnerColumn,
)
from rich.table import Table
from rich.text import Text

from .vimm_scraper import (
    extract_download_url,
    VimmScraperError,
)
from .ip_rotator import IPRotator, TorRotator

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

# Max simultaneous workers = min(len(proxies), max_workers)
# Tor mode: only 1 worker at a time (since NEWNYM is global)
DEFAULT_MAX_WORKERS = 3
DEFAULT_OUTPUT_DIR = "."


@dataclass
class DownloadResult:
    vault_url: str
    download_url: Optional[str]
    filename: Optional[str]
    success: bool
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    bytes_downloaded: int = 0


@dataclass
class DownloadJob:
    vault_url: str
    download_url: str
    output_dir: str
    index: int
    total: int
    short_name: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_path(path: str) -> str:
    """If *path* exists, append a counter before the extension."""
    p = Path(path)
    if not p.exists():
        return path
    counter = 1
    while True:
        candidate = p.parent / f"{p.stem}_{counter}{p.suffix}"
        if not candidate.exists():
            return str(candidate)
        counter += 1


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Session factory per worker
# ---------------------------------------------------------------------------

def _make_session(rotator: IPRotator) -> requests.Session:
    """Create a requests.Session configured with the given proxy."""
    sess = requests.Session()
    proxies = rotator.get_proxies()
    if proxies:
        sess.proxies.update(proxies)
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://vimm.net/vault/",
        "Cookie": "counted=1",
    })
    return sess


# ---------------------------------------------------------------------------
# Resolve output filename from download URL / Content-Disposition
# ---------------------------------------------------------------------------

KNOWN_EXTENSIONS = frozenset({
    ".zip", ".7z", ".rvz", ".iso", ".bin", ".cue", ".rom",
    ".nsp", ".xci", ".wad", ".gba", ".nds", ".3ds",
    ".smc", ".sfc", ".nes", ".gen", ".md",
    ".gcm", ".gcz", ".wbfs", ".wia", ".chd", ".m3u",
})


def _resolve_filename(
    session: requests.Session,
    download_url: str,
    vault_id: str,
    output_dir: str,
) -> str:
    """Determine a sensible local filename for the download."""
    basename = os.path.basename(download_url)

    # Try Content-Disposition header
    try:
        head = session.head(download_url, timeout=15)
        cd = head.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            fname = cd.split("filename=")[-1].strip('" ').strip("'")
            if fname:
                basename = fname
    except Exception:
        pass

    # Fallback: use vault ID
    if not basename or basename == "download.php":
        basename = f"vimm_vault_{vault_id}.zip"

    return _unique_path(os.path.join(output_dir, basename))


# ---------------------------------------------------------------------------
# Stream download with Rich progress updates
# ---------------------------------------------------------------------------

def _stream_download(
    session: requests.Session,
    download_url: str,
    output_path: str,
    progress: Progress,
    task_id: TaskID,
    chunk_size: int = 64 * 1024,
) -> tuple[bool, int]:
    """Stream a file, updating the Rich progress bar along the way.

    Returns (success, bytes_written).
    """
    try:
        resp = session.get(download_url, stream=True, timeout=60)
        resp.raise_for_status()

        total = int(resp.headers.get("Content-Length", "0")) or None
        written = 0

        # If we know the total, set it on the task so the bar works correctly
        if total:
            progress.update(task_id, total=total)

        with open(output_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    progress.update(task_id, advance=len(chunk))

        # Mark as complete (avoids a stale incomplete bar if total was unknown)
        progress.update(task_id, completed=written or 1, total=written or 1)
        return (True, written)

    except Exception as exc:
        logger.error("Stream download failed for %s: %s", download_url, exc)
        if os.path.isfile(output_path):
            os.remove(output_path)
        return (False, 0)


# ---------------------------------------------------------------------------
# Worker task (runs in a thread)
# ---------------------------------------------------------------------------

def _worker_task(
    job: DownloadJob,
    rotator: IPRotator,
    progress: Progress,
    task_id: TaskID,
) -> DownloadResult:
    """Run by a single worker thread — rotate IP, resolve filename, download.

    Updates the Rich progress task throughout.
    """
    start = time.time()

    # --- Rotate IP before this download ---
    progress.update(task_id, description=f"(ROTATE) {job.short_name}")
    if not rotator.rotate():
        # Warn but continue — the current proxy/IP still works
        progress.update(
            task_id,
            description=f"[yellow]! {job.short_name} — rotation failed, continuing with current IP[/]",
        )
        logger.warning(
            "IP rotation failed for %s — continuing with current proxy",
            job.vault_url,
        )

    # --- Create session ---
    session = _make_session(rotator)
    proxies = rotator.get_proxies()
    proxy_str = proxies.get("http", "direct") if proxies else "direct"

    # Shorten proxy string for display
    display_proxy = proxy_str
    if "@" in proxy_str:
        # Show just host:port, hide credentials
        display_proxy = proxy_str.split("@")[-1]
    if len(display_proxy) > 40:
        display_proxy = display_proxy[:37] + "..."

    progress.update(
        task_id,
        description=f"[cyan](DL)[/] {job.short_name} [[dim]{display_proxy}[/]]",
    )

    # --- Resolve output filename ---
    vault_id = job.vault_url.rstrip("/").split("/")[-1]
    output_path = _resolve_filename(session, job.download_url, vault_id, job.output_dir)

    # --- Download with real-time progress ---
    success, bytes_written = _stream_download(
        session, job.download_url, output_path, progress, task_id,
    )

    elapsed = time.time() - start
    session.close()

    if success:
        progress.update(
            task_id,
            description=f"[green](OK)[/] {os.path.basename(output_path)}",
        )
    else:
        progress.update(
            task_id,
            description=f"[red](ERR) {job.short_name} — download failed[/]",
        )

    progress.stop_task(task_id)  # stop spinner for both success & failure

    return DownloadResult(
        vault_url=job.vault_url,
        download_url=job.download_url,
        filename=output_path if success else None,
        success=success,
        bytes_downloaded=bytes_written,
        elapsed_seconds=elapsed,
        error=None if success else "Download failed",
    )


# ---------------------------------------------------------------------------
# Rich progress columns
# ---------------------------------------------------------------------------

def _make_progress() -> Progress:
    """Create the Rich Progress instance with our custom columns."""
    return Progress(
        TextColumn("[progress.description]{task.description}", justify="left"),
        SpinnerColumn(spinner_name="dots", style="grey50"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%", style="green"),
        "·",
        TransferSpeedColumn(),
        "·",
        TimeElapsedColumn(),
        expand=True,
        transient=False,
    )


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------

def download_all(
    vault_urls: list[str],
    rotator: IPRotator,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    max_workers: int = DEFAULT_MAX_WORKERS,
    prefer_primary: bool = True,
) -> list[DownloadResult]:
    """Scrape & download multiple games from vimm.net concurrently.

    Each worker uses a fresh IP (via the rotator) so that vimm's
    1-download-per-IP limit is bypassed.

    Args:
        vault_urls: List of vault page URLs.
        rotator: IPRotator backend (Tor / proxy pool).
        output_dir: Where to save downloaded files.
        max_workers: How many concurrent downloads (limited by available IPs).
        prefer_primary: Use download.vimm.net instead of download2.vimm.net.

    Returns:
        List of DownloadResult objects.
    """
    console = Console()
    os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Phase 1: Scrape all vault pages for download URLs
    # ------------------------------------------------------------------
    console.print()
    console.rule("[bold cyan]== Phase 1/2: Resolving download URLs[/]")
    console.print()

    download_urls: list[tuple[str, str]] = []  # (vault_url, dl_url)

    scrape_session = requests.Session()
    for idx, vault_url in enumerate(vault_urls, 1):
        console.print(
            f"  [{idx}/{len(vault_urls)}] [dim]{vault_url}[/]",
            end=" " * max(0, 4 - len(str(idx))),
        )
        try:
            dl_url = extract_download_url(
                vault_url,
                session=scrape_session,
                proxies=rotator.get_proxies(),
                prefer_primary=prefer_primary,
            )
            download_urls.append((vault_url, dl_url))
            console.print("[green]OK[/]")
        except VimmScraperError as e:
            console.print(f"[red]ERR {e}[/]")
    scrape_session.close()

    if not download_urls:
        console.print("\n[red]No download URLs could be resolved. Nothing to do.[/]")
        return []

    resolve_count = len(download_urls)
    total_count = len(vault_urls)
    if resolve_count == total_count:
        console.print(f"\n[green]OK All {resolve_count} URLs resolved successfully![/]")
    else:
        console.print(
            f"\n[yellow]Resolved {resolve_count}/{total_count}[/]"
        )

    # ------------------------------------------------------------------
    # Phase 2: Download files concurrently with Rich progress bars
    # ------------------------------------------------------------------
    console.print()
    console.rule("[bold cyan]== Phase 2/2: Downloading with IP rotation[/]")
    console.print()

    # Use the user-configured worker count (no limit for Tor mode)
    effective_workers = max_workers
    if isinstance(rotator, TorRotator) and max_workers > 1:
        console.print(
            "[yellow]! Tor mode: IP rotation is shared across all workers[/]\n"
            "    [dim]Each download will try to rotate Tor's exit node, but since"
            " NEWNYM is global,[/]\n"
            "    [dim]concurrent workers share the same Tor circuit.[/]"
        )

    console.print(f"  [dim]Rotator:[/] {rotator.name()}")
    console.print(f"  [dim]Workers:[/] {effective_workers}")
    console.print(f"  [dim]Output:[/]  [underline]{os.path.abspath(output_dir)}[/]")

    # --- Build job list ---
    jobs: list[DownloadJob] = []
    for idx, (vault_url, dl_url) in enumerate(download_urls):
        vault_id = vault_url.rstrip("/").split("/")[-1]
        jobs.append(DownloadJob(
            vault_url=vault_url,
            download_url=dl_url,
            output_dir=output_dir,
            index=idx + 1,
            total=len(download_urls),
            short_name=f"vault_{vault_id}",
        ))

    results: list[DownloadResult] = []
    results_lock = threading.Lock()

    def task_wrapper(job: DownloadJob, progress: Progress, task_id: TaskID):
        result = _worker_task(job, rotator, progress, task_id)
        with results_lock:
            results.append(result)

    # --- Run with Rich Progress ---
    progress = _make_progress()

    with progress:
        # Add all tasks up front
        task_ids: list[TaskID] = []
        for job in jobs:
            task_id = progress.add_task(
                description=f"[dim]WAIT {job.short_name}[/]",
                total=None,   # unknown until we get Content-Length
            )
            task_ids.append(task_id)

        # Submit workers — each picks up its task by index
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = []
            for job, task_id in zip(jobs, task_ids):
                future = pool.submit(task_wrapper, job, progress, task_id)
                futures.append(future)

            # Wait for all
            for future in futures:
                future.result()

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    console.print()
    console.rule("[bold cyan]== Download Summary[/]")

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    table = Table(box=None, padding=(0, 2))
    table.add_column("Status", no_wrap=True)
    table.add_column("File", style="bold")
    table.add_column("Size", justify="right")
    table.add_column("Time", justify="right")

    for r in successes:
        table.add_row(
            "[green]OK[/]",
            os.path.basename(r.filename or ""),
            _format_size(r.bytes_downloaded),
            f"{r.elapsed_seconds:.1f}s",
        )
    for r in failures:
        table.add_row(
            "[red]ERR[/]",
            r.vault_url,
            "—",
            f"{r.elapsed_seconds:.1f}s",
        )

    # Totals row
    total_bytes = sum(r.bytes_downloaded for r in successes)
    total_time = max(r.elapsed_seconds for r in results) if results else 0
    table.add_row("", "", "", "")  # spacer
    table.add_row(
        f"[bold]{len(successes)}/{len(results)}[/]",
        "[bold]Total[/]" if successes else "",
        f"[bold]{_format_size(total_bytes)}[/]" if successes else "",
        f"[bold]{total_time:.1f}s[/]" if results else "",
    )

    console.print(table)
    console.print(f"  [dim]Output directory:[/] [underline]{os.path.abspath(output_dir)}[/]")
    console.print()

    return results
