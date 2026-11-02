"""E-postlarm."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from . import config, db

log = logging.getLogger("sajtvakt.mail")


last_error = ""


def smtp() -> dict:
    """Inställningarna från panelen gäller i första hand, annars de i .env."""
    if db.setting("smtp_host"):
        return {
            "host": db.setting("smtp_host"),
            "port": int(db.setting("smtp_port") or 587),
            "user": db.setting("smtp_user"),
            "password": db.setting("smtp_password"),
            "sender": db.setting("smtp_from") or db.setting("smtp_user"),
            "tls": db.setting("smtp_tls") or "starttls",
        }
    return dict(config.SMTP)


def configured() -> bool:
    cfg = smtp()
    return bool(cfg["host"] and cfg["sender"] and db.setting("alert_email"))


def send(subject: str, body: str, to: str | None = None) -> bool:
    global last_error
    cfg = smtp()
    to = to or db.setting("alert_email")
    if not (cfg["host"] and cfg["sender"] and to):
        last_error = "E-post är inte inställd (server, avsändare eller mottagare saknas)."
        log.info("E-post ej konfigurerad, skulle ha skickat: %s", subject)
        return False
    try:
        # Ämnesraden kan innehålla sajtnamn som sajten själv bestämmer – rensa styrtecken,
        # annars kraschar mejlbygget (och larmet uteblir) på ett namn med radbrytning i.
        clean = "".join(ch for ch in str(subject) if ch >= " " and ch != "\x7f").strip()
        msg = EmailMessage()
        msg["Subject"] = f"Sajtvakt: {clean or 'larm'}"
        msg["From"] = cfg["sender"]
        msg["To"] = to
        msg.set_content(body + f"\n\n— Sajtvakt\n{config.PUBLIC_URL}\n")
        if cfg["tls"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ssl.create_default_context(), timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            if cfg["tls"] == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        last_error = ""
        return True
    except Exception as e:  # Ett larm som inte går iväg ska inte stoppa resten.
        last_error = str(e)
        log.error("Kunde inte skicka e-post: %s", e)
        db.event(None, "warning", "E-postlarm kunde inte skickas", str(e))
        return False
