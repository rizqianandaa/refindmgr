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


# NOTE ON TRUST: every entry is cloned from a third-party GitHub repository at
# whatever HEAD points to today, as root, onto the EFI System Partition. The
# clone is not pinned, so an upstream account takeover lands files on the boot
# partition of anyone who installs. Pinning a commit here would freeze themes
# forever and block legitimate updates, so instead refindmgr records the commit
# it actually installed in themes.json and prints it, and 'install <key>@<sha>'
# lets anyone who wants reproducibility pin explicitly.
CATALOG: List[ThemeCatalogEntry] = [
    ThemeCatalogEntry(
        "lite", "rEFInd-lite", "https://github.com/Littlebanbrick/rEFInd-lite",
        description="Tema ringan dengan ikon minimal.",
    ),
    ThemeCatalogEntry(
        "demon-slayer", "rEFInd Demon Slayer",
        "https://github.com/Wi-Fight-IT/rEFInd-demon-slayer",
        description="Bertema anime, tersedia dua varian karakter.",
        variants=(("Tanjiro", "rEFInd-Tanjiro"), ("Zenitsu", "rEFInd-Zenitsu")),
    ),
    ThemeCatalogEntry(
        "soho", "Soho rEFInd", "https://github.com/blackma9ick/refind",
        description="Palet Rose Pine; pilih Main, Moon, atau Dawn saat memasang.",
        install_name="rose-pine",
    ),
    ThemeCatalogEntry(
        "planets", "rEFInd Planets", "https://github.com/peteyyz/refind-planets",
        description="Latar bertema tata surya.",
        install_name="planets",
    ),
    ThemeCatalogEntry(
        "digital-void", "rEFInd Digital Void",
        "https://github.com/Wi-Fight-IT/rEFInd-digital-void",
        description="Gelap, kontras tinggi, aksen neon.",
    ),
    ThemeCatalogEntry(
        "minimalistic", "rEFInd Minimalistic",
        "https://github.com/mehedi-codes/refind-minimalistic",
        description="Ikon datar tanpa banner mencolok.",
        install_name="rEFInd-Minimalistic-Theme",
    ),
    ThemeCatalogEntry(
        "sublime", "rEFInd Sublime", "https://github.com/senpaiSubby/refind-sublime",
        description="Nuansa gelap ala editor Sublime Text.",
    ),
    ThemeCatalogEntry(
        "catppuccin", "Catppuccin rEFInd", "https://github.com/catppuccin/refind",
        description="Palet Catppuccin yang populer di berbagai aplikasi.",
        install_name="catppuccin",
    ),
]


def find(key: str) -> Optional[ThemeCatalogEntry]:
    aliases = {"minimal": "minimalistic"}
    key = aliases.get(key, key)
    for entry in CATALOG:
        if entry.key == key:
            return entry
    return None
