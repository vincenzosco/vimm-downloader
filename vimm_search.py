"""
vimm_search.py - Search vimm.net's vault for games by console + query.

Uses the same search URL format as the Vimmdl tool:
    https://vimm.net/vault/?p=list&system={console_code}&q={query}

Returns structured results and renders them as a Rich table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .console_list import resolve_console

logger = logging.getLogger(__name__)

#: Browser-like headers reused from scraper
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "DNT": "1",
    "Referer": "https://vimm.net/vault/",
    "Cookie": "counted=1",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """A single game found in search results."""

    title: str
    vault_url: str
    system: str = ""
    region: str = ""
    version: str = ""
    languages: str = ""
    rating: str = ""


@dataclass
class SearchResponse:
    query: str
    console_name: str
    console_code: str
    total: int
    results: list[SearchResult] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------

def search_vimm(
    console_name: str,
    query: str,
    session: Optional[requests.Session] = None,
    proxies: Optional[dict] = None,
) -> SearchResponse:
    """Search vimm.net's vault for games matching *query* on *console_name*.

    Args:
        console_name: User-friendly name like ``"n64"`` or ``"playstation-2"``.
        query: Search terms (e.g. ``"Mario Kart"``).
        session: Optional shared requests.Session.
        proxies: Optional proxy dict.

    Returns:
        A SearchResponse with parsed results.
    """
    code = resolve_console(console_name)
    if code is None and console_name:
        # Maybe they passed the raw code – use it anyway
        code = console_name

    sess = session or requests.Session()
    close = session is None

    search_url = f"https://vimm.net/vault/?p=list&system={code or ''}&q={query}"

    try:
        logger.info("Searching vimm.net: %s", search_url)
        resp = sess.get(search_url, headers=HEADERS, proxies=proxies, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        if close:
            sess.close()
        return SearchResponse(
            query=query,
            console_name=console_name,
            console_code=code or "",
            total=0,
            error=str(e),
        )
    finally:
        if close:
            sess.close()

    results = _parse_results(resp.text, code)
    return SearchResponse(
        query=query,
        console_name=console_name,
        console_code=code or "",
        total=len(results),
        results=results,
    )


def _parse_results(html: str, code: Optional[str]) -> list[SearchResult]:
    """Parse search result HTML into structured SearchResult objects.

    When code is empty/falsy (search all consoles), the table has columns:
        System | Title | Region | Version | (Link)
    When a console is specified, the table has columns:
        Title | Region | Version | Languages | Rating | (Link)
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "rounded"})
    if table is None:
        return []

    rows = table.find_all("tr")[1:]  # skip header
    results: list[SearchResult] = []

    for tr in rows:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        if not code:  # All-consoles search: cells[1] = Title
            href, title = _visible_link_href(cells[1])
        else:  # Single-console search: cells[0] = Title
            href, title = _visible_link_href(cells[0])

        vault_url = f"https://vimm.net{href}" if href.startswith("/") else href

        # Region from <img> titles
        region_cell = cells[2] if not code else cells[1]
        region_parts: list[str] = []
        for img in region_cell.find_all("img"):
            alt = img.get("title", "") or img.get("alt", "")
            if alt:
                region_parts.append(alt)
        region = " ".join(region_parts)

        if not code:  # All-consoles search → System | Title | Region | Version
            system = cells[0].get_text(strip=True)
            version = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            results.append(SearchResult(
                title=title,
                vault_url=vault_url,
                system=system,
                region=region,
                version=version,
            ))
        else:  # Single-console search → Title | Region | Version | Languages | Rating
            version = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            languages = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            rating = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            results.append(SearchResult(
                title=title,
                vault_url=vault_url,
                system="",  # implied by console filter
                region=region,
                version=version,
                languages=languages,
                rating=rating,
            ))

    return results


def _visible_link_href(cell) -> tuple:
    """Return (href, title_text) from the first visible <a> in *cell*.

    vimm.net prepends a hidden <a> with rating text (e.g. "9")
    style="display:none" before the real game link.  This helper
    skips those hidden anchors.
    """
    for a in cell.find_all("a"):
        style = (a.get("style") or "").lower()
        if "display:none" in style or "display: none" in style:
            continue
        return a.get("href", ""), a.get_text(strip=True)
    # Fallback — if all links were hidden, use the last one
    all_links = cell.find_all("a")
    if all_links:
        a = all_links[-1]
        return a.get("href", ""), a.get_text(strip=True)
    return "", "—"


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------

def render_results(resp: SearchResponse, console: Console) -> None:
    """Print search results to the given Rich console."""
    if resp.error:
        console.print(f"\n[red]✗ Search failed:[/] {resp.error}")
        return

    if resp.total == 0:
        console.print(
            f"\n[yellow]No results[/] for [cyan]'{resp.query}'[/] "
            f"on [cyan]{resp.console_name or 'all consoles'}[/]."
        )
        return

    # Title
    console.print()
    console.rule(
        f"[bold cyan]🔍 Results for[/] "
        f"[white]'{resp.query}'[/] "
        f"on [white]{resp.console_name or 'all consoles'}[/] "
        f"[dim]({resp.total} found)[/]"
    )
    console.print()

    # Build table
    if not resp.console_code:  # all-consoles
        table = Table(box=None, padding=(0, 1), highlight=True)
        table.add_column("#", style="dim", justify="right", no_wrap=True)
        table.add_column("System", style="cyan")
        table.add_column("Title", style="bold")
        table.add_column("Region")
        table.add_column("Version")

        for i, r in enumerate(resp.results, 1):
            table.add_row(
                str(i),
                r.system,
                _title_cell(r.title, r.vault_url),
                r.region,
                r.version,
            )
    else:  # single-console
        table = Table(box=None, padding=(0, 1), highlight=True)
        table.add_column("#", style="dim", justify="right", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Region")
        table.add_column("Version")
        table.add_column("Languages")
        table.add_column("Rating")

        for i, r in enumerate(resp.results, 1):
            table.add_row(
                str(i),
                _title_cell(r.title, r.vault_url),
                r.region,
                r.version,
                r.languages,
                r.rating,
            )

    console.print(table)
    console.print(
        f"\n[dim]To download a game, use its vault URL:[/]\n"
        f"  python -m vimm_bulk_downloader [underline]https://vimm.net/vault/XXXX[/]\n"
    )


def _title_cell(title: str, url: str) -> Text:
    """Rich Text object for a title with clickable-style formatting."""
    t = Text(title)
    if url:
        t.stylize("underline")
    return t
