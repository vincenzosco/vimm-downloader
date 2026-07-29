"""
ip_rotator.py - IP rotation strategies for bypassing per-IP download limits.

Provides multiple backends:
  1. TorRotator   – Uses the Tor network with Stem to rotate exit nodes.
  2. ProxyRotator – Uses a user-supplied list of HTTP/SOCKS proxies.
"""

import os
import time
import random
import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional

import requests

from .tor_manager import TorManager

logger = logging.getLogger(__name__)


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
# Tor backend
# ---------------------------------------------------------------------------

class TorRotator(IPRotator):
    """Rotate IP address via the Tor network.

    Requires:
      - Tor daemon running with ControlPort enabled (default 9051)
      - SOCKS5 proxy exposed (default port 9050)

    On each rotate() call a NEWNYM signal is sent to Tor's control port,
    building a new circuit and thus a different exit-node IP.
    """

    def __init__(
        self,
        socks_port: int = 9050,
        control_port: int = 9051,
        control_password: Optional[str] = None,
        min_circuit_seconds: int = 10,
    ):
        self.socks_port = socks_port
        self.control_port = control_port
        self.control_password = control_password
        self.min_circuit_seconds = min_circuit_seconds
        self._last_rotate = 0.0

    def name(self) -> str:
        return f"Tor (SOCKS5 :{self.socks_port}, control :{self.control_port})"

    def get_proxies(self) -> dict:
        return {
            "http": f"socks5h://127.0.0.1:{self.socks_port}",
            "https": f"socks5h://127.0.0.1:{self.socks_port}",
        }

    def rotate(self) -> bool:
        """Send NEWNYM signal to Tor control port."""
        try:
            from stem import Signal
            from stem.control import Controller
        except ImportError:
            logger.error(
                "stem library is required for Tor rotation. "
                "Install it: pip install stem"
            )
            return False

        # Respect Tor's minimum circuit lifetime
        elapsed = time.time() - self._last_rotate
        if elapsed < self.min_circuit_seconds:
            wait = self.min_circuit_seconds - elapsed
            logger.info(
                "Waiting %.1f seconds before next Tor circuit change ...", wait
            )
            time.sleep(wait)

        try:
            with Controller.from_port(port=self.control_port) as controller:
                if self.control_password:
                    controller.authenticate(password=self.control_password)
                else:
                    controller.authenticate()  # cookie auth
                controller.signal(Signal.NEWNYM)
                self._last_rotate = time.time()
                logger.info("Tor circuit renewed — new exit-node IP assigned.")
                return True
        except Exception as exc:
            logger.error("Tor NEWNYM failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Proxy list backend
# ---------------------------------------------------------------------------

class ProxyRotator(IPRotator):
    """Rotate through a user-supplied list of proxies.

    Each call to rotate() advances to the next proxy in the list (round-robin).
    get_proxies() returns the current proxy without advancing.
    Thread-safe for concurrent workers.

    The first rotate() selects proxy[0]; subsequent calls advance to 1, 2, …
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
    def from_file(cls, path: str) -> "ProxyRotator":
        """Load proxies from a text file (one proxy per line).

        Format per line: protocol://user:pass@host:port
        Example: socks5://user:pass@127.0.0.1:1080
                 http://127.0.0.1:8080
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Proxy file not found: {path}")
        with open(path) as fh:
            proxies = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
        if not proxies:
            raise ValueError(f"No proxies found in {path}")
        return cls(proxy_list=proxies)

    @classmethod
    def from_url(cls, url: str) -> "ProxyRotator":
        """Fetch proxies from a remote URL (e.g., free proxy API)."""
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            proxies = [
                line.strip()
                for line in resp.text.strip().splitlines()
                if line.strip()
            ]
            return cls(proxy_list=proxies)
        except requests.RequestException as e:
            raise ValueError(f"Failed to fetch proxies from {url}: {e}") from e


# ---------------------------------------------------------------------------
# Factory / auto-detect
# ---------------------------------------------------------------------------

def detect_tor() -> bool:
    """Check if a Tor SOCKS proxy is reachable on the default port."""
    try:
        resp = requests.get(
            "https://check.torproject.org/",
            proxies={
                "http": "socks5h://127.0.0.1:9050",
                "https": "socks5h://127.0.0.1:9050",
            },
            timeout=10,
        )
        return "Congratulations" in resp.text
    except Exception:
        return False


def ensure_tor_running(
    socks_port: int = 9050,
    control_port: int = 9051,
    interactive_install: bool = True,
) -> bool:
    """Convenience: install Tor if needed, start it, and wait until ready.

    Returns True once Tor is running and accepting connections.
    """
    mgr = TorManager(
        socks_port=socks_port,
        control_port=control_port,
    )
    return mgr.ensure_running(interactive_install=interactive_install)


def create_rotator(
    mode: str = "tor",
    proxy_file: Optional[str] = None,
    proxy_list: Optional[list[str]] = None,
    tor_socks_port: int = 9050,
    tor_control_port: int = 9051,
    tor_password: Optional[str] = None,
) -> IPRotator:
    """Factory: create the appropriate IP rotator backend.

    Args:
        mode: 'tor' or 'proxy'
        proxy_file: Path to proxy list file (mode='proxy')
        proxy_list: Inline list of proxy strings (mode='proxy')
        tor_socks_port: Tor SOCKS5 port
        tor_control_port: Tor control port
        tor_password: Tor control password (optional)

    Returns:
        An IPRotator instance.
    """
    if mode == "tor":
        return TorRotator(
            socks_port=tor_socks_port,
            control_port=tor_control_port,
            control_password=tor_password,
        )
    elif mode == "proxy":
        if proxy_list:
            return ProxyRotator(proxy_list=proxy_list)
        elif proxy_file:
            return ProxyRotator.from_file(proxy_file)
        else:
            raise ValueError(
                "proxy mode requires either --proxy-file or --proxy-list"
            )
    else:
        raise ValueError(f"Unknown rotator mode: {mode}")
