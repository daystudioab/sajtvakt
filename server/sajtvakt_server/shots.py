"""Skärmdumpar av startsidorna: en aktuell per sajt, plus en "före" vid uppdateringar
så att Sajtvakt kan se om sidan ändrat utseende fast den svarar normalt."""
from __future__ import annotations

import importlib.util
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger("sajtvakt.shots")

SHOTS = config.DATA_DIR / "shots"
_sem = threading.Semaphore(2)  # Högst två webbläsare samtidigt – de tar minne.
# Andel av bilden (0–1) som tydligt ändrats för att räknas som "ser annorlunda ut".
# Uppmätt: färgbyte i ett tema ~0,02, helt annan sida med samma upplägg ~0,1, sida utan CSS ~0,55.
CHANGED = 0.25


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def path(site_id: int, which: str = "") -> Path:
    return SHOTS / (f"{site_id}-{which}.jpg" if which else f"{site_id}.jpg")


def taken_at(site_id: int, which: str = "") -> int | None:
    p = path(site_id, which)
    return int(p.stat().st_mtime) if p.exists() else None


def url_for(site_id: int, which: str = "") -> str | None:
    t = taken_at(site_id, which)
    if not t:
        return None
    return f"/skarmdump/{site_id}" + (f"/{which}" if which else "") + f"?v={t}"


def take(site_id: int, url: str, which: str = "") -> Path | None:
    """Fotografera sidan. Returnerar sökvägen, eller None om det inte gick (då behålls den gamla bilden)."""
    if not available():
        return None
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = SHOTS / f".tmp-{site_id}-{secrets.token_hex(4)}.jpg"
    sep = "&" if "?" in url else "?"
    target = f"{url}{sep}sajtvakt-shot={int(time.time())}"  # Förbi sidcachen, som hälsokollen.
    with _sem:
        try:
            subprocess.run([sys.executable, "-m", "sajtvakt_server.shoot", target, str(tmp)],
                           timeout=120, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            log.warning("Skärmdump av %s misslyckades: %s", url, (e.stderr or b"")[-400:].decode(errors="replace"))
        except Exception as e:
            log.warning("Skärmdump av %s misslyckades: %s", url, e)
    if not tmp.exists() or tmp.stat().st_size < 1500:
        tmp.unlink(missing_ok=True)
        return None
    dest = path(site_id, which)
    os.replace(tmp, dest)
    return dest


def difference(a: Path, b: Path) -> float | None:
    """Hur stor del av bilden som tydligt ändrats (0 = lika, 1 = allt ändrat)."""
    try:
        from PIL import Image, ImageChops, ImageStat
        with Image.open(a) as ia, Image.open(b) as ib:
            sa = ia.convert("L").resize((64, 40))
            sb = ib.convert("L").resize((64, 40))
            changed = ImageChops.difference(sa, sb).point(lambda v: 255 if v > 40 else 0)
            return ImageStat.Stat(changed).mean[0] / 255
    except Exception as e:
        log.warning("Kunde inte jämföra skärmdumpar: %s", e)
        return None


def delete(site_id: int) -> None:
    for p in (path(site_id), path(site_id, "fore")):
        p.unlink(missing_ok=True)
