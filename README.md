# Vimm Bulk Downloader

Download multiple games from [vimm.net](https://vimm.net) at the same time, bypassing the 1-download-per-IP limit using automatic IP rotation.

## Features

- **Concurrent downloads** -- download up to 40 games at once, each using a different IP address
- **IP rotation backends**:
  - **Tor** (free) -- automatically rotates Tor exit nodes for each download
  - **Proxy list** -- rotates through a pool of HTTP/SOCKS proxies
- **Search** -- search the vault by console and query directly from the CLI
- **GUI mode** -- desktop interface with search, download queue, format selector, and settings management
- **Format selector** -- choose the saved file extension: auto, iso, wbfs, rvz, zip, or 7z
- **Rich progress bars** -- real-time display of download speed, percentage, and ETA (CLI mode)
- **Configurable** -- control concurrency (up to 40), output directory, Tor ports, and more

## How it works

1. Provide vault page URLs (e.g., `https://vimm.net/vault/9663`)
2. The tool scrapes each page to find the direct download link (`download.php?mediaId=...`)
3. Before each download, the IP address is rotated via Tor or a proxy pool
4. Downloads run concurrently -- each with a different IP

Since vimm.net limits one download per IP address, using a different IP for each concurrent stream allows downloading several games at once.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

### Tor mode (default)

Install and run Tor on your system:

```bash
# macOS
brew install tor

# Ubuntu / Debian
sudo apt install tor
```

Ensure your `torrc` has ControlPort enabled:

```
ControlPort 9051
CookieAuthentication 1
```

Then start Tor:

```bash
# macOS
brew services start tor

# Linux
sudo systemctl start tor
```

### Proxy mode

Collect a list of HTTP or SOCKS proxies (one per line in a text file). Free proxy lists can be found at:
- [Free Proxy List](https://free-proxy-list.net/)
- [ProxyScrape](https://proxyscrape.com/free-proxy-list)

Note: vimm.net may block datacenter IPs. Residential or SOCKS5 proxies work best.

## Installation

```bash
cd vimm_bulk_downloader
pip install -r requirements.txt
```

## Usage

### Commands

The tool supports several subcommands:

```
python -m vimm_bulk_downloader download [options] <vault-urls>
python -m vimm_bulk_downloader search <console> <query>
python -m vimm_bulk_downloader consoles
python -m vimm_bulk_downloader gui
```

### Download games

```bash
# Download specific games by vault URL
python -m vimm_bulk_downloader download \
    https://vimm.net/vault/9663 \
    https://vimm.net/vault/70794

# Download from a URL list file
python -m vimm_bulk_downloader download --url-file games.txt

# Use a proxy list instead of Tor
python -m vimm_bulk_downloader download \
    --mode proxy \
    --proxy-file proxies.txt \
    --url-file games.txt

# Control concurrency (up to 40 workers in GUI, default 3)
python -m vimm_bulk_downloader download \
    --workers 5 \
    --url-file games.txt

# Save to a specific folder
python -m vimm_bulk_downloader download \
    --output ./roms \
    --url-file games.txt
```

### Search for games

```bash
# Search all consoles
python -m vimm_bulk_downloader search all "Mario Kart"

# Search a specific console
python -m vimm_bulk_downloader search n64 "Zelda"
python -m vimm_bulk_downloader search ps2 "Grand Theft Auto"
python -m vimm_bulk_downloader search genesis "Sonic"

# List available consoles
python -m vimm_bulk_downloader consoles
```

### Launch the GUI

```bash
python -m vimm_bulk_downloader gui
```

The GUI provides three tabs:
- **Search** -- search the vault, browse results, and add games to the download queue
- **Downloads** -- manage the download queue with real-time progress bars and a format selector (auto/iso/wbfs/rvz/zip/7z) for the saved file extension
- **Settings** -- configure IP rotation, output directory, and concurrency (up to 40 workers)

### Backward-compatible usage

If the first argument is a vault URL, the `download` subcommand can be omitted:

```bash
python -m vimm_bulk_downloader https://vimm.net/vault/9663
```

## CLI Reference

```
usage: vimm-bulk-downloader [download|search|consoles|gui] ...

Download subcommand:
  VAULT_URL              One or more vimm.net vault URLs
  -f, --url-file FILE    Path to a file containing vault URLs (one per line)
  -m, --mode tor|proxy   IP rotation backend (default: tor)
  -p, --proxy-file FILE  Path to proxy list file (mode=proxy)
  --tor-socks-port PORT  Tor SOCKS5 port (default: 9050)
  --tor-control-port PORT  Tor control port (default: 9051)
  --tor-password PWD     Tor control password (optional)
  -o, --output DIR       Output directory (default: current directory)
  -w, --workers N        Max concurrent downloads (default: 3, max: 40 in GUI)
  --no-primary           Don't rewrite download2.vimm.net to download.vimm.net
  -v, --verbose          Show debug logs
  --version              Show version

Search subcommand:
  <console>              Console name or code (use "consoles" to list)
  <query>                Search terms

Consoles subcommand:
  (no arguments)         Lists all available console names and codes

GUI subcommand:
  (no arguments)         Launches the desktop GUI
```

## Tips

- vimm.net is slow -- each download is typically capped at ~500 KB/s. Factor this into your expectations.
- Tor adds additional latency. For better speed, use a proxy pool with `--mode proxy`.
- To run 40 concurrent downloads, you need 40 different proxies in your pool. Tor mode is limited to 1 worker because the NEWNYM signal changes the circuit globally.
- The tool automatically rewrites `download2.vimm.net` to `download.vimm.net` (unless `--no-primary` is passed), which the community has found more reliable.
- Be respectful of vimm.net's limited server resources. This tool is designed for convenience, not mass scraping.

## Project structure

```
vimm_bulk_downloader/
  __init__.py             Package init with version
  __main__.py             Entry point for "python -m"
  cli.py                  Command-line interface with subcommands
  console_list.py         Console name-to-code mappings
  downloader.py           Concurrent download orchestrator with Rich progress
  gui.py                  tkinter desktop GUI
  ip_rotator.py           IP rotation backends (Tor, proxy list)
  vimm_scraper.py         Scrapes vault pages for download URLs
  vimm_search.py          Search engine with Rich table output
  README.md
  requirements.txt
  .gitignore
```

## Disclaimer

This tool is for personal, non-commercial use only. Respect vimm.net's terms of service and server capacity. The authors are not responsible for any misuse.
