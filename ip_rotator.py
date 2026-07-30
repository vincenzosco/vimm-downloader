"""
ip_rotator.py - Proxy rotation strategy for bypassing per-IP download limits.

Provides:
  ProxyRotator – Uses a list of HTTP/SOCKS proxies with round-robin rotation.
"""

import os
import time
import random
import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

_rich_console = Console()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Proxifly free-proxy-list CDN URLs (updated every 5 minutes)
# Format: ip:port per line
# Source: https://github.com/proxifly/free-proxy-list
# ---------------------------------------------------------------------------

PROXIFLY_CDN_BASE = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies"

PROXIFLY_URLS = {
    "all":    f"{PROXIFLY_CDN_BASE}/all/data.txt",
    "socks5": f"{PROXIFLY_CDN_BASE}/protocols/socks5/data.txt",
    "socks4": f"{PROXIFLY_CDN_BASE}/protocols/socks4/data.txt",
    "http":   f"{PROXIFLY_CDN_BASE}/protocols/http/data.txt",
    "https":  f"{PROXIFLY_CDN_BASE}/protocols/https/data.txt",
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class IPRotator(ABC):
    """Abstract IP rotation strategy."""

    @abstractmethod
    def get_proxies(self) -> Optional[dict]:
        """Return a requests-compatible proxy dict, or None for direct."""

    @abstractmethod
    def rotate(self) -> bool:
        """Force a new IP address.  Returns True on success."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name."""


# ---------------------------------------------------------------------------
# Proxy health checker
# ---------------------------------------------------------------------------

PROXY_CHECK_URL = "https://httpbin.org/ip"


def check_proxies(
    proxies: list[str],
    test_url: str = PROXY_CHECK_URL,
    timeout: int = 10,
    max_workers: int = 30,
) -> list[str]:
    """Test a list of proxies concurrently and return only the working ones.

    Each proxy is tested by making a GET request to *test_url* through it.
    Proxies that respond successfully within *timeout* seconds are kept.
    Testing is done in parallel with up to *max_workers* threads.

    Returns:
        A new list containing only the proxies that passed the health check.
    """
    if not proxies:
        return []

    total = len(proxies)
    working: list[str] = []
    checked = 0
    lock = threading.Lock()

    progress = Progress(
        TextColumn("[cyan]Checking proxies ...[/]"),
        SpinnerColumn(spinner_name="dots"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        "·",
        TimeElapsedColumn(),
        console=_rich_console,
        transient=True,
    )

    def test_one(proxy: str) -> tuple[str, bool]:
        """Test a single proxy. Returns (proxy, is_working)."""
        try:
            resp = requests.get(
                test_url,
                proxies={"http": proxy, "https": proxy},
                timeout=timeout,
            )
            return proxy, resp.ok
        except Exception:
            return proxy, False

    with progress:
        task_id = progress.add_task("", total=total)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(test_one, p): p for p in proxies}
            # Throttle description refresh to avoid excessive terminal redraws
            last_report = 0
            for future in as_completed(futures):
                proxy, ok = future.result()
                with lock:
                    checked += 1
                    if ok:
                        working.append(proxy)
                    alive = len(working)
                progress.update(task_id, advance=1)
                # Only update description every ~5% to reduce flicker
                if checked - last_report >= max(1, total // 20):
                    last_report = checked
                    progress.update(
                        task_id,
                        description=f"[cyan]{alive} alive, {checked}/{total} tested[/]",
                    )
            # Final description update
            progress.update(
                task_id,
                description=f"[cyan]{len(working)} alive, {total} tested[/]",
            )

    return working


# ---------------------------------------------------------------------------
# Proxy list backend
# ---------------------------------------------------------------------------

class ProxyRotator(IPRotator):
    """Rotate through a user-supplied list of proxies.

    Each call to rotate() advances to the next proxy in the list (round-robin).
    get_proxies() returns the current proxy without advancing.
    Thread-safe for concurrent workers.

    The first rotate() selects proxy[0]; subsequent calls advance to 1, 2, ...
    """

    def __init__(
        self,
        proxy_list: list[str],
    ):
        if not proxy_list:
            raise ValueError("proxy_list must contain at least one proxy")
        self._proxies = list(proxy_list)
        self._index = -1  # first rotate() advances to 0
        self._lock = threading.Lock()
        self._failed_count = 0
        random.shuffle(self._proxies)

    def name(self) -> str:
        return f"Proxy pool ({len(self._proxies)} proxies, round-robin)"

    def get_proxies(self) -> dict:
        """Return the *current* proxy without advancing the index."""
        with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
        return {"http": proxy, "https": proxy}

    def rotate(self) -> bool:
        """Advance to the next proxy in the list (round-robin).

        Workers call this *before* building a session, so the first call
        selects proxy[0], the second selects proxy[1], etc.
        """
        with self._lock:
            self._index += 1
        self._failed_count = 0
        return True

    def mark_failed(self, proxy: str):
        """Note that this proxy failed so we can deprioritise it."""
        self._failed_count += 1
        logger.warning("Proxy marked as failed: %s", proxy)

    @classmethod
    def from_file(cls, path: str, check: bool = True) -> "ProxyRotator":
        """Load proxies from a text file (one proxy per line).

        Format per line: protocol://user:pass@host:port
        Example: socks5://user:pass@127.0.0.1:1080
                 http://127.0.0.1:8080

        Args:
            path: Path to proxy list file.
            check: If True, health-check proxies before returning.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Proxy file not found: {path}")
        with open(path) as fh:
            proxies = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
        if not proxies:
            raise ValueError(f"No proxies found in {path}")
        if check:
            proxies = check_proxies(proxies)
        if not proxies:
            raise ValueError(
                "All proxies from file failed the health check — "
                "no working proxies available.\n"
                "    Try --no-proxy-check to skip health checking, "
                "or check your proxy file."
            )
        return cls(proxy_list=proxies)

    @classmethod
    def from_url(cls, url: str, check: bool = True) -> "ProxyRotator":
        """Fetch proxies from a remote URL (e.g., free proxy API).

        Args:
            url: URL to fetch proxy list from.
            check: If True, health-check proxies before returning.
        """
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            proxies = [
                line.strip()
                for line in resp.text.strip().splitlines()
                if line.strip()
            ]
            proxies = check_proxies(proxies) if check else proxies
            if not proxies:
                raise ValueError(
                    "All fetched proxies failed the health check — "
                    "no working proxies available.\n"
                    "    Try --no-proxy-check to skip health checking, "
                    "or use a different proxy source."
                )
            return cls(proxy_list=proxies)
        except requests.RequestException as e:
            raise ValueError(f"Failed to fetch proxies from {url}: {e}") from e


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_rotator(
    proxy_file: Optional[str] = None,
    proxy_list: Optional[list[str] | str] = None,
    proxy_check: bool = True,
) -> IPRotator:
    """Factory: create the appropriate IP rotator backend.

    Args:
        proxy_file: Path to proxy list file.
        proxy_list: Inline list of proxy strings, or "default" for Proxifly CDN.
        proxy_check: If True, health-check proxies before use.

    Returns:
        An IPRotator instance.
    """
    if isinstance(proxy_list, str) and proxy_list == "default":
        # Fetch fresh proxies from the Proxifly free-proxy-list CDN
        logger.info("Fetching fresh proxies from Proxifly free-proxy-list CDN ...")
        try:
            return ProxyRotator.from_url(PROXIFLY_URLS["socks5"], check=proxy_check)
        except ValueError as e:
            raise ValueError(
                f"Failed to fetch default proxy pool: {e}\n"
                "    Fall back to --proxy-file with a custom list."
            ) from e
    elif isinstance(proxy_list, list):
        proxies = check_proxies(proxy_list) if proxy_check else proxy_list
        if not proxies:
            raise ValueError(
                "All provided proxies failed the health check — "
                "no working proxies available.\n"
                "    Try --no-proxy-check to skip health checking."
            )
        return ProxyRotator(proxy_list=proxies)
    elif proxy_file:
        return ProxyRotator.from_file(proxy_file, check=proxy_check)
    else:
        raise ValueError(
            "proxy mode requires --proxy-list default, "
            "--proxy-file <file>, or --proxy-list <list>"
        )
