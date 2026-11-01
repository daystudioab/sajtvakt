"""Det Sajtvakt faktiskt gör: backup, uppdatering med skyddsnät, push av backup,
upptids- och SSL-koll. Allt körs som jobb i bakgrunden, ett i taget per sajt."""
from __future__ import annotations

import json
import logging
import secrets
import shutil
import socket
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import config, db, mailer, shots, store
from .client import SajtvaktError, Site, parse_connection

log = logging.getLogger("sajtvakt.jobs")

_executor = ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_JOBS, thread_name_prefix="jobb")
_site_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

JOB_NAMES = {
    "backup": "Backup",
    "nightly": "Nattlig körning",
    "update": "Uppdatering",
    "restore": "Återställning",
    "migrate": "Flytt",
    "refresh": "Uppdatera status",
    "rollback": "Återställ uppdatering",
    "shot": "Skärmdump",
    "remove": "Avinstallation",
}


def _lock(site_id: int) -> threading.Lock:
    with _locks_guard:
        return _site_locks.setdefault(site_id, threading.Lock())


def client(site_row) -> Site:
    return Site(site_row["url"], site_row["key"], timeout=180,
                endpoint=site_row["endpoint"] or "admin-post", rescue_url=site_row["rescue_url"])


def fmt_bytes(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "kB") else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return ""


# ====================================================================== job runner

def submit(kind: str, site_id: int | None, fn: Callable[[int], dict | None]) -> int:
    """Köa ett jobb. fn får jobb-id och returnerar ett resultat (visas i panelen)."""
    job_id = db.job_create(site_id, kind)

    def runner():
        lock = _lock(site_id) if site_id else None
        if lock:
            lock.acquire()
        try:
            db.job_update(job_id, status="running", started=int(time.time()), progress="Startar …")
            result = fn(job_id)
            db.job_update(job_id, status="done", finished=int(time.time()), progress="Klart", result=result or {})
        except Exception as e:
            log.error("Jobb %s (%s) misslyckades: %s\n%s", job_id, kind, e, traceback.format_exc())
            msg = e.message if isinstance(e, SajtvaktError) else str(e)
            db.job_update(job_id, status="error", finished=int(time.time()), error=msg)
            if site_id:
                db.run("UPDATE sites SET last_error = ? WHERE id = ?", msg, site_id)
                if kind not in ("refresh", "shot") and not getattr(e, "emailed", False):
                    row = db.site(site_id)
                    name = row["name"] if row else f"sajt {site_id}"
                    db.event(site_id, "error", f"{JOB_NAMES.get(kind, kind)} misslyckades", msg)
                    mailer.send(f"{JOB_NAMES.get(kind, kind)} misslyckades – {name}",
                                f"{JOB_NAMES.get(kind, kind)} för {name} gick inte igenom:\n\n{msg}\n\n{config.PUBLIC_URL}/sajt/{site_id}")
        finally:
            if lock:
                lock.release()

    _executor.submit(runner)
    return job_id


def step_budget(row) -> int:
    """Hur länge ett steg får jobba på sajten: hälften av webbhotellets tidsgräns, högst 20 s."""
    st = status_of(row) or {}
    limit = int((st.get("environment") or {}).get("max_execution_time") or 0)
    if limit <= 0:
        return 20
    return max(5, min(20, limit // 2))


def busy(site_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM jobs WHERE site_id = ? AND status IN ('queued','running')", site_id))


def progress(job_id: int | None, text: str) -> None:
    if job_id:
        db.job_update(job_id, progress=text)


# ====================================================================== sites

def add_site(connection: str, name: str = "") -> int:
    url, key = parse_connection(connection.strip())
    endpoint = "admin-post"
    s = Site(url, key, timeout=60)
    try:
        info = s.ping()
    except SajtvaktError as e:
        if e.code in ("unauthorized", "bad_timestamp"):
            raise
        # wp-admin kan vara låst (t.ex. av ett säkerhetstillägg) – prova reservvägen.
        endpoint = "frontend"
        s = Site(url, key, timeout=60, endpoint=endpoint)
        info = s.ping()
    rescue = info.get("rescue")
    st = s.status()
    name = name.strip() or st["site"]["name"] or urlparse(url).netloc
    now = int(time.time())
    existing = db.one("SELECT id FROM sites WHERE url = ?", url)
    if existing:
        db.run("UPDATE sites SET key=?, endpoint=?, rescue_url=?, status_json=?, status_at=? WHERE id=?",
               key, endpoint, rescue, json.dumps(st), now, existing["id"])
        db.event(existing["id"], "info", "Ny nyckel sparad")
        return existing["id"]
    sid = db.run(
        "INSERT INTO sites(name, url, key, endpoint, rescue_url, added, status_json, status_at) VALUES(?,?,?,?,?,?,?,?)",
        name, url, key, endpoint, rescue, now, json.dumps(st), now,
    )
    note = " Wp-admin är spärrat för anrop, så reservvägen används." if endpoint == "frontend" else ""
    db.event(sid, "ok", "Sajten lades till", f"WordPress {st['versions']['wordpress']}, PHP {st['versions']['php']}.{note}")
    check_safety_nets(sid)
    return sid


def check_safety_nets(site_id: int) -> None:
    """Fungerar nödingången? Och är Sajtvakts datamapp skyddad mot åtkomst utifrån?"""
    row = db.site(site_id)
    st = status_of(row) or {}
    info = st.get("sajtvakt") or {}
    # Prova vägarna i tur och ordning: den som fungerade sist, filen i WordPress-roten, filen i plugins-mappen.
    candidates = [u for u in (row["rescue_url"], info.get("rescue_alt"), info.get("rescue")) if u]
    candidates = list(dict.fromkeys(candidates))
    working, last_error = None, None
    for url in candidates:
        try:
            s = client(row)
            s.rescue_url = url
            if s.request("ping", rescue=True).get("ok"):
                working = url
                break
        except Exception as e:
            last_error = e
    if working:
        if working != row["rescue_url"]:
            db.run("UPDATE sites SET rescue_url = ? WHERE id = ?", working, site_id)
            db.event(site_id, "ok", "Nödingången fungerar", working)
    else:
        db.event(site_id, "warning", "Nödingången kan inte nås",
                 "Om en uppdatering kraschar WordPress helt kan Sajtvakt inte återställa automatiskt – "
                 "bara från backup. Uppdatera Sajtvakt-tillägget till senaste versionen; den lägger en nödingång i "
                 f"WordPress-roten som fungerar även när webbhotellet spärrar plugins-mappen. ({last_error})")
    canary = (st.get("sajtvakt") or {}).get("canary")
    if canary:
        try:
            with urllib.request.urlopen(urllib.request.Request(canary, headers={"User-Agent": "Sajtvakt"}), timeout=20) as r:
                if r.status == 200 and b"sajtvakt-canary" in r.read(100):
                    db.event(site_id, "warning", "Sajtvakts datamapp går att läsa utifrån",
                             "Webbservern (troligen nginx) följer inte .htaccess. Innehållet skyddas av slumpade namn och "
                             "PHP-skydd, men be gärna webbhotellet spärra wp-content/sajtvakt-data.")
        except Exception:
            pass  # 403/404 = skyddad, som det ska vara.


def refresh_status(site_id: int, job_id: int | None = None, refresh: bool = True) -> dict:
    row = db.site(site_id)
    progress(job_id, "Hämtar status från sajten …")
    c = client(row)
    st = c.status(refresh=refresh)
    try:
        st["_snapshots"] = c.request("snapshots").get("snapshots", [])[:10]
    except Exception:
        st["_snapshots"] = []
    # rescue_url väljs av check_safety_nets (den väg som faktiskt fungerar) – sätts här bara om den saknas.
    db.run("UPDATE sites SET status_json = ?, status_at = ?, last_error = NULL, rescue_url = COALESCE(rescue_url, ?) WHERE id = ?",
           json.dumps(st), int(time.time()), st.get("sajtvakt", {}).get("rescue"), site_id)
    return st


def status_of(row) -> dict | None:
    return json.loads(row["status_json"]) if row["status_json"] else None


# ====================================================================== backup

def do_backup(site_id: int, kind: str = "manual", job_id: int | None = None, note: str = "") -> int:
    row = db.site(site_id)
    backup_id = db.run("INSERT INTO backups(site_id, created, kind, status, note) VALUES(?,?,?,?,?)",
                       site_id, int(time.time()), kind, "running", note)
    work = config.TMP_DIR / f"dl-{site_id}-{backup_id}"
    shutil.rmtree(work, ignore_errors=True)
    dest = store.backup_path(site_id, backup_id)
    try:
        def prog(j):
            if j.get("phase") == "db":
                p = j["progress"]
                progress(job_id, f"Backup: databasen ({p['tables_done']}/{p['tables_total']} tabeller)")
            elif j.get("phase") in ("scan", "files"):
                p = j["progress"]
                progress(job_id, f"Backup: filer ({p['files_done']}/{p['files_total']})")
        progress(job_id, "Backup: startar …")
        man = client(row).backup(str(work), progress=prog, budget=step_budget(row))
        progress(job_id, "Backup: sparar …")
        stats = store.ingest(work, dest)
        db.run("UPDATE backups SET status='done', size=?, files=?, tables=?, path=? WHERE id=?",
               stats["size"], stats["files"], man.get("tables", 0), str(dest), backup_id)
        db.run("UPDATE sites SET last_backup = ? WHERE id = ?", int(time.time()), site_id)
        if man.get("skipped"):
            db.event(site_id, "warning", f"Backup klar men {len(man['skipped'])} filer hoppades över",
                     "\n".join(f"{s['file']} – {s['reason']}" for s in man["skipped"][:30]))
        db.event(site_id, "ok", "Backup klar",
                 f"{stats['files']} filer, {man.get('tables', 0)} tabeller, {fmt_bytes(stats['size'])} (nytt i lagret: {fmt_bytes(stats['new_bytes'])})")
        return backup_id
    except Exception as e:
        msg = e.message if isinstance(e, SajtvaktError) else str(e)
        db.run("UPDATE backups SET status='error', error=? WHERE id=?", msg, backup_id)
        store.delete_backup(dest)
        db.event(site_id, "error", "Backup misslyckades", msg)
        mailer.send(f"Backup misslyckades – {row['name']}", f"Backupen av {row['url']} misslyckades:\n\n{msg}")
        try:
            e.emailed = True
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ====================================================================== health

MAINT_WORDS = (b"maintenance", b"underh\xc3\xa5ll", b"coming soon", b"kommer snart", b"under construction")


def check_http(url: str, maintenance: bool = False) -> dict:
    """Hämta sidan som en besökare. Trasig = går inte att nå, 5xx eller WordPress felsida.
    I underhållsläge räknas en underhållssida (503) som att sajten fungerar."""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}sajtvakt-check={int(time.time())}",
                                 headers={"User-Agent": "Sajtvakt-monitor/0.1", "Cache-Control": "no-cache"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(400_000)
            code = r.status
            retry_after = bool(r.headers.get("Retry-After"))
    except urllib.error.HTTPError as e:
        body = e.read(400_000) if e.fp else b""
        code = e.code
        retry_after = bool(e.headers.get("Retry-After")) if e.headers else False
    except Exception as e:
        return {"ok": False, "code": None, "ms": None, "error": str(e)[:200]}
    ms = int((time.time() - t) * 1000)
    wp_error = b'id="error-page"' in body or b"wp-die-message" in body
    low = body[:200_000].lower()
    # 503 som ser ut som en underhållssida (Elementor, WP Maintenance Mode, SeedProd m.fl.).
    maint_like = code == 503 and (retry_after or any(w in low for w in MAINT_WORDS))
    ok = code < 500 and not wp_error
    if maintenance and code == 503:
        # Underhållsläge: 503 är underhållssidan – även när den visas via WordPress egen
        # felsida (wp_die), som WordPress inbyggda underhållsläge och vissa tillägg gör.
        ok = True
    return {"ok": ok, "code": code, "ms": ms, "bytes": len(body), "maintenance": maint_like,
            "error": "WordPress visar en felsida" if wp_error else ("underhållssida (503)" if maint_like else None)}


def healthy(after: dict, before: dict | None) -> bool:
    if not after["ok"]:
        return False
    if before and before.get("code") and before["code"] < 400 and after["code"] and after["code"] >= 400:
        return False
    # En vit sida (200 men nästan tom) räknas också som trasig.
    if before and (before.get("bytes") or 0) >= 2000 and (after.get("bytes") or 0) < before["bytes"] * 0.2:
        return False
    return True


def monitor_site(site_id: int) -> None:
    row = db.site(site_id)
    in_maint = bool(row["maintenance"])
    res = check_http(row["url"], maintenance=in_maint)
    now = int(time.time())
    db.run("UPDATE sites SET last_code = ? WHERE id = ?", res["code"], site_id)
    if res["ok"]:
        if row["up"] == 0 and row["down_since"] and in_maint and res["code"] == 503:
            db.event(site_id, "info", "Underhållsläge", "Sajten visar sin underhållssida. Sajtvakt larmar inte för den.")
        elif row["up"] == 0 and row["down_since"]:
            mins = max(1, (now - row["down_since"]) // 60)
            db.event(site_id, "ok", "Sajten är uppe igen", f"Var nere i ungefär {mins} min.")
            mailer.send(f"Uppe igen – {row['name']}", f"{row['url']} svarar igen efter ungefär {mins} minuter.")
        db.run("UPDATE sites SET up=1, fails=0, down_since=NULL, checked_at=?, response_ms=? WHERE id=?", now, res["ms"], site_id)
        return
    fails = (row["fails"] or 0) + 1
    if fails >= 2 and row["up"] != 0:
        since = now - config.MONITOR_INTERVAL
        reason = res["error"] or f"HTTP {res['code']}"
        db.run("UPDATE sites SET up=0, fails=?, down_since=?, checked_at=? WHERE id=?", fails, since, now, site_id)
        hint = ("\n\nSidan ser ut som en underhållssida. Har du satt sajten i underhållsläge med flit? Slå då på "
                f"Underhållsläge för sajten i panelen, så slutar larmen: {config.PUBLIC_URL}/sajt/{site_id}") if res.get("maintenance") else ""
        db.event(site_id, "error", "Sajten är nere", reason + (" – slå på Underhållsläge för sajten om det är med flit." if hint else ""))
        mailer.send(f"NERE – {row['name']}", f"{row['url']} svarar inte som den ska ({reason}).\n\nSajtvakt kollar igen var {config.MONITOR_INTERVAL // 60}:e minut och mejlar när den är uppe.{hint}")
    else:
        db.run("UPDATE sites SET fails=?, checked_at=? WHERE id=?", fails, now, site_id)


def ssl_days_left(url: str) -> int | None:
    u = urlparse(url)
    if u.scheme != "https":
        return None
    ctx = ssl.create_default_context()
    with socket.create_connection((u.hostname, u.port or 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=u.hostname) as s:
            cert = s.getpeercert()
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    return int((expires - time.time()) // 86400)


def check_ssl(site_id: int) -> None:
    row = db.site(site_id)
    try:
        days = ssl_days_left(row["url"])
    except Exception as e:
        days = -1
        db.event(site_id, "warning", "SSL-certifikatet kunde inte kontrolleras", str(e)[:300])
    db.run("UPDATE sites SET ssl_days=?, ssl_checked=? WHERE id=?", days, int(time.time()), site_id)
    if days is not None and 0 <= days < 14:
        db.event(site_id, "warning", f"SSL-certifikatet går ut om {days} dagar",
                 "Certifikat förnyas oftast automatiskt några dagar innan. Gör de inte det: kontrollera hos webbhotellet.")
        mailer.send(f"SSL går snart ut – {row['name']}", f"Certifikatet för {row['url']} går ut om {days} dagar. Kontrollera förnyelsen hos webbhotellet.")


# ====================================================================== updates

def pending_updates(row, st: dict, include_major_core: bool = False) -> tuple[list[dict], list[str]]:
    skip = set(json.loads(row["skip_plugins"] or "[]"))
    items, notes = [], []
    for p in st["plugins"]:
        if not p["update"]:
            continue
        if p["file"] in skip:
            notes.append(f"{p['name']} {p['update']['version']} väntar (låst i Sajtvakt)")
        elif not p["update"]["package"]:
            notes.append(f"{p['name']} {p['update']['version']} kan inte hämtas – licensen saknas eller har gått ut")
        else:
            items.append({"type": "plugin", "slug": p["file"], "name": p["name"], "to": p["update"]["version"], "from": p["version"]})
    for t in st["themes"]:
        if t["update"]:
            if t["slug"] in skip:
                notes.append(f"Temat {t['name']} {t['update']['version']} väntar (låst i Sajtvakt)")
            elif t["update"]["package"]:
                items.append({"type": "theme", "slug": t["slug"], "name": t["name"], "to": t["update"]["version"], "from": t["version"]})
            else:
                notes.append(f"Temat {t['name']} {t['update']['version']} kan inte hämtas automatiskt")
    if st["updates"]["translations"]:
        items.append({"type": "translations", "slug": "translations", "name": "Översättningar"})
    core = st["updates"]["core"]
    if core:
        # WordPress uppdateras alltid, även större versioner – med kopia och återställning som tilläggen.
        if "wordpress" in skip:
            notes.append(f"WordPress {core['version']} väntar (låst i Sajtvakt)")
        else:
            items.append({"type": "core", "slug": "wordpress", "name": "WordPress", "to": core["version"], "from": st["versions"]["wordpress"]})
    return items, notes


CRASH_CODES = {"site_error", "http_error", "bad_response", "network"}


def _rollback(site: Site, ref: str = "", snapshot: str = "") -> str:
    """Återställ exakt den ögonblicksbild som hör till uppdateringen (ref) eller ett angivet id,
    via vanliga vägen eller nödingången. Returnerar vilken väg som fungerade."""
    try:
        r = site.rollback(snapshot=snapshot, ref=ref)
        if r.get("ok"):
            return "api"
    except Exception:
        pass
    r = site.rollback(snapshot=snapshot, ref=ref, rescue=True)
    if not r.get("ok"):
        raise SajtvaktError("rollback_failed", r.get("message", "Återställningen misslyckades"))
    return "nödingång"


def _label(i: dict) -> str:
    if i["type"] == "translations":
        return "Översättningar"
    return f"{i['name']} {i.get('from', '')} → {i.get('to', '')}".strip()


def do_update(site_id: int, job_id: int | None = None, only: list[str] | None = None,
              include_major_core: bool = False) -> dict:
    row = db.site(site_id)
    s = client(row)
    st = refresh_status(site_id, job_id)
    items, notes = pending_updates(row, st, include_major_core)
    if only is not None:
        items = [i for i in items if i["slug"] in only]
    if not items:
        return {"updated": [], "notes": notes, "message": "Inget att uppdatera"}

    maint = bool(row["maintenance"])
    baseline = check_http(row["url"], maint)
    if not baseline["ok"]:
        raise SajtvaktError("site_down", "Sajten fungerar inte redan före uppdateringen – uppdaterar inte."
                            + (" Den visar en underhållssida – slå på Underhållsläge för sajten om det är med flit." if baseline.get("maintenance") else ""))

    recent = db.one("SELECT id FROM backups WHERE site_id=? AND status='done' AND created > ?", site_id, int(time.time()) - 2 * 3600)
    if not recent:
        do_backup(site_id, "pre-update", job_id, note="Före uppdatering")

    # Skärmdump före, så att utseendet kan jämföras efteråt.
    progress(job_id, "Skärmdump före uppdateringen …")
    before_shot = shots.take(site_id, row["url"], "fore")

    files_items = [i for i in items if i["type"] in ("plugin", "theme", "translations")]
    core_items = [i for i in items if i["type"] == "core"]
    updated, rolled_back, failed = [], [], []

    def run_batch(batch: list[dict], refresh: bool = False) -> tuple[dict, bool]:
        """Uppdatera och kontrollera. Varje körning får en egen referens, så att rätt
        ögonblicksbild återställs – aldrig en annan uppdaterings."""
        ref = secrets.token_hex(16)
        try:
            res = s.update([{"type": i["type"], "slug": i["slug"]} for i in batch], refresh=refresh, ref=ref)
        except SajtvaktError as e:
            if e.code not in CRASH_CODES:
                raise  # Pluginet sa nej (t.ex. fullt på disken) – inget har ändrats.
            log.warning("Uppdateringsanropet dog: %s", e)
            return {"results": [], "ref": ref}, False
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("Uppdateringsanropet avbröts: %s", e)
            return {"results": [], "ref": ref}, False
        res["ref"] = ref
        after = check_http(row["url"], maint)
        pc = res.get("check") or {}
        ok = healthy(after, baseline) and (pc.get("ok") is not False or (maint and pc.get("code") == 503))
        if ok:
            # Startsidan kan komma från en cache – kontrollera att WordPress självt laddar (adminläge).
            try:
                s.ping()
            except Exception:
                ok = False
        return res, ok

    def undo(res: dict) -> str:
        try:
            return _rollback(s, res["ref"])
        except SajtvaktError:
            # Finns ingen ögonblicksbild hann inget ändras – det räcker om sajten fungerar.
            if healthy(check_http(row["url"], maint), baseline):
                return "behövdes inte"
            raise

    if files_items:
        progress(job_id, f"Uppdaterar {len(files_items)} st …")
        res, ok = run_batch(files_items)

        if ok:
            for r in res.get("results", []):
                (updated if r["ok"] and r["status"] == "updated" else failed if not r["ok"] else []).append(r)
        else:
            progress(job_id, "Sajten gick sönder – återställer …")
            try:
                how = undo(res)
            except SajtvaktError:
                how = None
            if how is None or not healthy(check_http(row["url"], maint), baseline):
                db.event(site_id, "error", "Sajten är trasig efter uppdatering och kunde inte återställas automatiskt",
                         "Återställ från backupen i panelen.")
                mailer.send(f"AKUT – {row['name']} trasig efter uppdatering",
                            f"Efter uppdatering av {', '.join(_label(i) for i in files_items)} fungerar inte {row['url']}, "
                            f"och den automatiska återställningen hjälpte inte.\n\nÅterställ från backupen i panelen: {config.PUBLIC_URL}/sajt/{site_id}")
                err = SajtvaktError("still_broken", "Sajten fungerar inte efter återställningen.")
                err.emailed = True
                raise err
            db.event(site_id, "warning", "Uppdateringen fick sajten att gå sönder – återställd", f"Återställd via {how}.")

            # Hitta den skyldige: ett i taget.
            if len(files_items) > 1:
                for item in files_items:
                    progress(job_id, f"Testar {item['name']} för sig …")
                    r1, ok1 = run_batch([item], refresh=True)
                    if ok1:
                        updated.extend(r for r in r1.get("results", []) if r["ok"] and r["status"] == "updated")
                    else:
                        undo(r1)
                        rolled_back.append(item)
            else:
                rolled_back.extend(files_items)

    if core_items:
        progress(job_id, "Uppdaterar WordPress …")
        res, ok = run_batch(core_items)
        if ok:
            for r in res.get("results", []):
                (updated if r["ok"] and r["status"] == "updated" else failed if not r["ok"] else []).append(r)
        else:
            progress(job_id, "WordPress-uppdateringen fick sajten att gå sönder – återställer …")
            try:
                how = undo(res)
            except SajtvaktError:
                how = None
            if how is None or not healthy(check_http(row["url"], maint), baseline):
                db.event(site_id, "error", "Sajten fungerar inte efter WordPress-uppdateringen",
                         "Den automatiska återställningen hjälpte inte. Återställ från backupen före uppdateringen.")
                mailer.send(f"AKUT – {row['name']} trasig efter WordPress-uppdatering",
                            f"{row['url']} fungerar inte efter uppdateringen av WordPress, och den automatiska återställningen hjälpte inte.\n\n"
                            f"Återställ från backupen före uppdateringen i panelen: {config.PUBLIC_URL}/sajt/{site_id}")
                err = SajtvaktError("still_broken", "Sajten fungerar inte efter WordPress-uppdateringen.")
                err.emailed = True
                raise err
            db.event(site_id, "warning", "WordPress-uppdateringen fick sajten att gå sönder – återställd", f"Återställd via {how}.")
            rolled_back.extend(core_items)

    refresh_status(site_id, job_id, refresh=False)

    after_shot = None
    if before_shot:
        progress(job_id, "Skärmdump efter uppdateringen …")
        after_shot = shots.take(site_id, row["url"])
        if after_shot and updated and not rolled_back:
            d = shots.difference(before_shot, after_shot)
            if d is not None and d >= shots.CHANGED:
                db.event(site_id, "warning", "Startsidan ser annorlunda ut efter uppdateringen",
                         f"Sidan svarar normalt, men {round(d * 100)} % av skärmdumpen har ändrats jämfört med före uppdateringen. "
                         "Det kan vara ett bildspel som bytt bild – eller något som gått sönder. Jämför bilderna på sajtens sida.")

    if updated:
        db.event(site_id, "ok", f"Uppdaterade {len(updated)} st",
                 "\n".join(f"{r['name']} {r.get('before') or ''} → {r.get('after') or ''}" for r in updated))
    if failed:
        db.event(site_id, "warning", f"{len(failed)} uppdateringar misslyckades",
                 "\n".join(f"{r['name']}: {r.get('message', '')}" for r in failed))
    if rolled_back:
        names = ", ".join(_label(i) for i in rolled_back)
        db.event(site_id, "warning", "Återställd: " + names,
                 "Uppdateringen kraschade sajten och togs tillbaka. Den hoppas över tills du låser upp den eller uppdaterar manuellt.")
        skip = set(json.loads(db.site(site_id)["skip_plugins"] or "[]"))
        skip.update(i["slug"] for i in rolled_back if i["type"] in ("plugin", "theme", "core"))
        db.run("UPDATE sites SET skip_plugins = ? WHERE id = ?", json.dumps(sorted(skip)), site_id)
        mailer.send(f"Uppdatering återställd – {row['name']}",
                    f"Uppdateringen av {names} fick {row['url']} att gå sönder. Sajtvakt återställde den automatiskt och sajten fungerar igen.\n\n"
                    + (f"Övriga uppdateringar gick igenom: {', '.join(r['name'] for r in updated)}.\n\n" if updated else "")
                    + "Den är nu låst i Sajtvakt så att den inte provas igen varje natt. Lås upp den i panelen när en rättad version finns.")
    if failed and not rolled_back:
        mailer.send(f"Uppdateringar misslyckades – {row['name']}",
                    "\n".join(f"{r['name']}: {r.get('message', '')}" for r in failed))

    return {
        "updated": [f"{r['name']} {r.get('before') or ''} → {r.get('after') or ''}" for r in updated],
        "rolled_back": [_label(i) for i in rolled_back],
        "failed": [f"{r['name']}: {r.get('message', '')}" for r in failed],
        "notes": notes,
        "shot": bool(after_shot),
    }


def do_remove(site_id: int, kind: str, slug: str, job_id: int | None = None) -> dict:
    """Avinstallera ett tillägg eller tema. Pluginet sparar en kopia först, så det kan läggas tillbaka."""
    row = db.site(site_id)
    s = client(row)
    progress(job_id, "Sparar en kopia och tar bort …")
    maint = bool(row["maintenance"])
    baseline = check_http(row["url"], maint)
    res = s.request("remove", {"type": kind, "slug": slug, "ref": secrets.token_hex(16)}, timeout=600)
    name = res.get("name") or slug
    refresh_status(site_id, job_id, refresh=False)
    after = check_http(row["url"], maint)
    what = "tillägget" if kind == "plugin" else "temat"
    if baseline["ok"] and not healthy(after, baseline):
        db.event(site_id, "error", f"Sajten fungerar inte efter att {name} togs bort",
                 f"Lägg tillbaka {what} under \"Före uppdateringar\" på sajtens sida (det blir då inaktivt och behöver aktiveras), "
                 "eller återställ från backupen.")
        mailer.send(f"Trasig efter avinstallation – {row['name']}",
                    f"{row['url']} svarar inte som den ska efter att {name} togs bort.\n\n{config.PUBLIC_URL}/sajt/{site_id}")
    else:
        db.event(site_id, "ok", f"Avinstallerade {name}", f"En kopia av {what} finns under \"Före uppdateringar\" om du ångrar dig.")
    skip = set(json.loads(db.site(site_id)["skip_plugins"] or "[]"))
    if slug in skip:
        skip.discard(slug)
        db.run("UPDATE sites SET skip_plugins = ? WHERE id = ?", json.dumps(sorted(skip)), site_id)
    return {"message": f"{name} är borttaget"}


def do_shot(site_id: int, job_id: int | None = None) -> dict:
    row = db.site(site_id)
    progress(job_id, "Tar skärmdump av startsidan …")
    if not shots.take(site_id, row["url"]):
        raise SajtvaktError("shot_failed", "Kunde inte ta en skärmdump av startsidan.")
    return {"message": "Skärmdump tagen"}


def do_manual_rollback(site_id: int, snapshot: str, job_id: int | None = None) -> dict:
    row = db.site(site_id)
    how = _rollback(client(row), snapshot=snapshot)
    refresh_status(site_id, job_id, refresh=False)
    db.event(site_id, "ok", "Uppdatering återställd manuellt", f"Ögonblicksbild {snapshot} via {how}.")
    return {"message": f"Återställd via {how}"}


# ====================================================================== push (restore / migrate)

def do_push(backup_id: int, target_id: int, mode: str, job_id: int | None = None) -> dict:
    b = db.one("SELECT * FROM backups WHERE id = ?", backup_id)
    src = db.site(b["site_id"])
    target = db.site(target_id)
    if not b or b["status"] != "done" or not b["path"]:
        raise SajtvaktError("bad_backup", "Backupen finns inte eller är ofullständig.")

    # Skyddsnät: backup av målet innan något skrivs över.
    progress(job_id, "Tar en säkerhetsbackup av målsajten först …")
    do_backup(target_id, "pre-restore", job_id, note=f"Före {'återställning' if mode == 'restore' else 'flytt från ' + src['name']}")

    work = config.TMP_DIR / f"push-{backup_id}-{target_id}-{int(time.time())}"
    try:
        progress(job_id, "Förbereder backupen …")
        store.build_package(Path(b["path"]), work)

        def prog(j):
            if j.get("phase") == "upload" and "file" in j:
                progress(job_id, f"Skickar {j['file']} ({fmt_bytes(j['received'])} av {fmt_bytes(j['size'])})")
            elif j.get("progress"):
                p = j["progress"]
                pct = f" {p['percent']} %" if p.get("percent") is not None else ""
                progress(job_id, f"{p.get('text', j.get('phase'))}{pct}")

        res = client(target).push_backup(str(work), mode=mode, progress=prog, budget=step_budget(target))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    when = datetime.fromtimestamp(b["created"], config.TZ).strftime("%Y-%m-%d %H:%M")
    if mode == "migrate":
        db.event(target_id, "ok", f"Flyttad hit från {src['name']}", f"Backup från {when}. {res['result'].get('changed', 0)} databasrader fick ny adress.")
        if src["id"] != target_id:
            db.event(src["id"], "info", f"Kopierad till {target['name']}", f"Backup från {when}.")
    else:
        db.event(target_id, "ok", "Återställd från backup", f"Backup från {when}.")

    refresh_status(target_id, job_id, refresh=False)
    check = check_http(target["url"], bool(target["maintenance"]))
    if not check["ok"]:
        db.event(target_id, "warning", "Sajten svarar inte som väntat efter återställningen", check.get("error") or f"HTTP {check['code']}")
    return {"message": "Klart", "home": res["result"].get("home"), "check": check}


# ====================================================================== nightly & housekeeping

def nightly(site_id: int, job_id: int | None = None) -> dict:
    out: dict = {}
    do_backup(site_id, "nightly", job_id)
    out["backup"] = "klar"
    row = db.site(site_id)
    if row["auto_update"] and db.setting("auto_update") == "1":
        out["update"] = do_update(site_id, job_id)
    if not (out.get("update") or {}).get("shot"):
        progress(job_id, "Tar skärmdump av startsidan …")
        shots.take(site_id, row["url"])
    return out


def prune(site_id: int) -> int:
    days = int(db.setting("retention_days") or 30)
    now = int(time.time())
    newest = db.one("SELECT id FROM backups WHERE site_id=? AND status='done' ORDER BY created DESC LIMIT 1", site_id)
    keep_id = newest["id"] if newest else -1
    old = db.q(
        "SELECT * FROM backups WHERE site_id=? AND id != ? AND ("
        " (kind != 'manual' AND created < ?) OR (kind = 'manual' AND created < ?) OR (status = 'error' AND created < ?))",
        site_id, keep_id, now - days * 86400, now - 90 * 86400, now - 7 * 86400,
    )
    for b in old:
        store.delete_backup(b["path"])
        db.run("DELETE FROM backups WHERE id = ?", b["id"])
    db.run("DELETE FROM events WHERE site_id = ? AND created < ?", site_id, now - 180 * 86400)
    db.run("DELETE FROM jobs WHERE site_id = ? AND created < ?", site_id, now - 30 * 86400)
    return len(old)


def delete_site(site_id: int) -> None:
    for b in db.q("SELECT path FROM backups WHERE site_id = ?", site_id):
        store.delete_backup(b["path"])
    shutil.rmtree(store.BACKUPS / str(site_id), ignore_errors=True)
    shots.delete(site_id)
    db.run("DELETE FROM events WHERE site_id = ?", site_id)
    db.run("DELETE FROM jobs WHERE site_id = ?", site_id)
    db.run("DELETE FROM sites WHERE id = ?", site_id)
