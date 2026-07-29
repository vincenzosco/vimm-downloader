# Vimm Bulk Downloader

Download multiple games from [vimm.net](https://vimm.net) at the same time, bypassing the 1-download-per-IP limit using automatic IP rotation.

## Features

- **Concurrent downloads** -- download up to 40 games at once, each using a different IP address
- **IP rotation backends**:
  - **Tor** (free) -- automatically rotates Tor exit nodes for each download
  - **Proxy list** -- rotates through a pool of HTTP/SOCKS proxies
- **Default free proxy pool** -- fetch fresh SOCKS5 proxies from the [Proxifly CDN](https://github.com/proxifly/free-proxy-list) (updated every 5 minutes) with `--proxy-list default`
- **Proxy health-checking** -- each proxy is tested against a live endpoint before use; dead proxies are automatically removed (can be skipped with `--no-proxy-check`)
- **Auto-start Tor** -- automatically starts the Tor daemon when needed (and installs it if missing)
- **Auto-enable ControlPort** -- if Tor is running without a ControlPort, the tool asks whether to enable it automatically with sudo
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
- **Linux users only:** `sudo apt install python3-tk -y` (required for GUI mode -- tkinter is not included by default on Linux)
- `pip install -r requirements.txt` will install: requests, beautifulsoup4, rich, colorama, stem, PySocks

### Tor mode (default) -- auto-managed

Tor is automatically started when you run a download with `--mode tor` (the default).
If Tor is not installed, the tool prompts you to install it (CLI mode) or auto-installs
it via your system package manager (GUI mode).

Supported package managers:
- macOS: `brew install tor`
- Debian/Ubuntu: `apt-get install -y tor`
- Fedora: `dnf install -y tor`
- Arch: `pacman -S --noconfirm tor`
- openSUSE: `zypper install -y tor`

The tool generates a temporary `torrc` configuration with proper SOCKS and Control
ports, and cleans up the Tor process on exit. No manual setup needed.

If Tor is already running via the system service but the ControlPort is disabled
(common on Linux), the tool detects this and asks whether to automatically:
1. Add `ControlPort 9051` and `CookieAuthentication 1` to `/etc/tor/torrc`
2. Restart the Tor service with `sudo systemctl restart tor`

This enables IP rotation without any manual configuration.

To use an existing Tor daemon instead (e.g., running via `brew services` or `systemctl`),
simply make sure it is running before launching the tool. The auto-start logic detects
an already-running Tor and skips startup.

### Proxy mode

You can use proxies in two ways:

**1. Default free proxy pool (recommended):**

The tool can fetch fresh SOCKS5 proxies from the [Proxifly free-proxy-list CDN](https://github.com/proxifly/free-proxy-list), which is updated every 5 minutes with 150+ working proxies from 100+ countries. Each proxy is health-checked automatically before use.

```bash
python -m vimm_bulk_downloader download \
    --mode proxy --proxy-list default \
    --url-file games.txt
```

Short form (``--proxy-list`` without a value defaults to ``default``):

```bash
python -m vimm_bulk_downloader download \
    --mode proxy --proxy-list \
    --url-file games.txt
```

**2. Custom proxy file:**

Collect a list of HTTP or SOCKS proxies (one per line in a text file). Free proxy lists can be found at:
- [Free Proxy List](https://free-proxy-list.net/)
- [ProxyScrape](https://proxyscrape.com/free-proxy-list)

```bash
python -m vimm_bulk_downloader download \
    --mode proxy --proxy-file proxies.txt \
    --url-file games.txt
```

**3. Inline proxy URLs:**

Pass a comma-separated list of proxy URLs directly:

```bash
python -m vimm_bulk_downloader download \
    --mode proxy --proxy-list "socks5://127.0.0.1:1080,http://proxy.example.com:8080" \
    --url-file games.txt
```

Note: vimm.net may block datacenter IPs. Residential or SOCKS5 proxies work best.

### Proxy health-checking

By default, all proxies are tested before use. The tool:
1. Tests each proxy against `https://httpbin.org/ip` (10-second timeout)
2. Runs up to 30 tests in parallel
3. Shows a real-time progress bar: `12 alive, 45/150 tested`
4. Removes dead proxies automatically -- only working ones are kept

To skip health-checking (faster startup, but may use dead proxies):

```bash
python -m vimm_bulk_downloader download \
    --mode proxy --proxy-list default --no-proxy-check \
    --url-file games.txt
```

## Installation

### Run from the project directory (no install):

```bash
cd /path/to/parent  # must be the PARENT of vimm_bulk_downloader/
                   # (python -m vimm_bulk_downloader finds the package by folder name)
python3 -m pip install -r vimm_bulk_downloader/requirements.txt
cd vimm_bulk_downloader
pip install -r requirements.txt
```

**Important:** `python -m vimm_bulk_downloader` must be run from the **parent directory**
of the project folder, not from inside it. If you are inside `vimm_bulk_downloader/`,
python looks for `vimm_bulk_downloader/vimm_bulk_downloader/` which does not exist.

```bash
# Correct -- run from parent directory
cd /path/to/parent/
python -m vimm_bulk_downloader gui

# Wrong -- this will fail with "No module named vimm_bulk_downloader"
cd /path/to/parent/vimm_bulk_downloader/
python -m vimm_bulk_downloader gui
```

### Install as a package (works from anywhere):

```bash
cd /path/to/vimm_bulk_downloader
python3 -m pip install -e . --break-system-packages  # macOS with system Python
pip install -e .                                      # other systems

# Now works from any directory:
vimm-downloader gui
python -m vimm_bulk_downloader gui
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

# Control concurrency (up to 40 workers, default 3)
python -m vimm_bulk_downloader download \
    --workers 5 \
    --url-file games.txt

# Tor mode now allows multiple workers (each download uses the same Tor circuit)
python -m vimm_bulk_downloader download \
    --workers 2 \
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
cd /path/to/parent/  # or install with pip -e . first
python -m vimm_bulk_downloader gui
```

Or if you installed the package:

```bash
vimm-downloader gui
```

The GUI provides three tabs:
- **Search** -- search the vault, browse results, and add games to the download queue
- **Downloads** -- manage the download queue with real-time progress bars and a format selector (auto/iso/wbfs/rvz/zip/7z) for the saved file extension
- **Settings** -- configure IP rotation (Tor auto-start/install, auto-enable ControlPort, proxy file, default proxy pool checkbox, proxy health-check toggle), output directory, and concurrent workers (up to 40)

### Backward-compatible usage

If the first argument is a vault URL, the `download` subcommand can be omitted:

```bash
python -m vimm_bulk_downloader https://vimm.net/vault/9663
```

## CLI Reference

```
usage: vimm-bulk-downloader [download|search|consoles|gui] ...

Download subcommand:
  VAULT_URL                One or more vimm.net vault URLs
  -f, --url-file FILE      Path to a file containing vault URLs (one per line)
  -m, --mode tor|proxy     IP rotation backend (default: tor)
  -p, --proxy-file FILE    Path to proxy list file (mode=proxy)
  --proxy-list SOURCE      Proxy source: "default" to fetch free proxies from
                           Proxifly CDN, or a comma-separated list of proxy
                           URLs (mode=proxy). Use without a value for default.
  --no-proxy-check         Skip proxy health-checking (faster startup, may
                           use dead proxies)
  --tor-socks-port PORT    Tor SOCKS5 port (default: 9050)
  --tor-control-port PORT  Tor control port (default: 9051)
  --tor-password PWD       Tor control password (optional)
  -o, --output DIR         Output directory (default: current directory)
  -w, --workers N          Max concurrent downloads (default: 3, max: 40)
  --no-primary             Don't rewrite download2.vimm.net to download.vimm.net
  -v, --verbose            Show debug logs
  --version                Show version

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
- To run 40 concurrent downloads, you need 40 different proxies in your pool.
- When using Tor mode with multiple workers, each download uses the same Tor circuit (NEWNYM is global). Concurrent downloads still work, but they will share the same exit node IP.
- For true parallel downloads with different IPs, use `--mode proxy` with a pool of proxies.
- The tool automatically rewrites `download2.vimm.net` to `download.vimm.net` (unless `--no-primary` is passed), which the community has found more reliable.
- Tor is auto-started on demand and stopped when the program exits. No need to manage it manually.
- When using the GUI with Tor mode, clicking "Test Tor" in Settings or starting a download will automatically install Tor (if missing) and start it.
- temporary files (torrc, data directory, logs) are cleaned up when the tool exits.
- Be respectful of vimm.net's limited server resources. This tool is designed for convenience, not mass scraping.

## Project structure

```
vimm_bulk_downloader/
  __init__.py             Package init with version
  __main__.py             Entry point for "python -m"
  cli.py                  Command-line interface with subcommands
  console_list.py         Console name-to-code mappings
  downloader.py           Concurrent download orchestrator with Rich progress
  gui.py                  tkinter desktop GUI (search, queue, progress, settings)
  ip_rotator.py           IP rotation backends (Tor, proxy list)
  tor_manager.py          Tor daemon lifecycle (detect, install, start, stop)
  vimm_scraper.py         Scrapes vault pages for download URLs
  vimm_search.py          Search engine with Rich table output
  pyproject.toml          Build config for pip-installable package
  README.md
  requirements.txt
  .gitignore
```

## Disclaimer

This tool is for personal, non-commercial use only. Respect vimm.net's terms of service and server capacity. The authors are not responsible for any misuse.
