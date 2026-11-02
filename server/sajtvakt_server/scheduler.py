"""Klockan: nattlig backup + uppdatering, upptidskoll, status och städning."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import config, db, jobs, mailer, shots, store

log = logging.getLogger("sajtvakt.scheduler")

STATUS_EVERY = 6 * 3600
_stop = threading.Event()
last_tick = 0.0  # När tick() senast gick klart – /halsa larmar om den blir gammal.
_monitor_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="upptid")


def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _once_per_day(key: str, at_hour: int) -> bool:
    """Sant en gång per dag, första gången efter at_hour (lokal tid)."""
    now = datetime.now(config.TZ)
    if now.hour < at_hour:
        return False
    if db.setting(f"ran:{key}") == _today():
        return False
    db.set_setting(f"ran:{key}", _today())
    return True


def tick() -> None:
    now = int(time.time())
    local = datetime.now(config.TZ)
    backup_hour = int(db.setting("backup_hour") or 2)

    # Upptid – parallellt, så att en sajt som hänger inte försenar de andra.
    due = [r for r in db.sites() if not r["checked_at"] or now - r["checked_at"] >= config.MONITOR_INTERVAL]

    def _monitor(row):
        try:
            jobs.monitor_site(row["id"])
        except Exception as e:
            log.warning("Upptidskoll %s: %s", row["url"], e)

    list(_monitor_pool.map(_monitor, due))

    # Wordfence-skanningar som Sajtvakt startat: läs av var tredje minut tills de är klara.
    def _poll(row):
        try:
            jobs.poll_security(row["id"])
        except Exception as e:
            log.warning("Säkerhetsavläsning %s: %s", row["url"], e)
    polling = []
    for r in db.sites():
        if jobs.sec_pending(r["id"]) and now - int(db.setting(f"sec_poll:{r['id']}") or 0) >= 180:
            db.set_setting(f"sec_poll:{r['id']}", str(now))
            polling.append(r)
    list(_monitor_pool.map(_poll, polling))

    for row in db.sites():
        sid = row["id"]

        # Nattlig körning: en gång per dygn efter backuptimmen (sajterna köas, max N parallellt).
        if local.hour >= backup_hour and db.setting(f"nightly:{sid}") != _today() and not jobs.busy(sid):
            db.set_setting(f"nightly:{sid}", _today())
            jobs.submit("nightly", sid, lambda j, sid=sid: jobs.nightly(sid, j))
            continue

        # Färsk status (uppdateringar, versioner) några gånger per dygn.
        if (not row["status_at"] or now - row["status_at"] >= STATUS_EVERY) and not jobs.busy(sid):
            jobs.submit("refresh", sid, lambda j, sid=sid: (jobs.refresh_status(sid, j), {"message": "Status hämtad"})[1])
            continue

        # Saknas skärmdump (ny sajt, eller precis uppgraderad server): ta en, högst var 6:e timme.
        if shots.available() and not shots.taken_at(sid) and not jobs.busy(sid):
            tried = int(db.setting(f"shot_try:{sid}") or 0)
            if now - tried >= 6 * 3600:
                db.set_setting(f"shot_try:{sid}", str(now))
                jobs.submit("shot", sid, lambda j, sid=sid: jobs.do_shot(sid, j))

    # Städning och SSL en gång per dygn, efter nattkörningen – men aldrig medan en backup pågår
    # (städningen av lagret får inte ta filer som en pågående backup just återanvänt).
    running = db.one("SELECT 1 FROM jobs WHERE status IN ('queued','running')") or db.one("SELECT 1 FROM backups WHERE status='running'")
    if not running and _once_per_day("housekeeping", min(23, backup_hour + 3)):
        for row in db.sites():
            try:
                removed = jobs.prune(row["id"])
                if removed:
                    log.info("Rensade %s gamla backuper för %s", removed, row["url"])
                jobs.check_ssl(row["id"])
                if local.weekday() == 0:  # En gång i veckan: nödingång och skyddad datamapp.
                    jobs.check_safety_nets(row["id"])
            except Exception as e:
                log.warning("Städning %s: %s", row["url"], e)
        res = store.gc()
        log.info("Lagerstädning: %s", res)
        db.set_setting("store_usage_at", "0")  # Städningen ändrade lagret – räkna om siffran.
        disk = shutil.disk_usage(config.DATA_DIR)
        if disk.free < 0.1 * disk.total or disk.free < 5 * 1024**3:
            db.event(None, "warning", "Lite diskutrymme kvar på Sajtvakt-servern", f"{jobs.fmt_bytes(disk.free)} ledigt av {jobs.fmt_bytes(disk.total)}.")
            mailer.send("Disken börjar bli full", f"Sajtvakt-servern har bara {jobs.fmt_bytes(disk.free)} ledigt. Minska antal dagar backuper sparas eller utöka disken, annars kan nattens backuper misslyckas.")


def loop() -> None:
    log.info("Schemaläggaren startar (backup kl %s, %s)", db.setting("backup_hour"), config.TZ)
    # Jobb som var igång när servern stängdes av blir aldrig klara – markera dem.
    # Nattkörningar som avbröts får sin dagmarkör nollställd, annars hoppas den sajtens backup
    # över i natt utan nytt försök (t.ex. efter en omstart eller uppgradering mitt i körningen).
    for r in db.q("SELECT DISTINCT site_id FROM jobs WHERE status IN ('queued','running') AND kind='nightly' AND site_id IS NOT NULL"):
        db.run("DELETE FROM settings WHERE key = ?", f"nightly:{r['site_id']}")
    db.run("UPDATE jobs SET status='error', error='Avbrutet (servern startades om)', finished=? WHERE status IN ('queued','running')", int(time.time()))
    db.run("UPDATE backups SET status='error', error='Avbrutet (servern startades om)' WHERE status='running'")
    while not _stop.is_set():
        try:
            tick()
            global last_tick
            last_tick = time.time()
        except Exception as e:
            log.exception("Schemaläggarfel: %s", e)
        _stop.wait(30)


def start() -> None:
    if config.SCHEDULER_ENABLED:
        global last_tick
        last_tick = time.time()  # Så att /halsa inte larmar innan första ticken hunnit gå.
        threading.Thread(target=loop, name="schemalaggare", daemon=True).start()


def stop() -> None:
    _stop.set()
