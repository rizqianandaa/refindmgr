"""Katalog tema rEFInd pilihan."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ThemeCatalogEntry:
    key: str
    name: str
    git_url: str
    description: str = ""
    variants: Tuple[Tuple[str, str], ...] = ()
    install_name: Optional[str] = None


CATALOG: List[ThemeCatalogEntry] = [
    ThemeCatalogEntry("lite", "rEFInd-lite", "https://github.com/Littlebanbrick/rEFInd-lite"),
    ThemeCatalogEntry("demon-slayer", "rEFInd Demon Slayer", "https://github.com/Wi-Fight-IT/rEFInd-demon-slayer", variants=(("Tanjiro", "rEFInd-Tanjiro"), ("Zenitsu", "rEFInd-Zenitsu"))),
    ThemeCatalogEntry("soho", "Soho rEFInd", "https://github.com/blackma9ick/refind", install_name="rose-pine"),
    ThemeCatalogEntry("planets", "rEFInd Planets", "https://github.com/peteyyz/refind-planets", install_name="planets"),
    ThemeCatalogEntry("digital-void", "rEFInd Digital Void", "https://github.com/Wi-Fight-IT/rEFInd-digital-void"),
    ThemeCatalogEntry("minimalistic", "rEFInd Minimalistic", "https://github.com/mehedi-codes/refind-minimalistic", install_name="rEFInd-Minimalistic-Theme"),
    ThemeCatalogEntry("sublime", "rEFInd Sublime", "https://github.com/senpaiSubby/refind-sublime"),
]


def find(key: str) -> Optional[ThemeCatalogEntry]:
    for entry in CATALOG:
        if entry.key == key:
            return entry
    return None
