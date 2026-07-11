"""Katalog tema rEFInd populer yang sudah dikurasi (open source, terverifikasi ada theme.conf)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ThemeCatalogEntry:
    key: str
    name: str
    git_url: str
    description: str


CATALOG: List[ThemeCatalogEntry] = [
    ThemeCatalogEntry(
        key="minimal",
        name="rEFInd-minimal",
        git_url="https://github.com/evanpurkhiser/rEFInd-minimal",
        description="Tema minimalis dan bersih, salah satu tema rEFInd paling populer (2000+ bintang GitHub).",
    ),
    ThemeCatalogEntry(
        key="regular",
        name="refind-theme-regular",
        git_url="https://github.com/bobafetthotmail/refind-theme-regular",
        description="Tema simpel & rapi, ada varian terang dan gelap yang bisa dipilih lewat theme.conf.",
    ),
    ThemeCatalogEntry(
        key="regular-dark",
        name="refind-theme-regular (dark fork)",
        git_url="https://github.com/1j01/refind-theme-regular",
        description="Fork bertema gelap dari refind-theme-regular.",
    ),
    ThemeCatalogEntry(
        key="material",
        name="refind-material-theme",
        git_url="https://github.com/Patricol/refind-material-theme",
        description="Tema bergaya Material Design.",
    ),
    ThemeCatalogEntry(
        key="brads",
        name="Brad's Refind Theme",
        git_url="https://github.com/toptensoftware/brads-refind-theme",
        description="Tema minimal dengan aset vector (SVG) untuk semua ikon.",
    ),
]


def find(key: str) -> Optional[ThemeCatalogEntry]:
    for entry in CATALOG:
        if entry.key == key:
            return entry
    return None
