"""Panelen (FastAPI + Jinja). Kör: uvicorn sajtvakt_server.web:app"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import socket
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

from . import __version__, config, db, jobs, mailer, scheduler, shots, store
from .client import SajtvaktError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sajtvakt.web")

HERE = Path(__file__).parent
app = FastAPI(title="Sajtvakt", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

SESSION_COOKIE = "sv_session"
SESSION_DAYS = 30


# ====================================================================== template helpers

def _ts(value, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(int(value), config.TZ).strftime(fmt) if value else "–"


def _ago(value):
    if not value:
        return "aldrig"
    d = int(time.time()) - int(value)
    if d < 60:
        return "nyss"
    if d < 3600:
        return f"{d // 60} min sedan"
    if d < 86400:
        return f"{d // 3600} tim sedan"
    days = d // 86400
    return "i går" if days == 1 else f"{days} dagar sedan"


DAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def _dayline(value):
    d = datetime.fromtimestamp(int(value), config.TZ)
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} · {d:%H:%M}"


def _when(value):
    """Kort tidsangivelse för loggar: "i natt 02:22", "i går 06:00", "fre 14:35", "12 okt"."""
    if not value:
        return "–"
    d = datetime.fromtimestamp(int(value), config.TZ)
    days = (datetime.now(config.TZ).date() - d.date()).days
    if days == 0:
        return ("i natt " if d.hour < 6 else "i dag ") + f"{d:%H:%M}"
    if days == 1:
        return f"i går {d:%H:%M}"
    if days < 7:
        return f"{DAYS[d.weekday()]} {d:%H:%M}"
    return f"{d.day} {MONTHS[d.month - 1]}"


def _host(url):
    return (url or "").split("//")[-1].strip("/")


templates.env.filters["ts"] = _ts
templates.env.filters["dayline"] = _dayline
templates.env.filters["when"] = _when
templates.env.filters["host"] = _host
templates.env.filters["ago"] = _ago
templates.env.filters["bytes"] = jobs.fmt_bytes
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
templates.env.globals["JOB_NAMES"] = jobs.JOB_NAMES
templates.env.globals["SHIELD"] = Markup(
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<path d="M12 2.5 4.5 5.3v5.9c0 4.7 3.2 8.9 7.5 10.3 4.3-1.4 7.5-5.6 7.5-10.3V5.3z" fill="currentColor" opacity=".16"/>'
    '<path d="M12 2.5 4.5 5.3v5.9c0 4.7 3.2 8.9 7.5 10.3 4.3-1.4 7.5-5.6 7.5-10.3V5.3z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'
    '<path d="m8.6 12.2 2.3 2.3 4.6-4.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
templates.env.globals["LOGO"] = Markup(
    '<svg viewBox="0 0 28 28" fill="none" aria-hidden="true"><rect x="1.5" y="1.5" width="25" height="25" rx="7" '
    'stroke="currentColor" stroke-width="2"/><path d="M6 15h4l2-5 4 9 2-4h4" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
templates.env.globals["LEVELS"] = {"error": "FEL", "warning": "VARN", "ok": "OK", "info": "INFO"}
templates.env.globals["VERSION"] = __version__
templates.env.globals["KIND_NAMES"] = {
    "nightly": "Nattlig", "pre-update": "Före uppdatering", "pre-restore": "Före återställning", "manual": "Manuell",
}


# ====================================================================== auth

def _hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(_hash_password(pw, bytes.fromhex(salt)).split("$")[2], h)
    except Exception:
        return False


_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Caddy sätter X-Real-IP till den verkliga adressen och skriver över det klienten skickar.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


_failures_lock = threading.Lock()


def _throttled(ip: str) -> tuple[bool, bool]:
    """(spärrad, långsammare): max 5 fel per adress och kvart. Många fel totalt gör alla
    försök långsammare i stället för att låsa ute dig."""
    now = time.time()
    with _failures_lock:
        for k in list(_failures):
            _failures[k] = [t for t in _failures[k] if now - t < 900]
            if not _failures[k]:
                del _failures[k]
        total = sum(len(v) for v in _failures.values())
        return len(_failures.get(ip, [])) >= 5, total >= 20


def _record_failure(ip: str) -> None:
    with _failures_lock:
        _failures.setdefault(ip, []).append(time.time())


def _session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE token = ?", hashlib.sha256(token.encode()).hexdigest())
    if not row or time.time() - row["seen"] > SESSION_DAYS * 86400:
        return None
    if time.time() - row["seen"] > 300:
        db.run("UPDATE sessions SET seen = ? WHERE token = ?", int(time.time()), row["token"])
    return row


class NeedLogin(Exception):
    pass


class BadCsrf(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/logga-in", status_code=303)


@app.exception_handler(BadCsrf)
async def _bad_csrf(request: Request, exc: BadCsrf):
    return HTMLResponse("Formuläret har gått ut – gå tillbaka och ladda om sidan.", status_code=400)


def auth(request: Request):
    s = _session(request)
    if not s:
        raise NeedLogin()
    return s


def csrf(request: Request, token: str):
    s = auth(request)
    if not hmac.compare_digest(s["csrf"], token or ""):
        raise BadCsrf()
    return s


def _attention() -> list[dict]:
    """Varningar som inte kvitterats, senaste veckan. Upprepningar (t.ex. SSL varje dag) slås ihop."""
    rows = db.q("SELECT e.*, s.name AS site_name FROM events e LEFT JOIN sites s ON s.id = e.site_id "
                "WHERE e.acked = 0 AND e.level IN ('warning','error') AND e.created > ? "
                "AND e.title NOT IN ('Sajten är nere') ORDER BY e.id DESC LIMIT 200", int(time.time()) - 7 * 86400)
    groups: dict = {}
    for r in rows:
        key = (r["site_id"], re.sub(r"\d+", "#", r["title"]))
        if key in groups:
            groups[key]["ids"].append(r["id"])
        else:
            groups[key] = {**dict(r), "ids": [r["id"]]}
    return list(groups.values())


def _side() -> dict:
    """Det som visas i sidomenyn på alla sidor."""
    now = datetime.now(config.TZ)
    hour = int(db.setting("backup_hour") or 2)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    mins = int((nxt - now).total_seconds() // 60)
    disk = shutil.disk_usage(config.DATA_DIR)
    used = round(100 * (disk.total - disk.free) / disk.total) if disk.total else 0
    return {"next_run": f"{nxt:%H:%M} · om {mins // 60} h {mins % 60:02d} m", "disk_pct": used,
            "disk_warn": used >= 90, "mail_ok": mailer.configured(), "attention": len(_attention())}


def render(request: Request, template_name: str, **ctx):
    s = _session(request)
    ctx.update(request=request, csrf=s["csrf"] if s else "", flash=request.query_params.get("m"),
               flash_bad=request.query_params.get("f") == "bad", now=int(time.time()))
    if s:
        ctx.setdefault("side", _side())
    return templates.TemplateResponse(request, template_name, ctx)


def back(url: str, msg: str = "", bad: bool = False):
    """Tillbaka till en sida med ett meddelande – rött om bad=True."""
    from urllib.parse import quote
    if msg:
        url += ("&" if "?" in url else "?") + "m=" + quote(msg) + ("&f=bad" if bad else "")
    return RedirectResponse(url, status_code=303)


# ====================================================================== lifecycle

@app.on_event("startup")
def _startup():
    db.init()
    if config.ADMIN_PASSWORD and not db.one("SELECT 1 FROM settings WHERE key='admin_hash'"):
        db.set_setting("admin_hash", _hash_password(config.ADMIN_PASSWORD))
        log.info("Administratörslösenord sparat från SAJTVAKT_ADMIN_PASSWORD.")
    scheduler.start()


@app.on_event("shutdown")
def _shutdown():
    scheduler.stop()


@app.get("/halsa")
def health():
    return {"ok": True}


# ====================================================================== login

@app.get("/logga-in", response_class=HTMLResponse)
def login_form(request: Request):
    configured = bool(db.one("SELECT 1 FROM settings WHERE key='admin_hash'"))
    return render(request, "login.html", configured=configured, error=None)


@app.post("/logga-in")
def login(request: Request, password: str = Form("")):
    ip = _client_ip(request)
    stored = db.setting("admin_hash")
    blocked, slow = _throttled(ip)
    if blocked:
        return render(request, "login.html", configured=True, error="För många försök. Vänta en kvart.")
    if slow:
        time.sleep(3)
    if not stored or not _check_password(password, stored):
        _record_failure(ip)
        time.sleep(1)
        return render(request, "login.html", configured=bool(stored), error="Fel lösenord.")
    token = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token, created, seen, csrf) VALUES(?,?,?,?)",
           hashlib.sha256(token.encode()).hexdigest(), int(time.time()), int(time.time()), secrets.token_urlsafe(24))
    db.run("DELETE FROM sessions WHERE seen < ?", int(time.time()) - SESSION_DAYS * 86400)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=config.SECURE_COOKIES)
    return resp


@app.post("/logga-ut")
def logout(request: Request, csrf_token: str = Form("")):
    s = csrf(request, csrf_token)
    db.run("DELETE FROM sessions WHERE token = ?", s["token"])
    resp = RedirectResponse("/logga-in", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ====================================================================== dashboard

def _nights(row, backup_days: set, rolled_days: set, today, backup_hour: int, n: int = 14) -> list[dict]:
    """En ruta per natt: backup klar, uppdatering återställd, ingen backup – eller före sajten fanns."""
    added = datetime.fromtimestamp(row["added"], config.TZ).date()
    out = []
    for i in range(n - 1, -1, -1):
        d = today - timedelta(days=i)
        label = f"{d.day} {MONTHS[d.month - 1]}"
        if d < added:
            out.append({"kind": "none", "title": f"{label}: inte tillagd än"})
        elif d in rolled_days:
            out.append({"kind": "roll", "title": f"{label}: backup klar, en uppdatering återställdes"})
        elif d in backup_days:
            out.append({"kind": "ok", "title": f"{label}: backup klar"})
        elif d == today and datetime.now(config.TZ).hour <= backup_hour:
            out.append({"kind": "wait", "title": f"{label}: körs i natt"})
        else:
            out.append({"kind": "miss", "title": f"{label}: ingen backup"})
    return out


def _site_summary(row, backup_days=None, rolled_days=None, today=None, backup_hour=2) -> dict:
    st = jobs.status_of(row)
    n_updates = 0
    if st:
        u = st["updates"]
        n_updates = u["plugins"] + u["themes"] + (1 if u["core"] else 0)
    running = db.one("SELECT * FROM jobs WHERE site_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", row["id"])
    out = {"row": row, "st": st, "updates": n_updates, "job": running,
           "locked": len(json.loads(row["skip_plugins"] or "[]")),
           "shot": shots.url_for(row["id"]), "shot_at": shots.taken_at(row["id"])}
    if today is not None:
        nights = _nights(row, backup_days or set(), rolled_days or set(), today, backup_hour)
        ok = sum(1 for x in nights if x["kind"] in ("ok", "roll"))
        counted = sum(1 for x in nights if x["kind"] not in ("none", "wait"))
        out["nights"] = nights
        out["nights_label"] = f"Backup {ok} av {counted} nätter"
        now = int(time.time())
        stale = row["last_backup"] and now - row["last_backup"] > 36 * 3600
        recent_trouble = any(x["kind"] in ("miss", "roll") for x in nights[-2:])
        if row["up"] == 0:
            out["tone"] = "bad"
        elif (row["ssl_days"] is not None and row["ssl_days"] < 14) or stale or recent_trouble:
            out["tone"] = "warn"
        else:
            out["tone"] = ""
    return out


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    auth(request)
    now = int(time.time())
    today = datetime.now(config.TZ).date()
    backup_hour = int(db.setting("backup_hour") or 2)
    since = now - 15 * 86400
    day = lambda ts: datetime.fromtimestamp(ts, config.TZ).date()  # noqa: E731
    backup_days: dict[int, set] = {}
    for b in db.q("SELECT site_id, created FROM backups WHERE status='done' AND created > ?", since):
        backup_days.setdefault(b["site_id"], set()).add(day(b["created"]))
    rolled_days: dict[int, set] = {}
    for e in db.q("SELECT site_id, created FROM events WHERE created > ? AND site_id IS NOT NULL AND "
                  "(title LIKE 'Återställd:%' OR title LIKE 'Uppdateringen fick sajten%')", since):
        rolled_days.setdefault(e["site_id"], set()).add(day(e["created"]))
    sites = [_site_summary(r, backup_days.get(r["id"]), rolled_days.get(r["id"]), today, backup_hour) for r in db.sites()]

    attention = _attention()
    down = [s["row"] for s in sites if s["row"]["up"] == 0]
    log = db.q("SELECT e.*, s.name AS site_name FROM events e LEFT JOIN sites s ON s.id = e.site_id "
               "WHERE e.title != 'Backup klar' ORDER BY e.id DESC LIMIT 8")
    night = {
        "backups": db.one("SELECT COUNT(*) AS n FROM backups WHERE kind='nightly' AND status='done' AND created > ?", now - 86400)["n"],
        "updated": sum(int(m.group(1)) for e in db.q("SELECT title FROM events WHERE created > ? AND title LIKE 'Uppdaterade % st'", now - 86400)
                       if (m := re.match(r"Uppdaterade (\d+) st", e["title"]))),
    }
    totals = {
        "sites": len(sites),
        "up": sum(1 for s in sites if s["row"]["up"] == 1),
        "down": len(down),
        "updates": sum(s["updates"] for s in sites),
        "fresh": sum(1 for s in sites if s["row"]["last_backup"] and now - s["row"]["last_backup"] < 26 * 3600),
        "stored": store.usage(),
    }
    return render(request, "dashboard.html", sites=sites, attention=attention, down=down, log=log, night=night,
                  totals=totals, mail_ok=mailer.configured(), backup_hour=backup_hour,
                  any_busy=any(s["job"] for s in sites), shots_on=shots.available())


@app.get("/skarmdump/{site_id}")
def screenshot(request: Request, site_id: int):
    auth(request)
    p = shots.path(site_id)
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/skarmdump/{site_id}/fore")
def screenshot_before(request: Request, site_id: int):
    auth(request)
    p = shots.path(site_id, "fore")
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/handelser/kvittera")
def events_ack(request: Request, csrf_token: str = Form(""), ids: str = Form(""), alla: str = Form("")):
    csrf(request, csrf_token)
    if alla == "1":
        db.run("UPDATE events SET acked = 1 WHERE acked = 0")
        return back("/handelser", "Alla varningar är kvitterade.")
    for i in ids.split(","):
        if i.strip().isdigit():
            db.run("UPDATE events SET acked = 1 WHERE id = ?", int(i))
    return back("/")


# ====================================================================== sites

@app.get("/sajt/ny", response_class=HTMLResponse)
def add_form(request: Request):
    auth(request)
    return render(request, "add_site.html", error=None, connection="", name="")


@app.post("/sajt/ny")
def add_site(request: Request, csrf_token: str = Form(""), connection: str = Form(""), name: str = Form("")):
    csrf(request, csrf_token)
    try:
        sid = jobs.add_site(connection, name)
    except (ValueError, KeyError):
        return render(request, "add_site.html", error="Nyckeln ser inte rätt ut. Kopiera den igen från Verktyg → Sajtvakt på sajten.", connection=connection, name=name)
    except SajtvaktError as e:
        msg = {"unauthorized": "Sajten godkände inte nyckeln. Har den bytts? Kopiera den igen.",
               "bad_timestamp": "Klockan på sajtens server går fel – kontakta webbhotellet."}.get(e.code, f"Kunde inte ansluta: {e.message}")
        return render(request, "add_site.html", error=msg, connection=connection, name=name)
    except Exception as e:
        return render(request, "add_site.html", error=f"Kunde inte nå sajten: {e}", connection=connection, name=name)
    # Första backupen direkt; nattkörningen (med uppdateringar) börjar i natt.
    db.set_setting(f"nightly:{sid}", datetime.now(config.TZ).strftime("%Y-%m-%d"))
    jobs.submit("backup", sid, lambda j: {"backup": jobs.do_backup(sid, "manual", j, note="Första backupen")})
    jobs.submit("refresh", sid, lambda j: (jobs.monitor_site(sid), jobs.check_ssl(sid), {"message": "Kontrollerad"})[2])
    return back(f"/sajt/{sid}", "Sajten är tillagd. Första backupen körs nu.")


def _site_or_404(site_id: int):
    row = db.site(site_id)
    if not row:
        raise HTTPException(status_code=404, detail="Sajten finns inte")
    return row


@app.get("/sajt/{site_id}", response_class=HTMLResponse)
def site_page(request: Request, site_id: int):
    auth(request)
    row = _site_or_404(site_id)
    st = jobs.status_of(row)
    backups = db.q("SELECT * FROM backups WHERE site_id = ? ORDER BY created DESC LIMIT 60", site_id)
    events = db.q("SELECT * FROM events WHERE site_id = ? ORDER BY id DESC LIMIT 40", site_id)
    running = db.q("SELECT * FROM jobs WHERE site_id=? AND status IN ('queued','running') ORDER BY id", site_id)
    recent_jobs = db.q("SELECT * FROM jobs WHERE site_id=? AND status IN ('done','error') ORDER BY id DESC LIMIT 5", site_id)
    others = [s for s in db.sites() if s["id"] != site_id]
    skip = set(json.loads(row["skip_plugins"] or "[]"))
    pending, notes = jobs.pending_updates(row, st, include_major_core=True) if st else ([], [])
    fore_at = shots.taken_at(site_id, "fore")
    show_fore = bool(fore_at and time.time() - fore_at < 14 * 86400)
    return render(request, "site.html", site=row, st=st, backups=backups, events=events, running=running,
                  recent_jobs=recent_jobs, others=others, skip=skip, pending=pending, notes=notes,
                  shot=shots.url_for(site_id), shot_at=shots.taken_at(site_id), shots_on=shots.available(),
                  fore=shots.url_for(site_id, "fore") if show_fore else None, fore_at=fore_at)


def _start(site_id: int, kind: str, fn, msg: str):
    if jobs.busy(site_id):
        return back(f"/sajt/{site_id}", "Ett jobb pågår redan för den här sajten – vänta tills det är klart.", bad=True)
    jobs.submit(kind, site_id, fn)
    return back(f"/sajt/{site_id}", msg)


@app.post("/sajt/{site_id}/skarmdump")
def site_shot(request: Request, site_id: int, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    _site_or_404(site_id)
    return _start(site_id, "shot", lambda j: jobs.do_shot(site_id, j), "Tar en ny skärmdump …")


@app.post("/sajt/{site_id}/backup")
def site_backup(request: Request, site_id: int, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    _site_or_404(site_id)
    return _start(site_id, "backup", lambda j: {"backup": jobs.do_backup(site_id, "manual", j)}, "Backup startad.")


@app.post("/sajt/{site_id}/uppdatera")
def site_update(request: Request, site_id: int, csrf_token: str = Form(""), slug: str = Form("")):
    csrf(request, csrf_token)
    _site_or_404(site_id)
    only = [slug] if slug else None
    return _start(site_id, "update", lambda j: jobs.do_update(site_id, j, only=only, include_major_core=bool(slug)),
                  "Uppdatering startad – backup tas först om det behövs.")


@app.post("/sajt/{site_id}/status")
def site_refresh(request: Request, site_id: int, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    _site_or_404(site_id)
    return _start(site_id, "refresh", lambda j: (jobs.refresh_status(site_id, j), jobs.monitor_site(site_id),
                                                 jobs.check_safety_nets(site_id), {"message": "Status hämtad"})[3],
                  "Hämtar status …")


@app.post("/sajt/{site_id}/aterstall-uppdatering")
def site_rollback(request: Request, site_id: int, csrf_token: str = Form(""), snapshot: str = Form("")):
    csrf(request, csrf_token)
    _site_or_404(site_id)
    return _start(site_id, "rollback", lambda j: jobs.do_manual_rollback(site_id, snapshot, j), "Återställer …")


@app.post("/sajt/{site_id}/las")
def site_lock(request: Request, site_id: int, csrf_token: str = Form(""), slug: str = Form(""), lock: str = Form("1")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    skip = set(json.loads(row["skip_plugins"] or "[]"))
    (skip.add if lock == "1" else skip.discard)(slug)
    db.run("UPDATE sites SET skip_plugins = ? WHERE id = ?", json.dumps(sorted(skip)), site_id)
    return back(f"/sajt/{site_id}", "Låst – uppdateras inte automatiskt." if lock == "1" else "Upplåst – uppdateras automatiskt igen.")


@app.post("/sajt/{site_id}/installningar")
def site_settings(request: Request, site_id: int, csrf_token: str = Form(""), name: str = Form(""), auto_update: str = Form("0")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    db.run("UPDATE sites SET name = ?, auto_update = ? WHERE id = ?", name.strip() or row["name"], 1 if auto_update == "1" else 0, site_id)
    return back(f"/sajt/{site_id}", "Sparat.")


@app.post("/sajt/{site_id}/ny-nyckel")
def site_new_key(request: Request, site_id: int, csrf_token: str = Form(""), connection: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    try:
        from .client import parse_connection
        url, _ = parse_connection(connection.strip())
        if url.rstrip("/") != row["url"].rstrip("/"):
            return back(f"/sajt/{site_id}", "Nyckeln hör till en annan sajt.", bad=True)
        jobs.add_site(connection)
    except Exception as e:
        return back(f"/sajt/{site_id}", f"Nyckeln fungerade inte: {e}", bad=True)
    return back(f"/sajt/{site_id}", "Ny nyckel sparad.")


@app.post("/sajt/{site_id}/ta-bort")
def site_delete(request: Request, site_id: int, csrf_token: str = Form(""), confirm: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    if confirm.strip() != row["name"]:
        return back(f"/sajt/{site_id}", "Skriv sajtens namn exakt för att ta bort den.", bad=True)
    if jobs.busy(site_id):
        return back(f"/sajt/{site_id}", "Vänta tills pågående jobb är klart.", bad=True)
    jobs.delete_site(site_id)
    return back("/", f"{row['name']} och alla dess backuper är borttagna från Sajtvakt.")


# ====================================================================== backups

@app.post("/backup/{backup_id}/push")
def backup_push(request: Request, backup_id: int, csrf_token: str = Form(""), target: int = Form(0), confirm: str = Form("")):
    csrf(request, csrf_token)
    b = db.one("SELECT * FROM backups WHERE id = ?", backup_id)
    if not b or b["status"] != "done":
        return back("/", "Backupen finns inte.", bad=True)
    target_id = target or b["site_id"]
    tgt = db.site(target_id)
    if not tgt:
        return back(f"/sajt/{b['site_id']}", "Välj en målsajt.", bad=True)
    if confirm != "1":
        return back(f"/sajt/{b['site_id']}", "Bekräfta att målsajten ska skrivas över.", bad=True)
    mode = "restore" if target_id == b["site_id"] else "migrate"
    return _start(target_id, mode, lambda j: jobs.do_push(backup_id, target_id, mode, j),
                  ("Återställning" if mode == "restore" else "Flytt") + " startad. En säkerhetsbackup av sajten tas först.")


@app.get("/backup/{backup_id}/ladda-ner")
def backup_download(request: Request, backup_id: int):
    auth(request)
    b = db.one("SELECT b.*, s.name, s.url FROM backups b JOIN sites s ON s.id = b.site_id WHERE b.id = ?", backup_id)
    if not b or b["status"] != "done":
        return back("/", "Backupen finns inte.", bad=True)
    work = config.TMP_DIR / f"dl-zip-{backup_id}-{secrets.token_hex(4)}"
    pkg = store.build_package(Path(b["path"]), work / "pkg")
    host = b["url"].split("//")[-1].strip("/").replace("/", "_").replace(":", "-")
    name = f"sajtvakt-{host}-{_ts(b['created'], '%Y%m%d-%H%M')}.zip"
    out = work / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in sorted(pkg.iterdir()):
            zf.write(f, f.name)
    shutil.rmtree(pkg, ignore_errors=True)
    return FileResponse(out, filename=name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))


# ====================================================================== jobs (polling)

@app.get("/jobb/{job_id}")
def job_status(request: Request, job_id: int):
    auth(request)
    j = db.one("SELECT * FROM jobs WHERE id = ?", job_id)
    if not j:
        return JSONResponse({"error": "finns inte"}, status_code=404)
    return {"id": j["id"], "status": j["status"], "progress": j["progress"], "error": j["error"],
            "result": json.loads(j["result"]) if j["result"] else None}


# ====================================================================== settings & events

@app.get("/installningar", response_class=HTMLResponse)
def settings_page(request: Request):
    auth(request)
    return render(request, "settings.html",
                  s={k: db.setting(k) for k in ("backup_hour", "retention_days", "alert_email", "auto_update")},
                  smtp=mailer.smtp(), smtp_in_panel=bool(db.setting("smtp_host")), usage=store.usage(), tz=str(config.TZ))


@app.post("/installningar")
def settings_save(request: Request, csrf_token: str = Form(""), backup_hour: int = Form(2), retention_days: int = Form(30),
                  alert_email: str = Form(""), auto_update: str = Form("0")):
    csrf(request, csrf_token)
    db.set_setting("backup_hour", str(max(0, min(23, backup_hour))))
    db.set_setting("retention_days", str(max(3, min(365, retention_days))))
    db.set_setting("alert_email", alert_email.strip())
    db.set_setting("auto_update", "1" if auto_update == "1" else "0")
    return back("/installningar", "Inställningarna är sparade.")


@app.post("/installningar/testmejl")
def settings_test_mail(request: Request, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    ok = mailer.send("Testmejl", "Det här är ett test. Larmen från Sajtvakt kommer fram.")
    if ok:
        return back("/installningar", f"Testmejlet skickades till {db.setting('alert_email')}.")
    return back("/installningar", f"Kunde inte skicka: {_mail_error(mailer.last_error)}", bad=True)


def _mail_error(err: str) -> str:
    """Gör de vanligaste SMTP-felen begripliga."""
    e = err.lower()
    if "no address associated" in e or "name or service not known" in e or "nodename nor servname" in e:
        return f"servernamnet finns inte – kontrollera stavningen. ({err})"
    if "authentication" in e or "535" in e or "username and password not accepted" in e:
        return f"servern godkände inte användarnamn och lösenord. ({err})"
    if "timed out" in e or "timeout" in e:
        return f"servern svarar inte – fel port eller kryptering? ({err})"
    return err


@app.post("/installningar/epost")
def settings_mail(request: Request, csrf_token: str = Form(""), host: str = Form(""), port: int = Form(587),
                  tls: str = Form("starttls"), user: str = Form(""), password: str = Form(""), sender: str = Form("")):
    csrf(request, csrf_token)
    host = host.strip().lower()
    if host:
        try:
            socket.getaddrinfo(host, port if 0 < port < 65536 else 587, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError):
            hint = " Menade du mailcluster.loopia.se?" if "loopia" in host and host != "mailcluster.loopia.se" else ""
            return back("/installningar", f"Hittar ingen server som heter {host}. Kontrollera stavningen.{hint} Inget sparades.", bad=True)
    if sender.strip() and "@" not in sender:
        return back("/installningar", "Avsändaren ska vara en e-postadress, t.ex. larm@sajtvakt.se. Inget sparades.", bad=True)
    db.set_setting("smtp_host", host.strip())
    db.set_setting("smtp_port", str(port if 0 < port < 65536 else 587))
    db.set_setting("smtp_tls", tls if tls in ("starttls", "ssl", "none") else "starttls")
    db.set_setting("smtp_user", user.strip())
    db.set_setting("smtp_from", sender.strip())
    if password:  # Tomt fält = behåll det sparade lösenordet.
        db.set_setting("smtp_password", password)
    return back("/installningar", "E-postinställningarna är sparade. Skicka ett testmejl för att kontrollera dem.")


@app.post("/installningar/losenord")
def settings_password(request: Request, csrf_token: str = Form(""), current: str = Form(""), new: str = Form(""), new2: str = Form("")):
    s = csrf(request, csrf_token)
    if not _check_password(current, db.setting("admin_hash")):
        return back("/installningar", "Nuvarande lösenord stämmer inte.", bad=True)
    if len(new) < 12 or new != new2:
        return back("/installningar", "Det nya lösenordet måste vara minst 12 tecken och skrivas likadant två gånger.", bad=True)
    db.set_setting("admin_hash", _hash_password(new))
    db.run("DELETE FROM sessions WHERE token != ?", s["token"])
    return back("/installningar", "Lösenordet är bytt. Andra inloggningar har loggats ut.")


@app.get("/handelser", response_class=HTMLResponse)
def events_page(request: Request):
    auth(request)
    events = db.q("SELECT e.*, s.name AS site_name FROM events e LEFT JOIN sites s ON s.id = e.site_id ORDER BY e.id DESC LIMIT 300")
    return render(request, "events.html", events=events)
