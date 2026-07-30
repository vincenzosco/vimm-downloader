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
    TimeRemainingColumn,
    TransferSpeedColumn,
    SpinnerColumn,
)
from rich.table import Table
from rich.text import Text

from .vimm_scraper import (
    extract_download_url,
    VimmScraperError,
)
from .ip_rotator import IPRotator

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 3
DEFAULT_RETRIES = 3
DEFAULT_OUTPUT_DIR = "."

# Semaphore allowing at most 1 direct-connection (own IP) download at a time,
# used as a failsafe when proxy downloads fail.
_DIRECT_FALLBACK_LOCK = threading.Semaphore(1)


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

_PART_SUFFIX = ".vimm_part"


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


def _get_part_path(path: str) -> str:
    """Return the .vimm_part path used for in-progress downloads."""
    return path + _PART_SUFFIX


def _check_resume(session: requests.Session, url: str, part_path: str) -> int:
    """Check if we can resume a partial download.

    Returns the number of bytes already downloaded (0 if no resume possible).
    """
    if not os.path.isfile(part_path) or os.path.getsize(part_path) == 0:
        return 0

    try:
        head = session.head(url, timeout=15)
        accept_ranges = head.headers.get("Accept-Ranges", "")
        content_len = int(head.headers.get("Content-Length", "0") or 0)
        existing = os.path.getsize(part_path)

        if "bytes" not in accept_ranges.lower():
            logger.info("Server does not support Range requests -- starting from scratch")
            os.remove(part_path)
            return 0

        if content_len and existing >= content_len:
            # Already fully downloaded (edge case)
            logger.info("Partial file is already complete -- finishing up")
            return existing

        if content_len and existing > content_len:
            # File changed on server (new one is smaller) -- restart
            logger.info(
                "File changed on server (new size %d < existing %d) -- restarting",
                content_len, existing,
            )
            os.remove(part_path)
            return 0

        logger.info(
            "Resuming download: %d of %d bytes already written",
            existing, content_len or 0,
        )
        return existing
    except Exception as exc:
        logger.warning("Resume check failed (%s) -- starting from scratch", exc)
        return 0


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
    resume_bytes: int = 0,
) -> tuple[bool, int]:
    """Stream a file, updating the Rich progress bar along the way.

    Supports resuming interrupted downloads via the ``Range`` HTTP header
    when *resume_bytes* > 0.  The file is opened in append mode and we
    request only the remaining bytes from the server.

    Returns (success, bytes_written).
    """
    try:
        headers = {}
        if resume_bytes > 0:
            # Part of the file already exists -- request remaining bytes
            headers["Range"] = f"bytes={resume_bytes}-"

        resp = session.get(download_url, stream=True, timeout=60, headers=headers)
        resp.raise_for_status()

        # Determine total size: from Content-Length (full) or Content-Range (resumed)
        total = None
        if resume_bytes > 0 and "Content-Range" in resp.headers:
            # Parse "bytes X-Y/TOTAL" from Content-Range
            cr = resp.headers["Content-Range"]
            try:
                total = int(cr.split("/")[-1])
            except (ValueError, IndexError):
                total = None
        else:
            total = int(resp.headers.get("Content-Length", "0")) or None

        written = 0
        mode = "ab" if resume_bytes > 0 else "wb"

        if total:
            progress.update(task_id, total=total)
            if resume_bytes > 0:
                # Advance the bar to show what was already downloaded
                progress.update(task_id, completed=resume_bytes)

        with open(output_path, mode) as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    progress.update(task_id, advance=len(chunk))

        total_written = resume_bytes + written
        progress.update(task_id, completed=total_written, total=total_written)
        return (True, written)

    except Exception as exc:
        logger.error("Stream download failed for %s: %s", download_url, exc)
        # Only delete the file if we weren't resuming -- keep partials for next time
        if resume_bytes == 0 and os.path.isfile(output_path):
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
    max_retries: int = DEFAULT_RETRIES,
) -> DownloadResult:
    """Run by a single worker thread -- rotate IP, resolve filename, download.

    If a download fails, it is automatically retried with a fresh proxy up to
    *max_retries* times.  Each retry rotates to the next proxy in the pool.

    Updates the Rich progress task throughout.
    """
    start = time.time()
    vault_id = job.vault_url.rstrip("/").split("/")[-1]

    # --- Resolve output filename (once, stays the same across retries) ---
    session = _make_session(rotator)
    output_path = _resolve_filename(session, job.download_url, vault_id, job.output_dir)
    session.close()
    part_path = _get_part_path(output_path)

    success = False
    bytes_written = 0
    final_session = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            # Show retry status on progress bar
            progress.update(
                task_id,
                description=f"[yellow](RETRY {attempt}/{max_retries})[/] {job.short_name}",
            )

        # --- Rotate IP for this attempt ---
        progress.update(task_id, description=f"(ROTATE) {job.short_name}")
        rotator.rotate()

        # --- Create session with current proxy ---
        session = _make_session(rotator)
        proxies = rotator.get_proxies()
        proxy_str = proxies.get("http", "direct") if proxies else "direct"

        # Shorten proxy string for display
        display_proxy = proxy_str
        if "@" in proxy_str:
            display_proxy = proxy_str.split("@")[-1]
        if len(display_proxy) > 40:
            display_proxy = display_proxy[:37] + "..."

        progress.update(
            task_id,
            description=f"[cyan](DL)[/] {job.short_name} [[dim]{display_proxy}[/]]",
        )

        # --- Check for partial download to resume ---
        resume_bytes = _check_resume(session, job.download_url, part_path)
        if resume_bytes > 0:
            progress.update(
                task_id,
                description=f"[yellow](RESUME)[/] {job.short_name} ({_format_size(resume_bytes)} already downloaded)[/]",
            )
            logger.info("Resuming %s: %d bytes already on disk", job.short_name, resume_bytes)

        # --- Download with real-time progress (to .part file) ---
        success, bytes_written = _stream_download(
            session, job.download_url, part_path, progress, task_id,
            resume_bytes=resume_bytes,
        )

        if success:
            final_session = session
            break

        # --- Failsafe: if proxy download failed, retry with direct connection ---
        if proxies:
            # Only one direct download at a time (vimm's 1-per-IP limit)
            actual_bytes = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
            logger.info(
                "Proxy download failed for %s -- retrying with direct connection "
                "(%d bytes on disk)",
                job.short_name, actual_bytes,
            )
            progress.update(
                task_id,
                description=f"[yellow](FALLBACK direct)[/] {job.short_name} [{_format_size(actual_bytes)} on disk]",
            )
            _DIRECT_FALLBACK_LOCK.acquire()  # blocks until slot is free
            direct_session = None
            try:
                direct_session = requests.Session()
                direct_session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": "https://vimm.net/vault/",
                    "Cookie": "counted=1",
                })
                progress.update(
                    task_id,
                    description=f"[yellow](FALLBACK direct)[/] {job.short_name} [[dim]direct[/]]",
                )
                retry_success, fallback_bytes = _stream_download(
                    direct_session, job.download_url, part_path, progress, task_id,
                    resume_bytes=actual_bytes,
                )
                if retry_success:
                    success = True
                    bytes_written = fallback_bytes
                    final_session = session
                    logger.info("Direct fallback succeeded for %s", job.short_name)
            finally:
                _DIRECT_FALLBACK_LOCK.release()
                if direct_session is not None:
                    direct_session.close()

        if success:
            break

        # --- Not successful yet, close session and retry with a different proxy ---
        session.close()
        if attempt < max_retries:
            logger.info(
                "Retry %d/%d for %s with a different proxy...",
                attempt, max_retries, job.short_name,
            )

    elapsed = time.time() - start

    if success and final_session is not None:
        # Rename .part to final filename
        try:
            os.rename(part_path, output_path)
        except OSError as exc:
            logger.warning("Could not rename .part file: %s", exc)
            output_path = part_path  # fall back to .part name

        progress.update(
            task_id,
            description=f"[green](OK)[/] {os.path.basename(output_path)}",
        )
        final_session.close()
    else:
        progress.update(
            task_id,
            description=f"[red](ERR) {job.short_name} -- download failed[/]",
        )
        # Keep the .part file on disk for future resume attempts

    progress.stop_task(task_id)

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
    """Create the Rich Progress instance with our custom columns.

    Columns: description, spinner, bar, percentage, speed, ETA, elapsed.
    """
    return Progress(
        TextColumn("[progress.description]{task.description}", justify="left"),
        SpinnerColumn(spinner_name="dots", style="grey50"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%", style="green"),
        "·",
        TransferSpeedColumn(),
        "·",
        TimeRemainingColumn(),
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
    max_retries: int = DEFAULT_RETRIES,
) -> list[DownloadResult]:
    """Scrape & download multiple games from vimm.net concurrently.

    Each worker uses a fresh IP (via the rotator) so that vimm's
    1-download-per-IP limit is bypassed.

    Args:
        vault_urls: List of vault page URLs.
        rotator: IPRotator backend (proxy pool).
        output_dir: Where to save downloaded files.
        max_workers: How many concurrent downloads.
        prefer_primary: Use download.vimm.net instead of download2.vimm.net.
        max_retries: How many times to retry a failed download with a fresh proxy.

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

    for idx, vault_url in enumerate(vault_urls, 1):
        ok = False
        for attempt in range(1, max_retries + 1):
            # Rotate to a (new) proxy before each attempt
            rotator.rotate()

            if attempt == 1:
                console.print(
                    f"  [{idx}/{len(vault_urls)}] [dim]{vault_url}[/]",
                    end=" " * max(0, 4 - len(str(idx))),
                )
            else:
                console.print(
                    f"  [{idx}/{len(vault_urls)}] [yellow]RETRY {attempt}/{max_retries}[/] [dim]{vault_url}[/]",
                    end="",
                )

            # Fresh session per attempt -- stale state from a failed proxy
            # (bad cookie, connection hang) won't carry over
            scrape_session = requests.Session()
            try:
                dl_url = extract_download_url(
                    vault_url,
                    session=scrape_session,
                    proxies=rotator.get_proxies(),
                    prefer_primary=prefer_primary,
                )
                download_urls.append((vault_url, dl_url))
                console.print("[green]OK[/]")
                ok = True
                break
            except VimmScraperError as e:
                if attempt == max_retries:
                    console.print(f"[red]ERR ({max_retries} attempts) {e}[/]")
                else:
                    console.print(f"[red]FAIL attempt {attempt} — retrying...[/]")
            finally:
                scrape_session.close()

        if not ok:
            # Already printed ERR above; nothing more to do
            pass

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

    effective_workers = max_workers

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
        result = _worker_task(job, rotator, progress, task_id, max_retries=max_retries)
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

        # Submit workers -- each picks up its task by index
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
            "--",
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
