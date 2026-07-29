"""
vimm_scraper.py - Scrapes vimm.net vault pages to extract direct download URLs.

Based on reverse-engineering from the Vimmdl tool (devvratmiglani/Vimmdl).
A vault page like https://vimm.net/vault/XXXX contains a form with id='dl_form'
whose action points to the download endpoint (e.g., //download.vimm.net/download.php)
and an input containing the mediaId.
"""

import re
import time
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Browser-like headers to avoid being blocked
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Referer": "https://vimm.net/vault/",
    "Cookie": "counted=1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

VAULT_URL_PATTERN = re.compile(r"^https://vimm\.net/vault/\d+$")


class VimmScraperError(Exception):
    """Raised when scraping a vimm.net vault page fails."""


def validate_vault_url(url: str) -> bool:
    """Check if a URL looks like a valid vimm.net vault page."""
    return bool(VAULT_URL_PATTERN.match(url))


def extract_download_url(
    vault_url: str,
    session: Optional[requests.Session] = None,
    proxies: Optional[dict] = None,
    prefer_primary: bool = True,
) -> str:
    """Visit a vimm.net vault page and extract the direct download URL.

    Args:
        vault_url: Full URL to a vault page, e.g. https://vimm.net/vault/9663
        session: Optional requests.Session to reuse.
        proxies: Optional proxy dict for requests.
        prefer_primary: If True, rewrite 'download2.vimm.net' -> 'download.vimm.net'
                        (community workaround for faster/safer downloads).

    Returns:
        The resolved download URL.

    Raises:
        VimmScraperError: If the page cannot be fetched or parsed.
    """
    if not validate_vault_url(vault_url):
        raise VimmScraperError(f"Invalid vault URL: {vault_url}")

    sess = session or requests.Session()
    close_session = session is None

    try:
        logger.info("Fetching vault page: %s", vault_url)
        resp = sess.get(
            vault_url,
            headers=HEADERS,
            proxies=proxies,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise VimmScraperError(f"Failed to fetch {vault_url}: {e}") from e
    finally:
        if close_session:
            sess.close()

    soup = BeautifulSoup(resp.text, "html.parser")
    dl_form = soup.find("form", {"id": "dl_form"})

    if dl_form is None:
        raise VimmScraperError(
            f"Could not find download form (#dl_form) on {vault_url} "
            f"— the game may not be available or the page structure changed."
        )

    # Check the surrounding parent container for any status messages
    # about download availability.
    parent = dl_form.parent
    if parent is not None:
        # Skip comment nodes when checking text
        status_text = parent.get_text(strip=True) if parent else ""
        # The form itself can contain action text we don't want to flag.
        # Only warn if the parent text is notably longer than the form's own text.
        form_text = dl_form.get_text(strip=True)
        extra_text = status_text.replace(form_text, "").strip()
        if extra_text:
            unavailable_keywords = [
                "not available",
                "temporarily",
                "offline",
                "removed",
            ]
            if any(kw in extra_text.lower() for kw in unavailable_keywords):
                logger.warning("Download may be unavailable: %s", extra_text)

    action = dl_form.get("action", "")
    if not action:
        raise VimmScraperError(f"Download form has no 'action' attribute on {vault_url}")

    media_input = dl_form.find("input", {"name": "mediaId"})
    if media_input is None:
        # Try any input with a value
        media_input = dl_form.find("input")
    if media_input is None:
        raise VimmScraperError(f"Download form has no input element on {vault_url}")

    media_id = media_input.get("value", "")
    if not media_id:
        raise VimmScraperError(f"Could not extract mediaId from form on {vault_url}")

    # Construct the full download URL
    # action is typically protocol-relative: //download.vimm.net/download.php
    if action.startswith("//"):
        download_url = "https:" + action
    elif action.startswith("/"):
        download_url = "https://vimm.net" + action
    elif action.startswith("http"):
        download_url = action
    else:
        download_url = "https://" + action

    # Append mediaId as query parameter
    separator = "&" if "?" in download_url else "?"
    download_url = f"{download_url}{separator}mediaId={media_id}"

    # Community workaround: prefer primary server over download2
    if prefer_primary and "download2." in download_url:
        download_url = download_url.replace("download2.", "download.")
        logger.info("Rewrote download2 -> download (community workaround)")

    logger.info(
        "Extracted download URL: %s  (game page: %s)",
        download_url,
        vault_url,
    )
    return download_url


def extract_download_urls_batch(
    vault_urls: list[str],
    session: Optional[requests.Session] = None,
    proxies: Optional[dict] = None,
    prefer_primary: bool = True,
    delay: float = 1.0,
) -> dict[str, Optional[str]]:
    """Extract download URLs for multiple vault pages.

    Returns a dict mapping vault URL -> download URL (or None on failure).
    """
    sess = session or requests.Session()
    results: dict[str, Optional[str]] = {}

    for url in vault_urls:
        try:
            results[url] = extract_download_url(
                url,
                session=sess,
                proxies=proxies,
                prefer_primary=prefer_primary,
            )
        except VimmScraperError as e:
            logger.error("Failed to extract URL from %s: %s", url, e)
            results[url] = None

        if delay > 0:
            time.sleep(delay)

    if session is None:
        sess.close()

    return results
