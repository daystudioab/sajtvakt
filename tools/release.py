#!/usr/bin/env python3
"""Bygger en ny version av WordPress-tillägget för självuppdatering.

    python3 tools/release.py "Vad som är nytt" "En sak till"

Läser versionen ur plugin/sajtvakt.php, packar plugin/ som dist/sajtvakt-VERSION.zip
(med mappen "sajtvakt" i roten) och skriver dist/update.json. När det pushas
till main hämtar sajterna den nya versionen inom sex timmar, och Sajtvakt-servern
installerar den på natten.
"""
import datetime
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
DIST = ROOT / "dist"
REPO = "daystudioab/sajtvakt"

header = (PLUGIN / "sajtvakt.php").read_text(encoding="utf-8")
version = re.search(r"^\s*\*\s*Version:\s*([0-9.]+)", header, re.M).group(1)
core = (PLUGIN / "includes" / "class-core.php").read_text(encoding="utf-8")
if f"VERSION   = '{version}'" not in core:
    sys.exit(f"Versionen i class-core.php stämmer inte med sajtvakt.php ({version}).")

DIST.mkdir(exist_ok=True)
zip_path = DIST / f"sajtvakt-{version}.zip"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for f in sorted(PLUGIN.rglob("*")):
        if f.is_file() and not f.name.startswith(".") and "__pycache__" not in f.parts:
            info = zipfile.ZipInfo(f"sajtvakt/{f.relative_to(PLUGIN).as_posix()}", (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, f.read_bytes())

meta = {
    "version": version,
    # Versionen i filnamnet gör att cachar aldrig serverar en gammal zip.
    "package": f"https://raw.githubusercontent.com/{REPO}/main/dist/{zip_path.name}",
    "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    "requires": "6.0",
    "requires_php": "7.4",
    "date": datetime.date.today().isoformat(),
    "changes": sys.argv[1:],
}
# Senaste versionen även under ett fast namn, för den som installerar för hand.
(DIST / "sajtvakt.zip").write_bytes(zip_path.read_bytes())
(DIST / "update.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Sajtvakt {version}: dist/{zip_path.name}, dist/sajtvakt.zip och dist/update.json klara.")
print(f"Committa och pusha:  git add -A && git commit -m 'Sajtvakt {version}' && git push")
