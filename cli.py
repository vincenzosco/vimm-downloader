"""
cli.py - Command-line interface for vimm-bulk-downloader.

Usage examples:

  # Download games (positional URLs — backward compatible):
  python -m vimm_bulk_downloader https://vimm.net/vault/9663 https://vimm.net/vault/70794

  # Download via subcommand:
  python -m vimm_bulk_downloader download https://vimm.net/vault/9663

  # Search for games:
  python -m vimm_bulk_downloader search n64 "Mario Kart"

  # Search all consoles:
  python -m vimm_bulk_downloader search all "Sonic"

  # List available consoles:
  python -m vimm_bulk_downloader consoles
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

from . import __version__
from .downloader import download_all, DEFAULT_MAX_WORKERS, DEFAULT_OUTPUT_DIR
from .ip_rotator import create_rotator, detect_tor
from .vimm_scraper import validate_vault_url
from .console_list import CONSOLE_TABLE
from .vimm_search import search_vimm, render_results

colorama_init(autoreset=True)

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "Download multiple games from vimm.net concurrently, "
    "bypassing the 1-download-per-IP limit using IP rotation "
    "(Tor or proxy pool).  Also supports searching the vault."
)


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="[%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _add_shared_download_args(parser):
    """Add --mode, --proxy-file, --tor-*, --output, --workers, --no-primary."""
    parser.add_argument(
        "--mode", "-m",
        choices=["tor", "proxy"],
        default="tor",
        help="IP rotation backend (default: tor)",
    )
    parser.add_argument(
        "--proxy-file", "-p",
        metavar="FILE",
        help="Path to proxy list file (mode=proxy)",
    )
    parser.add_argument(
        "--tor-socks-port", type=int, default=9050,
        help="Tor SOCKS5 proxy port (default: 9050)",
    )
    parser.add_argument(
        "--tor-control-port", type=int, default=9051,
        help="Tor control port (default: 9051)",
    )
    parser.add_argument(
        "--tor-password", default=None,
        help="Tor control password",
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=f"Output directory (default: '{DEFAULT_OUTPUT_DIR}')",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int, default=DEFAULT_MAX_WORKERS,
        help=f"Max concurrent downloads (default: {DEFAULT_MAX_WORKERS})",
    )
    parser.add_argument(
        "--no-primary",
        action="store_true",
        dest="no_primary",
        help="Don't rewrite download2.vimm.net → download.vimm.net",
    )


def _ensure_rotator(args: dict):
    """Build and return an IPRotator from parsed args.  Exits on error."""
    try:
        rotator = create_rotator(
            mode=args["mode"],
            proxy_file=args.get("proxy_file"),
            tor_socks_port=args["tor_socks_port"],
            tor_control_port=args["tor_control_port"],
            tor_password=args.get("tor_password"),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"{Fore.RED}Error: {e}{Style.RESET_ALL}")
        sys.exit(1)
    return rotator


def _check_tor(args: dict):
    """If mode=tor, test connectivity and optionally prompt to continue."""
    if args["mode"] != "tor":
        return
    print(f"{Fore.CYAN}Checking Tor connectivity ...{Style.RESET_ALL}")
    if detect_tor():
        print(f"  {Fore.GREEN}✓ Tor detected{Style.RESET_ALL}")
    else:
        print(
            f"  {Fore.YELLOW}⚠ Tor not detected on localhost:9050.{Style.RESET_ALL}\n"
            f"    Make sure the Tor daemon is running with:\n"
            f"      • SOCKS5 on port {args['tor_socks_port']}\n"
            f"      • ControlPort on {args['tor_control_port']}\n"
            f"    Install: brew install tor  or  apt install tor"
        )
        proceed = input("    Continue anyway? [Y/n]: ").strip().lower()
        if proceed == "n":
            sys.exit(1)


def _print_banner():
    print(
        f"{Fore.CYAN}╔══════════════════════════════════════════╗\n"
        f"║   Vimm Bulk Downloader v{__version__}           ║\n"
        f"╚══════════════════════════════════════════╝{Style.RESET_ALL}"
    )


# ---------------------------------------------------------------------------
# Backward-compatible download (when no subcommand is given)
# ---------------------------------------------------------------------------

def _download_from_flat_args(argv: list[str]) -> dict:
    """Parse legacy flat args (no subcommand). Returns parsed dict."""
    import argparse

    parser = argparse.ArgumentParser(prog="vimm-bulk-downloader", description=_DESCRIPTION)
    url_group = parser.add_mutually_exclusive_group(required=True)
    url_group.add_argument(
        "urls",
        nargs="*",
        metavar="VAULT_URL",
        help="One or more vimm.net vault URLs",
    )
    url_group.add_argument("--url-file", "-f", metavar="FILE", dest="url_file")
    _add_shared_download_args(parser)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parsed = parser.parse_args(argv)
    return vars(parsed)


def _run_download(args: dict):
    """Execute download with parsed arguments."""
    vault_urls: list[str] = []
    if args.get("urls"):
        vault_urls = args["urls"]
    elif args.get("url_file"):
        vault_urls = _read_urls_from_file(args["url_file"])

    valid_urls = [u for u in vault_urls if validate_vault_url(u)]
    invalid_urls = [u for u in vault_urls if not validate_vault_url(u)]
    if invalid_urls:
        print(
            f"{Fore.YELLOW}Warning: skipping {len(invalid_urls)} invalid URL(s):"
            f"{Style.RESET_ALL}"
        )
        for u in invalid_urls:
            print(f"  {Fore.YELLOW}✗{Style.RESET_ALL} {u}")

    if not valid_urls:
        print(f"{Fore.RED}Error: no valid vault URLs provided.{Style.RESET_ALL}")
        sys.exit(1)

    _print_banner()
    print(f"\n{Fore.WHITE}Games to download:{Style.RESET_ALL} {len(valid_urls)}")

    _check_tor(args)
    rotator = _ensure_rotator(args)
    print(f"  IP rotator: {rotator.name()}")

    download_all(
        vault_urls=valid_urls,
        rotator=rotator,
        output_dir=args["output"],
        max_workers=args["workers"],
        prefer_primary=not args["no_primary"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_urls_from_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        print(f"{Fore.RED}Error: URL file not found: {path}{Style.RESET_ALL}")
        sys.exit(1)
    urls: list[str] = []
    with open(p) as fh:
        for line in fh:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(stripped)
    return urls


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate handler."""
    args_list = argv or sys.argv[1:]

    if not args_list:
        print("Usage: vimm-bulk-downloader [download|search|consoles] ...")
        print(f"  {Fore.CYAN}download{Style.RESET_ALL}  Download games from vault URLs")
        print(f"  {Fore.CYAN}search{Style.RESET_ALL}    Search the vault for games")
        print(f"  {Fore.CYAN}consoles{Style.RESET_ALL}  List available consoles")
        print(f"\n  Try: python -m vimm_bulk_downloader search n64 \"Mario Kart\"")
        sys.exit(0)

    first = args_list[0]

    # --- Subcommand: search ---
    if first == "search" and len(args_list) >= 3:
        _do_search(args_list[1:])
        return

    # --- Subcommand: consoles ---
    if first == "consoles":
        _do_list_consoles()
        return

    # --- Subcommand: download (explicit) ---
    if first == "download":
        if len(args_list) < 2:
            print(f"{Fore.RED}Error: download requires at least one vault URL.{Style.RESET_ALL}")
            sys.exit(1)
        args = _download_from_flat_args(args_list[1:])
        setup_logging(args.get("verbose", False))
        _run_download(args)
        return

    # --- Backward-compatible: first arg is a vault URL ---
    if validate_vault_url(first) or first.startswith("https://vimm.net"):
        args = _download_from_flat_args(args_list)
        setup_logging(args.get("verbose", False))
        _run_download(args)
        return

    # --- Subcommand: gui ---
    if first == "gui":
        _do_gui()
        return

    # --- Unknown ---
    # Maybe it's a search with just "search <query>" (no console)?
    if first == "search":
        print(
            f"{Fore.YELLOW}Usage:{Style.RESET_ALL} "
            f"python -m vimm_bulk_downloader search <console> <query>"
        )
        print(f"  Example: python -m vimm_bulk_downloader search n64 \"Mario Kart\"")
        print(f"  See consoles: python -m vimm_bulk_downloader consoles")
        sys.exit(0)

    # If nothing matched, show error
    print(f"{Fore.RED}Unknown command or invalid URL: {first}{Style.RESET_ALL}")
    print(f"  Usage: python -m vimm_bulk_downloader [download|search|consoles|gui] ...")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _do_search(args: list[str]):
    """Handle ``search <console> <query> [--verbose]``."""
    console_name = args[0]
    query_parts = []
    verbose = False
    for a in args[1:]:
        if a == "--verbose" or a == "-v":
            verbose = True
        else:
            query_parts.append(a)
    query = " ".join(query_parts).strip()

    if not query:
        print(f"{Fore.RED}Error: search query is empty.{Style.RESET_ALL}")
        sys.exit(1)

    setup_logging(verbose)
    from rich.console import Console
    console = Console()

    print()
    console.rule("[bold cyan]🔎 Vimm\'s Lair — Search[/]")
    console.print(f"  Console: [white]{console_name}[/]")
    console.print(f"  Query:   [white]{query}[/]")
    console.print()

    resp = search_vimm(console_name, query)
    render_results(resp, console)


# ---------------------------------------------------------------------------
# Console list
# ---------------------------------------------------------------------------

def _do_list_consoles():
    """Handle ``consoles`` subcommand."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print()
    console.rule("[bold cyan]🎮 Available Consoles[/]")
    console.print()

    table = Table(box=None, padding=(0, 2))
    table.add_column("Name / Alias", style="bold")
    table.add_column("Code", style="cyan")

    for entry in CONSOLE_TABLE:
        code_display = f'"{entry["Code"]}"' if entry["Code"] else "(empty — all consoles)"
        table.add_row(entry["Platform"], code_display)

    console.print(table)
    console.print(
        "\n[dim]Usage:[/]\n"
        f"  python -m vimm_bulk_downloader search [cyan]<name-or-code>[/] [cyan]\"<query>\"[/]\n"
    )


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _do_gui():
    """Launch the tkinter GUI."""
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
