"""Backup-lagring med deduplicering.

Varje fil i en backup sparas en gång, som en "blob" namngiven efter sin
SHA-256. 30 nattliga backuper av en sajt där bara några filer ändras tar
därför ungefär samma plats som en enda. Databasdumpen sparas som den är.

  store/blobs/ab/abcdef….z   (zlib-komprimerad)   eller  ….r  (redan komprimerad fil, t.ex. jpg)
  store/backups/<sajt>/<backup>/database.sql.gz
                              files.json.gz      [sökväg, sha256, storlek, datum]
                              manifest.json      (från pluginet)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
import zipfile
import zlib
from pathlib import Path

from . import config

BLOBS = config.STORE_DIR / "blobs"
BACKUPS = config.STORE_DIR / "backups"
PART_MAX = 64 * 1024 * 1024

COMPRESSED_EXT = {
    "jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "mp4", "m4v", "mov", "webm", "mp3", "m4a",
    "ogg", "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "pdf", "woff", "woff2", "docx", "xlsx", "pptx",
}

BLOBS.mkdir(parents=True, exist_ok=True)
BACKUPS.mkdir(parents=True, exist_ok=True)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def blob_path(sha: str) -> Path | None:
    base = BLOBS / sha[:2] / sha
    for suffix in (".z", ".r"):
        p = base.with_name(sha + suffix)
        if p.exists():
            return p
    return None


def backup_path(site_id: int, backup_id: int) -> Path:
    return BACKUPS / str(site_id) / str(backup_id)


# ------------------------------------------------------------------ ingest

def ingest(download_dir: Path, dest: Path) -> dict:
    """Flytta en nedladdad backup (zip-delar) in i lagret. Returnerar statistik."""
    dest.mkdir(parents=True, exist_ok=True)
    tmp_dir = BLOBS / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    entries: list[list] = []
    new_bytes = 0
    raw_bytes = 0
    for part in sorted(download_dir.glob("files-*.zip")):
        with zipfile.ZipFile(part) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                packed = _ext(info.filename) in COMPRESSED_EXT
                tmp = tmp_dir / f"{os.getpid()}-{time.time_ns()}"
                h = hashlib.sha256()
                comp = None if packed else zlib.compressobj(6)
                with zf.open(info) as src, open(tmp, "wb") as out:
                    while chunk := src.read(1024 * 1024):
                        h.update(chunk)
                        out.write(chunk if comp is None else comp.compress(chunk))
                    if comp is not None:
                        out.write(comp.flush())
                sha = h.hexdigest()
                existing = blob_path(sha)
                if existing:
                    tmp.unlink()
                    os.utime(existing)  # Markera som använd, så städningen inte tar den mitt i.
                else:
                    target = BLOBS / sha[:2] / (sha + (".r" if packed else ".z"))
                    target.parent.mkdir(exist_ok=True)
                    new_bytes += tmp.stat().st_size
                    os.replace(tmp, target)
                entries.append([info.filename, sha, info.file_size, list(info.date_time)])
                raw_bytes += info.file_size
        part.unlink()

    db = download_dir / "database.sql.gz"
    db_size = 0
    if db.exists():
        db_size = db.stat().st_size
        shutil.move(str(db), dest / "database.sql.gz")
    shutil.copy(download_dir / "manifest.json", dest / "manifest.json")
    with gzip.open(dest / "files.json.gz", "wt", encoding="utf-8") as f:
        json.dump(entries, f)

    return {"files": len(entries), "size": raw_bytes + db_size, "new_bytes": new_bytes + db_size}


def file_list(dest: Path) -> list[list]:
    p = dest / "files.json.gz"
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def _read_blob(sha: str):
    p = blob_path(sha)
    if p is None:
        raise FileNotFoundError(f"Blob saknas: {sha}")
    if p.suffix == ".r":
        with open(p, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
    else:
        d = zlib.decompressobj()
        with open(p, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield d.decompress(chunk)
        yield d.flush()


# ------------------------------------------------------------------ rebuild

def build_package(dest: Path, out_dir: Path) -> Path:
    """Återskapa en backup i pluginets format (för push eller nedladdning)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    outputs = []

    if (dest / "database.sql.gz").exists():
        shutil.copy(dest / "database.sql.gz", out_dir / "database.sql.gz")
        outputs.append(_describe(out_dir / "database.sql.gz"))

    part, size, zf = 0, 0, None
    for name, sha, fsize, dt in file_list(dest):
        if zf is None or (size > 0 and size + fsize > PART_MAX):
            if zf:
                zf.close()
                outputs.append(_describe(out_dir / f"files-{part:03d}.zip"))
            part += 1
            size = 0
            zf = zipfile.ZipFile(out_dir / f"files-{part:03d}.zip", "w", allowZip64=True)
        info = zipfile.ZipInfo(name, date_time=tuple(dt))
        info.compress_type = zipfile.ZIP_STORED if _ext(name) in COMPRESSED_EXT else zipfile.ZIP_DEFLATED
        with zf.open(info, "w", force_zip64=fsize > 2**31) as out:
            for chunk in _read_blob(sha):
                out.write(chunk)
        size += fsize
    if zf:
        zf.close()
        outputs.append(_describe(out_dir / f"files-{part:03d}.zip"))

    manifest["outputs"] = outputs
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_dir


def _describe(p: Path) -> dict:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return {"name": p.name, "size": p.stat().st_size, "sha256": h.hexdigest()}


# ------------------------------------------------------------------ cleanup

def delete_backup(path: str | Path | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def gc() -> dict:
    """Radera blobs som ingen backup längre använder."""
    used: set[str] = set()
    for fl in BACKUPS.glob("*/*/files.json.gz"):
        try:
            used.update(e[1] for e in file_list(fl.parent))
        except Exception:
            return {"removed": 0, "freed": 0, "error": f"kunde inte läsa {fl}"}  # Hellre ingen städning än fel städning.
    removed = freed = 0
    cutoff = time.time() - 6 * 3600  # Rör inte blobs från en backup som pågår just nu.
    for p in BLOBS.glob("??/*"):
        sha = p.name.split(".")[0]
        if sha not in used and p.stat().st_mtime < cutoff:
            freed += p.stat().st_size
            p.unlink()
            removed += 1
    for p in (BLOBS / "tmp").glob("*"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
    return {"removed": removed, "freed": freed}


def usage() -> int:
    total = 0
    for root, _dirs, files in os.walk(config.STORE_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
