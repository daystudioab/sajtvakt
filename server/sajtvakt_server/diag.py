"""Felsökning av långsamma backuper: var går tiden, och vilka tabeller är stora?

Körs på servern:
    docker compose exec -T sajtvakt python -m sajtvakt_server.diag "Nyheter"
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config, db, store
from .jobs import fmt_bytes, fmt_secs, fmt_timing


def _ts(v) -> str:
    return datetime.fromtimestamp(v, config.TZ).strftime("%Y-%m-%d %H:%M") if v else "–"


def table_sizes(dump: Path) -> list[tuple[str, int, int]]:
    """Okomprimerad storlek och antal INSERT-rader per tabell, ur en databasdump."""
    sizes: dict[str, list[int]] = {}
    cur = None
    head = re.compile(r"^-- Tabell (\S+)")
    with gzip.open(dump, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = head.match(line)
            if m:
                cur = m.group(1)
                sizes[cur] = [0, 0]
                continue
            if cur:
                sizes[cur][0] += len(line)
                if line.startswith("("):
                    sizes[cur][1] += 1
    return sorted(((t, b, r) for t, (b, r) in sizes.items()), key=lambda x: -x[1])


def main(name: str) -> None:
    db.init()
    site = db.one("SELECT * FROM sites WHERE name LIKE ? OR url LIKE ? ORDER BY id LIMIT 1", f"%{name}%", f"%{name}%")
    if not site:
        print(f"Hittar ingen sajt som matchar {name!r}.")
        return
    st = json.loads(site["status_json"] or "{}")
    env = st.get("environment") or {}
    print(f"== {site['name']}  {site['url']}")
    print(f"   PHP max_execution_time: {env.get('max_execution_time')} s · tillägg {st.get('versions', {}).get('sajtvakt')}")

    print("\n== Senaste jobben")
    for j in db.q("SELECT kind, status, created, started, finished, error FROM jobs WHERE site_id=? ORDER BY id DESC LIMIT 12", site["id"]):
        start = j["started"] or j["created"]
        dur = fmt_secs(j["finished"] - start) if j["finished"] and start else "pågår/okänd"
        print(f"   {_ts(j['created'])}  {j['kind']:<10} {j['status']:<7} {dur:>14}  {(j['error'] or '')[:70]}")

    print("\n== Senaste backuperna")
    newest_dump = None
    for b in db.q("SELECT * FROM backups WHERE site_id=? ORDER BY id DESC LIMIT 10", site["id"]):
        keys = b.keys()
        inc = "stegvis" if "incremental" in keys and b["incremental"] else "hel"
        fetched = fmt_bytes(b["fetched"]) if "fetched" in keys and b["fetched"] else "–"
        timing = json.loads(b["timing"]) if "timing" in keys and b["timing"] else None
        print(f"   {_ts(b['created'])}  {b['kind']:<11} {b['status']:<6} {inc:<8} {fmt_bytes(b['size'] or 0):>9}  hämtat {fetched:>9}  {b['files'] or 0} filer")
        if timing:
            print(f"      {fmt_timing(timing)}")
        if b["status"] == "error" and b["error"]:
            print(f"      fel: {b['error'][:120]}")
        if newest_dump is None and b["status"] == "done" and b["path"]:
            p = Path(b["path"]) / "database.sql.gz"
            if p.exists():
                newest_dump = p

    if newest_dump:
        print(f"\n== Databasen (ur senaste dumpen, {fmt_bytes(newest_dump.stat().st_size)} komprimerad)")
        rows = table_sizes(newest_dump)
        total = sum(r[1] for r in rows) or 1
        print(f"   Totalt {fmt_bytes(total)} SQL i {len(rows)} tabeller. Största:")
        for t, size, n in rows[:20]:
            print(f"   {fmt_bytes(size):>9}  {size * 100 // total:>3} %  {n:>9} rader  {t}")

    files = store.file_list(Path(newest_dump.parent)) if newest_dump else []
    if files:
        by_dir: dict[str, list[int]] = {}
        for rel, _sha, size, _dt in files:
            parts = rel.split("/")
            key = "/".join(parts[:3]) if parts[0] == "wp-content" and len(parts) > 3 else "/".join(parts[:-1]) or rel
            by_dir.setdefault(key, [0, 0])
            by_dir[key][0] += 1
            by_dir[key][1] += int(size)
        print(f"\n== Filerna ({len(files)} st). Mappar med flest filer:")
        for d, (n, size) in sorted(by_dir.items(), key=lambda x: -x[1][0])[:12]:
            print(f"   {n:>8} filer  {fmt_bytes(size):>9}  {d}")
    print(f"\n(körd {datetime.now(config.TZ):%Y-%m-%d %H:%M})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Nyheter")
