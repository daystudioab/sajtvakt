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


LONGDAYS = ["Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"]
LONGMONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"]


def _longdate(value):
    d = datetime.fromtimestamp(int(value), config.TZ)
    return f"{LONGDAYS[d.weekday()]} {d.day} {LONGMONTHS[d.month - 1]}"


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
templates.env.filters["longdate"] = _longdate
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
    '<svg viewBox="0 0 38 38" fill="none" aria-hidden="true"><rect x="2" y="2" width="34" height="34" rx="11" fill="currentColor" '
    'opacity=".16"/><rect x="2" y="2" width="34" height="34" rx="11" stroke="currentColor" stroke-width="2"/>'
    '<path d="M8 20h5l3-7 5 12 3-6h6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
# Ikoner: Lucide (ISC-licens, lucide.dev) – 24 px rutnät, färgen ärvs (currentColor).
_ICON_PATHS = {
    "updates": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "backup": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    "plugins": '<path d="M12 22v-5"/><path d="M15 8V2"/><path d="M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z"/><path d="M9 8V2"/>',
    "copies": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
    "screen": '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
    "status": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "events": '<path d="M3 5h.01"/><path d="M3 12h.01"/><path d="M3 19h.01"/><path d="M8 5h13"/><path d="M8 12h13"/><path d="M8 19h13"/>',
    "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    "sites": '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
    "log": '<path d="m7 11 2-2-2-2"/><path d="M11 13h4"/><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>',
    "mail": '<path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/><rect x="2" y="4" width="20" height="16" rx="2"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "search": '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    "logout": '<path d="m16 17 5-5-5-5"/><path d="M21 12H9"/><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "shield-ok": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    "shield-alert": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "warn": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "down": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "rollback": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "maint": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>',
    "trash": '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "storage": '<path d="M10 16h.01"/><path d="M2.212 11.577a2 2 0 0 0-.212.896V18a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5.527a2 2 0 0 0-.212-.896L18.55 5.11A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><path d="M21.946 12.013H2.054"/><path d="M6 16h.01"/>',
    "chev": '<path d="m9 18 6-6-6-6"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
}


def _icon(name: str, cls: str = "ic") -> Markup:
    return Markup(f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + _ICON_PATHS[name] + "</svg>")


templates.env.globals["icon"] = _icon
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
                "AND e.title NOT IN ('Sajten är nere') AND e.title NOT LIKE 'SSL-%' ORDER BY e.id DESC LIMIT 200", int(time.time()) - 7 * 86400)
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


SEVERITY = {"bad": 0, "warn": 1, "busy": 2, "maint": 3, "ok": 4}


def _sec_summary(st: dict | None, site_id: int) -> dict:
    """Säkerhetsläget (Wordfence) som en kort etikett för listan: kind = ok | warn | bad | busy | none."""
    def out(kind, label, title="", icon="shield"):
        return {"kind": kind, "label": label, "title": title or label, "icon": icon}
    if not st:
        return out("none", "–", "Ingen status hämtad än")
    sec = st.get("security")
    if sec is None:
        return out("none", "–", "Visas när sajten har Sajtvakt-tillägget 0.4.0 (uppdateras automatiskt i natt)")
    if not sec.get("installed"):
        return out("none", "Ingen Wordfence", "Wordfence är inte installerat")
    if not sec.get("active"):
        return out("warn", "Wordfence av", "Wordfence är installerat men inte aktiverat", "shield-alert")
    if sec.get("error"):
        return out("none", "Okänt", sec["error"])
    scan = sec.get("scan") or {}
    issues = jobs.real_issues(sec)
    if scan.get("running"):
        return out("busy", "Skannar …", "Wordfence skannar sajten just nu")
    if issues:
        severe = any(i["severity"] in ("critical", "high") for i in issues)
        n = len(issues)
        return out("bad" if severe else "warn", f"{n} problem",
                   "; ".join(i["title"] for i in issues[:3]) + (" …" if n > 3 else ""), "shield-alert")
    if not scan.get("last"):
        return out("none", "Ej skannad", "Wordfence har inte skannat sajten än")
    if scan.get("result") not in (None, "ok"):
        return out("warn", "Skanning misslyckades", scan["result"], "shield-alert")
    days = (time.time() - scan["last"]) / 86400
    when = datetime.fromtimestamp(scan["last"], config.TZ)
    if days > 8:
        return out("warn", f"Ej skannad på {int(days)} d", f"Senaste skanningen {when:%Y-%m-%d}", "shield-alert")
    return out("ok", "Säker", f"Wordfence hittade inga problem vid senaste skanningen {when:%Y-%m-%d %H:%M}", "shield-ok")


def _site_summary(row, backup_days=None, rolled_days=None, today=None, backup_hour=2) -> dict:
    st = jobs.status_of(row)
    n_updates = 0
    if st:
        u = st["updates"]
        n_updates = u["plugins"] + u["themes"] + (1 if u["core"] else 0)
    running = db.one("SELECT * FROM jobs WHERE site_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", row["id"])
    locked = len(json.loads(row["skip_plugins"] or "[]"))
    out = {"row": row, "st": st, "updates": n_updates, "job": running, "locked": locked,
           "shot": shots.url_for(row["id"]), "shot_at": shots.taken_at(row["id"]), "sec": _sec_summary(st, row["id"])}
    if today is None:
        return out
    nights = _nights(row, backup_days or set(), rolled_days or set(), today, backup_hour, n=7)
    out["nights"] = nights
    ok = sum(1 for x in nights if x["kind"] in ("ok", "roll"))
    counted = sum(1 for x in nights if x["kind"] not in ("none", "wait"))
    out["nights_label"] = f"Backup {ok} av {counted} nätter"
    now = int(time.time())
    stale = bool(row["last_backup"]) and now - row["last_backup"] > 36 * 3600 and now - row["added"] > 36 * 3600
    never = not row["last_backup"]
    in_maint = bool(row["maintenance"]) and (row["up"] != 0 or row["last_code"] == 503)
    # Läge (färg) och etikett. SSL följs i bakgrunden och mejlas, men visas inte här.
    if row["up"] == 0 and not in_maint:
        tone, label, kind = "bad", "Nere", "bad"
    elif in_maint:
        tone, label, kind = "maint", "Underhållsläge", "maint"
    elif running:
        tone, label, kind = "busy", f"{jobs.JOB_NAMES.get(running['kind'], running['kind'])} pågår", "info"
    elif locked:
        tone, label, kind = "warn", f"{locked} låst", "warn"
    elif stale or (never and now - row["added"] > 3600):
        tone, label, kind = "warn", "Backup saknas", "warn"
    elif n_updates:
        # Väntande uppdateringar: gul cirkel, men räknas inte som att sajten behöver dig.
        tone, label, kind = "ok", f"{n_updates} uppd.", "warn"
    elif st:
        tone, label, kind = "ok", "Aktuell", "ok"
    else:
        tone, label, kind = "ok", "Väntar på status", ""
    out.update(tone=tone, label=label, kind=kind, severity=SEVERITY[tone])
    parts = []
    if row["last_backup"]:
        lb = datetime.fromtimestamp(row["last_backup"], config.TZ)
        parts.append(f"Backup {lb:%H:%M}" if now - row["last_backup"] < 86400 else f"Backup {_ago(row['last_backup'])}")
    if row["up"] == 1 and row["response_ms"] and not in_maint:
        parts.append(f"{row['response_ms']} ms")
    if n_updates and label != f"{n_updates} uppd.":
        parts.append(f"{n_updates} uppd.")
    out["meta"] = " · ".join(parts)
    return out


def _group_summary(name: str, members: list[dict]) -> dict:
    """Samlad status för en grupp: det sämsta läget bland sajterna bestämmer."""
    bad = sum(1 for m in members if m["tone"] == "bad")
    warn = sum(1 for m in members if m["tone"] == "warn" or (m["tone"] != "bad" and m["sec"]["kind"] in ("warn", "bad")))
    maint = sum(1 for m in members if m["tone"] == "maint")
    updates = sum(m["updates"] for m in members)
    if bad:
        label, kind = (f"{bad} nere", "bad")
    elif warn:
        label, kind = (f"{warn} behöver dig", "warn")
    elif maint == len(members):
        label, kind = ("Underhållsläge", "maint")
    else:
        label, kind = ("Allt OK", "ok")
    meta = f"{len(members)} {'sajt' if len(members) == 1 else 'sajter'}"
    if maint and kind != "maint":
        meta += f" · {maint} i underhåll"
    if updates:
        meta += f" · {updates} uppd."
    return {"name": name, "sites": members, "label": label, "kind": kind, "meta": meta,
            "severity": min((m["severity"] for m in members), default=4)}


def _needs(sites: list[dict], attention: list[dict]) -> list[dict]:
    """Rutorna under "Behöver dig": sajter som är nere först, sedan okvitterade varningar."""
    out = []
    for s in sites:
        r = s["row"]
        if s["tone"] == "bad":
            out.append({"kind": "bad", "icon": "down", "site_id": r["id"], "title": f"{r['name']} är nere",
                        "detail": ("Svarar med en underhållssida (503). Är det med flit? Slå på underhållsläge, så larmar Sajtvakt "
                                   "bara om sajten slutar svara helt eller visar fel.") if r["last_code"] == 503
                        else f"Svarar inte som den ska{' (' + r['last_error'] + ')' if r['last_error'] else ''}. Sajtvakt kollar var 5:e minut och mejlar när den är uppe.",
                        "when": f"sedan {_ts(r['down_since'], '%H:%M')}" if r["down_since"] else "",
                        "maint_button": r["last_code"] == 503, "url": r["url"]})
    for a in attention:
        if a["title"] == "Sajten är nere":
            continue
        roll = a["title"].startswith("Återställd") or "återställd" in a["title"]
        out.append({"kind": "warn" if a["level"] == "warning" else "bad", "icon": "rollback" if roll else "warn",
                    "site_id": a["site_id"], "title": a["title"], "site_name": a["site_name"],
                    "detail": a["detail"] or "", "when": _when(a["created"]), "ids": a["ids"]})
    return out


LOG_ICONS = {"ok": "check", "info": "events", "warning": "warn", "error": "down"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, sort: str = "status"):
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
                  "(title LIKE 'Återställd:%' OR title LIKE 'Uppdateringen fick sajten%' OR title LIKE 'WordPress-uppdateringen fick%')", since):
        rolled_days.setdefault(e["site_id"], set()).add(day(e["created"]))
    sites = [_site_summary(r, backup_days.get(r["id"]), rolled_days.get(r["id"]), today, backup_hour) for r in db.sites()]
    by_name = sort == "namn"
    key = (lambda s: s["row"]["name"].lower()) if by_name else (lambda s: (s["severity"], s["row"]["name"].lower()))
    sites.sort(key=key)

    named: dict[str, list] = {}
    loose = []
    for s in sites:
        g = (s["row"]["group_name"] or "").strip()
        (named.setdefault(g, []) if g else loose).append(s)
    groups = [_group_summary(n, m) for n, m in named.items()]
    groups.sort(key=(lambda g: g["name"].lower()) if by_name else (lambda g: (g["severity"], g["name"].lower())))
    if loose:
        groups.append(_group_summary("Utan grupp" if named else "", loose))

    attention = _attention()
    needs = _needs(sites, attention)
    log = db.q("SELECT e.*, s.name AS site_name FROM events e LEFT JOIN sites s ON s.id = e.site_id "
               "WHERE e.title != 'Backup klar' AND e.title NOT LIKE 'SSL-%' ORDER BY e.id DESC LIMIT 6")
    night = {
        "backups": db.one("SELECT COUNT(*) AS n FROM backups WHERE kind='nightly' AND status='done' AND created > ?", now - 86400)["n"],
        "updated": sum(int(m.group(1)) for e in db.q("SELECT title FROM events WHERE created > ? AND title LIKE 'Uppdaterade % st'", now - 86400)
                       if (m := re.match(r"Uppdaterade (\d+) st", e["title"]))),
    }
    down = sum(1 for s in sites if s["tone"] == "bad")
    totals = {
        "sites": len(sites),
        "up": len(sites) - down,
        "down": down,
        "updates": sum(s["updates"] for s in sites),
        "fresh": sum(1 for s in sites if s["row"]["last_backup"] and now - s["row"]["last_backup"] < 26 * 3600),
        "stored": store.usage(),
    }
    return render(request, "dashboard.html", sites=sites, groups=groups, needs=needs, log=log, night=night,
                  totals=totals, mail_ok=mailer.configured(), backup_hour=backup_hour, sort="namn" if by_name else "status",
                  any_busy=any(s["job"] for s in sites), shots_on=shots.available(), LOG_ICONS=LOG_ICONS)


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
    skip_names = {"wordpress": "WordPress"}
    for p in (st or {}).get("plugins", []):
        skip_names[p["file"]] = p["name"]
    for t in (st or {}).get("themes", []):
        skip_names[t["slug"]] = t["name"]
    fore_at = shots.taken_at(site_id, "fore")
    show_fore = bool(fore_at and time.time() - fore_at < 14 * 86400)
    sec = (st or {}).get("security")
    return render(request, "site.html", site=row, st=st, sec=sec, sec_sum=_sec_summary(st, site_id),
                  sec_issues=jobs.real_issues(sec), sec_updates=len(((sec or {}).get("issues") or {}).get("items", [])) - len(jobs.real_issues(sec)),
                  sec_pending=jobs.sec_pending(site_id), sec_mode=db.setting("security_scan") or "weekly", backups=backups, events=events, running=running,
                  recent_jobs=recent_jobs, others=others, skip=skip, pending=pending, notes=notes, skip_names=skip_names,
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
    return _start(site_id, "update", lambda j: jobs.do_update(site_id, j, only=only),
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


@app.post("/sajt/{site_id}/sakerhet")
def site_security_scan(request: Request, site_id: int, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    sec = (jobs.status_of(row) or {}).get("security")
    if sec is None:
        return back(f"/sajt/{site_id}", "Sajten behöver Sajtvakt-tillägget 0.4.0 för säkerhetsskanning. "
                    "Det uppdateras automatiskt i natt – eller klicka Hämta status och sedan Uppdatera på raden Sajtvakt.", bad=True)
    if not sec.get("active"):
        return back(f"/sajt/{site_id}", "Wordfence är inte installerat och aktiverat på sajten.", bad=True)
    return _start(site_id, "security", lambda j: _scan_job(site_id),
                  "Startar Wordfence-skanning … Resultatet visas här när den är klar, oftast inom 5–20 minuter.")


def _scan_job(site_id: int) -> dict:
    r = jobs.start_security_scan(site_id)
    if r.get("error"):
        raise RuntimeError(r["error"])
    return {"message": "Skanning startad" if r.get("started") else (r.get("message") or "En skanning pågår redan")}


@app.post("/sakerhet/skanna-alla")
def security_scan_all(request: Request, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    n = 0
    for row in db.sites():
        sec = (jobs.status_of(row) or {}).get("security") or {}
        if sec.get("active") and not jobs.busy(row["id"]):
            jobs.submit("security", row["id"], lambda j, sid=row["id"]: _scan_job(sid))
            n += 1
    if not n:
        return back("/installningar", "Ingen sajt med aktivt Wordfence (och Sajtvakt 0.4.0) att skanna just nu.", bad=True)
    return back("/installningar", f"Startar Wordfence-skanning på {n} {'sajt' if n == 1 else 'sajter'}. Resultaten dyker upp i översikten.")


def _version_at_least(have: str, need: str) -> bool:
    def parts(v):
        return [int(x) for x in re.findall(r"\d+", v)[:3]] + [0, 0, 0]
    return parts(have)[:3] >= parts(need)[:3]


templates.env.globals["version_at_least"] = _version_at_least


@app.post("/sajt/{site_id}/avinstallera")
def site_remove(request: Request, site_id: int, csrf_token: str = Form(""), kind: str = Form(""), slug: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    st = jobs.status_of(row) or {}
    have = (st.get("versions") or {}).get("sajtvakt") or "0"
    if not _version_at_least(have, "0.3.1"):
        return back(f"/sajt/{site_id}", f"Sajten har Sajtvakt-tillägget {have}, och avinstallation kräver 0.3.1. "
                    "Klicka Hämta status och sedan Uppdatera på raden Sajtvakt under Uppdateringar.", bad=True)
    if kind == "plugin":
        item = next((p for p in st.get("plugins", []) if p["file"] == slug), None)
        if item and slug.split("/")[0] == "sajtvakt":
            return back(f"/sajt/{site_id}", "Sajtvakt kan inte avinstallera sig självt härifrån.", bad=True)
    elif kind == "theme":
        item = next((t for t in st.get("themes", []) if t["slug"] == slug), None)
        if item and (item.get("active") or item.get("parent")):
            return back(f"/sajt/{site_id}", "Temat används av sajten och kan inte tas bort.", bad=True)
    else:
        item = None
    if not item:
        return back(f"/sajt/{site_id}", "Hittar det inte på sajten – hämta status och försök igen.", bad=True)
    return _start(site_id, "remove", lambda j: jobs.do_remove(site_id, kind, slug, j), f"Tar bort {item['name']} …")


@app.post("/sajt/{site_id}/las")
def site_lock(request: Request, site_id: int, csrf_token: str = Form(""), slug: str = Form(""), lock: str = Form("1")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    skip = set(json.loads(row["skip_plugins"] or "[]"))
    (skip.add if lock == "1" else skip.discard)(slug)
    db.run("UPDATE sites SET skip_plugins = ? WHERE id = ?", json.dumps(sorted(skip)), site_id)
    return back(f"/sajt/{site_id}", "Låst – uppdateras inte automatiskt." if lock == "1" else "Upplåst – uppdateras automatiskt igen.")


def _set_maintenance(row, on: bool) -> None:
    if bool(row["maintenance"]) == on:
        return
    db.run("UPDATE sites SET maintenance = ? WHERE id = ?", 1 if on else 0, row["id"])
    if on and row["up"] == 0 and row["last_code"] in (503, None):
        # Nere för att den visade underhållssidan – räknas som uppe direkt, så att varningen försvinner.
        db.run("UPDATE sites SET up = 1, fails = 0, down_since = NULL WHERE id = ?", row["id"])
    db.event(row["id"], "info", "Underhållsläge påslaget" if on else "Underhållsläge avstängt",
             "Underhållssidan (503) räknas inte som nere. Larm går fortfarande om sajten inte svarar alls eller visar fel."
             if on else "")
    # Kolla direkt, så att översikten visar rätt läge utan att vänta på nästa koll.
    try:
        jobs.monitor_site(row["id"])
    except Exception as e:
        log.warning("Koll efter underhållsläge: %s", e)


@app.post("/sajt/{site_id}/installningar")
def site_settings(request: Request, site_id: int, csrf_token: str = Form(""), name: str = Form(""), auto_update: str = Form("0"),
                  maintenance: str = Form("0"), group: str = Form(""), group_new: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    if group == "__ny__":
        group = group_new
    db.run("UPDATE sites SET name = ?, auto_update = ?, group_name = ? WHERE id = ?", name.strip() or row["name"],
           1 if auto_update == "1" else 0, " ".join(group.split())[:60], site_id)
    _set_maintenance(row, maintenance == "1")
    return back(f"/sajt/{site_id}", "Sparat.")


def _groups() -> list[dict]:
    return [dict(r) for r in db.q("SELECT group_name AS name, COUNT(*) AS n FROM sites WHERE group_name != '' "
                                  "GROUP BY group_name ORDER BY group_name COLLATE NOCASE")]


templates.env.globals["all_groups"] = _groups


@app.post("/grupper/byt-namn")
def group_rename(request: Request, csrf_token: str = Form(""), old: str = Form(""), new: str = Form("")):
    csrf(request, csrf_token)
    new = " ".join(new.split())[:60]
    db.run("UPDATE sites SET group_name = ? WHERE group_name = ?", new, old)
    return back("/installningar", f"Gruppen {old} heter nu {new}." if new else f"Gruppen {old} är borttagen – sajterna ligger kvar utan grupp.")


@app.post("/sajt/{site_id}/underhall")
def site_maintenance(request: Request, site_id: int, csrf_token: str = Form(""), on: str = Form("1"), to: str = Form("")):
    csrf(request, csrf_token)
    row = _site_or_404(site_id)
    _set_maintenance(row, on == "1")
    return back("/" if to == "oversikt" else f"/sajt/{site_id}",
                f"Underhållsläge påslaget för {row['name']} – underhållssidan larmar inte längre." if on == "1"
                else f"Underhållsläge avstängt för {row['name']}.")


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


@app.post("/uppdatera-alla")
def update_all(request: Request, csrf_token: str = Form("")):
    """Uppdatera allt på alla sajter nu (samma säkra väg som natten: backup först, återställ det som går sönder)."""
    csrf(request, csrf_token)
    started, busy = [], []
    for row in db.sites():
        st = jobs.status_of(row)
        if not st:
            continue
        pending, _notes = jobs.pending_updates(row, st)
        if not pending:
            continue
        if jobs.busy(row["id"]):
            busy.append(row["name"])
            continue
        jobs.submit("update", row["id"], lambda j, sid=row["id"]: jobs.do_update(sid, j))
        started.append(row["name"])
    if not started:
        msg = "Inget att uppdatera just nu." if not busy else f"Jobb pågår redan på {', '.join(busy)} – försök igen när de är klara."
        return back("/", msg, bad=bool(busy))
    msg = f"Uppdaterar {len(started)} {'sajt' if len(started) == 1 else 'sajter'}: {', '.join(started)}. En backup tas först av varje."
    if busy:
        msg += f" Hoppade över {', '.join(busy)} (jobb pågår)."
    return back("/", msg)


@app.post("/backup/{backup_id}/ta-bort")
def backup_delete(request: Request, backup_id: int, csrf_token: str = Form("")):
    csrf(request, csrf_token)
    b = db.one("SELECT * FROM backups WHERE id = ?", backup_id)
    if not b:
        return back("/", "Backupen finns inte.", bad=True)
    url = f"/sajt/{b['site_id']}"
    if b["status"] == "running":
        return back(url, "Backupen pågår fortfarande – vänta tills den är klar.", bad=True)
    # En backup som just nu används för en återställning eller flytt får inte försvinna.
    if jobs.busy(b["site_id"]) or db.one("SELECT 1 FROM jobs WHERE kind IN ('restore','migrate') AND status IN ('queued','running')"):
        return back(url, "Ett jobb pågår – ta bort backupen när det är klart.", bad=True)
    store.delete_backup(b["path"])
    db.run("DELETE FROM backups WHERE id = ?", backup_id)
    if b["status"] == "done":
        kind = templates.env.globals["KIND_NAMES"].get(b["kind"], b["kind"])
        db.event(b["site_id"], "info", "Backup borttagen", f"{_ts(b['created'])} ({kind}, {jobs.fmt_bytes(b['size'])})")
        return back(url, "Backupen är borttagen. Utrymmet frigörs vid nattens städning (filer som andra backuper använder ligger kvar).")
    return back(url, "Den misslyckade backupen är borttagen ur listan.")


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
                  s={k: db.setting(k) for k in ("backup_hour", "retention_days", "alert_email", "auto_update", "security_scan")},
                  smtp=mailer.smtp(), smtp_in_panel=bool(db.setting("smtp_host")), usage=store.usage(), tz=str(config.TZ))


@app.post("/installningar")
def settings_save(request: Request, csrf_token: str = Form(""), backup_hour: int = Form(2), retention_days: int = Form(30),
                  alert_email: str = Form(""), auto_update: str = Form("0"), security_scan: str = Form("weekly")):
    csrf(request, csrf_token)
    db.set_setting("backup_hour", str(max(0, min(23, backup_hour))))
    db.set_setting("retention_days", str(max(3, min(365, retention_days))))
    db.set_setting("alert_email", alert_email.strip())
    db.set_setting("auto_update", "1" if auto_update == "1" else "0")
    db.set_setting("security_scan", security_scan if security_scan in ("off", "weekly", "daily") else "weekly")
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


# ====================================================================== tillägg på alla sajter

def _inventory() -> tuple[list[dict], list]:
    """Alla tillägg på alla sajter, grupperade per tilläggsmapp (samma tillägg = samma mapp)."""
    sites = db.sites()
    groups: dict[str, dict] = {}
    for row in sites:
        st = jobs.status_of(row)
        if not st:
            continue
        for p in st.get("plugins", []):
            key = p["file"].split("/")[0]
            g = groups.setdefault(key, {"key": key, "names": {}, "on": []})
            g["names"][p["name"]] = g["names"].get(p["name"], 0) + 1
            g["on"].append({"site": row, "version": p["version"], "active": p["active"],
                            "new": (p.get("update") or {}).get("version"), "locked": p["file"] in json.loads(row["skip_plugins"] or "[]")})
    known = [r for r in sites if r["status_json"]]
    out = []
    for g in groups.values():
        g["name"] = max(g["names"], key=g["names"].get)
        have = {x["site"]["id"] for x in g["on"]}
        g["missing"] = [r for r in known if r["id"] not in have]
        g["on"].sort(key=lambda x: x["site"]["name"].lower())
        g["active_count"] = sum(1 for x in g["on"] if x["active"])
        g["updates"] = sum(1 for x in g["on"] if x["new"])
        out.append(g)
    out.sort(key=lambda g: (-len(g["on"]), g["name"].lower()))
    return out, [r for r in sites if not r["status_json"]]


@app.get("/tillagg", response_class=HTMLResponse)
def plugins_page(request: Request, q: str = ""):
    auth(request)
    groups, unknown = _inventory()
    q = q.strip()
    if q:
        ql = q.lower()
        matches = [g for g in groups if ql in g["name"].lower() or ql in g["key"].lower()]
    else:
        matches = []
    total_sites = len(db.sites()) - len(unknown)
    return render(request, "plugins.html", q=q, groups=groups, matches=matches, unknown=unknown, total_sites=total_sites)


@app.get("/handelser", response_class=HTMLResponse)
def events_page(request: Request):
    auth(request)
    events = db.q("SELECT e.*, s.name AS site_name FROM events e LEFT JOIN sites s ON s.id = e.site_id ORDER BY e.id DESC LIMIT 300")
    return render(request, "events.html", events=events)
