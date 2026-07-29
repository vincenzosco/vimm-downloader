"""
console_list.py - vimm.net console codes for search queries.

Search URL uses the format:
    https://vimm.net/vault/?p=list&system={code}&q={query}

Pass code="" (empty string) to search all consoles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Map user-friendly names / short codes → vimm.net system query parameter.
CONSOLES: dict[str, str] = {
    # Full names
    "all": "",
    "nintendo": "NES",
    "master-system": "SMS",
    "genesis": "Genesis",
    "super-nintendo": "SNES",
    "sega-32x": "32X",
    "saturn": "Saturn",
    "playstation": "PS1",
    "nintendo-64": "N64",
    "dreamcast": "Dreamcast",
    "playstation-2": "PS2",
    "gamecube": "GameCube",
    "xbox": "Xbox",
    "xbox-360": "Xbox360",
    "playstation-3": "PS3",
    "wii": "Wii",
    "wiiware": "WiiWare",
    "game-boy": "GB",
    "game-gear": "GG",
    "virtual-boy": "VB",
    "game-boy-color": "GBC",
    "game-boy-advance": "GBA",
    "nintendo-ds": "DS",
    "ps-portable": "PSP",
    # Short codes
    "nes": "NES",
    "sms": "SMS",
    "gen": "Genesis",
    "snes": "SNES",
    "32x": "32X",
    "sat": "Saturn",
    "ps1": "PS1",
    "n64": "N64",
    "dc": "Dreamcast",
    "ps2": "PS2",
    "gc": "GameCube",
    "xbx": "Xbox",
    "x360": "Xbox360",
    "ps3": "PS3",
    "wii": "Wii",
    "wiw": "WiiWare",
    "gb": "GB",
    "gg": "GG",
    "vb": "VB",
    "gbc": "GBC",
    "gba": "GBA",
    "nds": "DS",
    "psp": "PSP",
}

#: Pretty-printable table for ``--list-consoles``
CONSOLE_TABLE: list[dict[str, str]] = [
    {"Platform": "all", "Code": ""},
    {"Platform": "nintendo (NES)", "Code": "NES"},
    {"Platform": "master-system (SMS)", "Code": "SMS"},
    {"Platform": "genesis (GEN)", "Code": "Genesis"},
    {"Platform": "super-nintendo (SNES)", "Code": "SNES"},
    {"Platform": "sega-32x (32X)", "Code": "32X"},
    {"Platform": "saturn (SAT)", "Code": "Saturn"},
    {"Platform": "playstation (PS1)", "Code": "PS1"},
    {"Platform": "nintendo-64 (N64)", "Code": "N64"},
    {"Platform": "dreamcast (DC)", "Code": "Dreamcast"},
    {"Platform": "playstation-2 (PS2)", "Code": "PS2"},
    {"Platform": "gamecube (GC)", "Code": "GameCube"},
    {"Platform": "xbox (XBX)", "Code": "Xbox"},
    {"Platform": "xbox-360 (X360)", "Code": "Xbox360"},
    {"Platform": "playstation-3 (PS3)", "Code": "PS3"},
    {"Platform": "wii (WII)", "Code": "Wii"},
    {"Platform": "wiiware (WIW)", "Code": "WiiWare"},
    {"Platform": "game-boy (GB)", "Code": "GB"},
    {"Platform": "game-gear (GG)", "Code": "GG"},
    {"Platform": "virtual-boy (VB)", "Code": "VB"},
    {"Platform": "game-boy-color (GBC)", "Code": "GBC"},
    {"Platform": "game-boy-advance (GBA)", "Code": "GBA"},
    {"Platform": "nintendo-ds (NDS)", "Code": "DS"},
    {"Platform": "ps-portable (PSP)", "Code": "PSP"},
]


def resolve_console(name_or_code: str) -> Optional[str]:
    """Try to resolve a user-supplied name/code to a vimm.net system value.

    Returns ``None`` if the name is not recognised.
    """
    return CONSOLES.get(name_or_code.lower().strip())
