"""Klient för Sajtvakt-pluginet (grunden till Sajtvakt-servern).

Signerar varje anrop med sajtens nyckel (HMAC-SHA256) och sköter
backup (start → steg → nedladdning) och push av backup (återställ/flytta).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from typing import Callable


class SajtvaktError(Exception):
    def __init__(self, code: str, message: str, http: int | None = None, data: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.http, self.data = code, message, http, data or {}


def parse_connection(conn: str) -> tuple[str, str]:
    """'sv1:…' från sajtens admin → (url, nyckel)."""
    if not conn.startswith("sv1:"):
        raise ValueError("Okänt nyckelformat (ska börja med sv1:)")
    raw = conn[4:]
    raw += "=" * (-len(raw) % 4)
    data = json.loads(base64.urlsafe_b64decode(raw))
    return data["u"], data["k"]


class Site:
    def __init__(self, url: str, key: str, timeout: int = 120, endpoint: str = "admin-post",
                 rescue_url: str | None = None):
        self.url = url if url.endswith("/") else url + "/"
        self.key = key
        self.timeout = timeout
        self.endpoint = endpoint  # "admin-post" eller "frontend" (reserv om wp-admin är låst)
        self.rescue_url = rescue_url  # pluginets nödingång, rapporteras av sajten

    @classmethod
    def from_connection(cls, conn: str, **kw) -> "Site":
        url, key = parse_connection(conn)
        return cls(url, key, **kw)

    # ------------------------------------------------------------------ signing

    def _sign(self, op: str, params: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        to_sign = "\n".join([
            "sajtvakt-v1", op, ts, nonce,
            hashlib.sha256(params.encode()).hexdigest(),
            hashlib.sha256(body).hexdigest(),
        ])
        sig = hmac.new(self.key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        h = {"X-Sajtvakt-Timestamp": ts, "X-Sajtvakt-Nonce": nonce, "X-Sajtvakt-Signature": sig}
        if params:
            h["X-Sajtvakt-Params"] = params
        return h

    def _endpoint(self, op: str, rescue: bool) -> str:
        if rescue:
            base = self.rescue_url or f"{self.url}wp-content/plugins/sajtvakt/rescue.php"
            return f"{base}?op={op}"
        if self.endpoint == "frontend":
            return f"{self.url}?sajtvakt-api={op}"
        return f"{self.url}wp-admin/admin-post.php?action=sajtvakt&op={op}"

    def request(self, op: str, params: dict | None = None, *, raw: bytes | None = None,
                rescue: bool = False, binary: bool = False, timeout: int | None = None):
        if raw is not None:
            params_s = base64.urlsafe_b64encode(json.dumps(params or {}).encode()).decode().rstrip("=")
            body = raw
            ctype = "application/octet-stream"
        else:
            params_s, body = "", json.dumps(params or {}).encode()
            ctype = "application/json"
        signed_op = f"rescue_{op}" if rescue else op
        headers = {"Content-Type": ctype, "User-Agent": "Sajtvakt-server/0.1", **self._sign(signed_op, params_s, body)}
        req = urllib.request.Request(self._endpoint(op, rescue), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                data = r.read()
                if binary:
                    return data, dict(r.headers)
                return self._decode(data, r.status)
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                j = json.loads(data)
            except Exception:
                raise SajtvaktError("http_error", f"HTTP {e.code}: {data[:200]!r}", e.code) from None
            if isinstance(j, dict) and "job" in j and "status" in j:
                return j  # Jobbstatus med ett fel i – anroparen läser det.
            if isinstance(j, dict) and isinstance(j.get("error"), str):
                if "ok" in j:  # Our own error format: raise with its code.
                    raise SajtvaktError(j["error"], j.get("message", ""), e.code, j) from None
            if isinstance(j, dict) and "ok" in j and e.code < 500:
                return j
            raise SajtvaktError("site_error", f"HTTP {e.code} från sajten (WordPress svarar inte normalt)", e.code, j if isinstance(j, dict) else None) from None

    @staticmethod
    def _decode(data: bytes, status: int):
        try:
            return json.loads(data)
        except Exception:
            raise SajtvaktError("bad_response", f"Inte JSON (HTTP {status}): {data[:200]!r}", status) from None

    def _retry(self, fn, attempts: int = 4, server_errors: bool = False):
        """Tål tillfälliga nätverksfel, 'upptagen' (förra steget kör fortfarande) och –
        för steg som tål att köras om – att PHP dödades mitt i (5xx)."""
        for i in range(attempts):
            try:
                return fn()
            except SajtvaktError as e:
                last = i == attempts - 1
                if e.code == "busy" and not last:
                    time.sleep(15)
                    continue
                if server_errors and e.code in ("site_error", "http_error", "bad_response") and not last:
                    time.sleep(10 * (i + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if i == attempts - 1:
                    raise SajtvaktError("network", f"Nätverksfel mot sajten: {e}") from None
                time.sleep(5 * (i + 1))

    # ------------------------------------------------------------------ helpers

    def ping(self):
        return self.request("ping")

    def status(self, refresh: bool = False):
        return self.request("status", {"refresh": refresh})

    def backup(self, dest_dir: str, *, files: bool = True, database: bool = True,
               budget: int = 20, chunk: int = 8 * 1024 * 1024,
               progress: Callable[[dict], None] | None = None) -> dict:
        """Kör en hel backup och laddar ner allt till dest_dir. Returnerar manifestet."""
        os.makedirs(dest_dir, exist_ok=True)
        job = self.request("backup_start", {"files": files, "database": database})
        jid = job["job"]
        try:
            fetched: set[str] = set()
            while True:
                job = self._retry(lambda: self.request("backup_step", {"job": jid, "budget": budget}, timeout=budget + 120), server_errors=True)
                if progress:
                    progress(job)
                if job["status"] == "error":
                    raise SajtvaktError("backup_failed", job.get("error") or "okänt fel")
                # Hämta färdiga delar direkt och låt sajten radera dem (sparar diskutrymme där).
                for out in job["outputs"]:
                    self._safe_path(dest_dir, out["name"])
                    if out["name"] not in fetched and out["name"] != "manifest.json":
                        self._download(jid, out, dest_dir, chunk)
                        fetched.add(out["name"])
                        self.request("backup_discard", {"job": jid, "file": out["name"]})
                if job["status"] == "done":
                    break
            man = next(o for o in job["outputs"] if o["name"] == "manifest.json")
            self._download(jid, man, dest_dir, chunk)
            with open(os.path.join(dest_dir, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
            for out in manifest.get("outputs", []):
                self._safe_path(dest_dir, out["name"])
            return manifest
        finally:
            try:
                self.request("backup_finish", {"job": jid})
            except Exception:
                pass

    SAFE_NAME = re.compile(r"^(database\.sql\.gz|files-\d{3}\.zip|manifest\.json)$")

    def _safe_path(self, dest_dir: str, name: str) -> str:
        """Filnamn kommer från sajten – en hackad sajt får aldrig kunna skriva utanför backupmappen."""
        if not isinstance(name, str) or not self.SAFE_NAME.match(name):
            raise SajtvaktError("bad_name", f"Sajten skickade ett otillåtet filnamn: {name!r}")
        base = os.path.realpath(dest_dir)
        path = os.path.realpath(os.path.join(base, name))
        if os.path.dirname(path) != base:
            raise SajtvaktError("bad_name", f"Otillåten sökväg: {name!r}")
        return path

    def _download(self, jid: str, out: dict, dest_dir: str, chunk: int):
        path = self._safe_path(dest_dir, out["name"])
        h = hashlib.sha256()
        with open(path, "wb") as f:
            offset = 0
            while offset < out["size"]:
                data, _ = self._retry(lambda: self.request(
                    "backup_download", {"job": jid, "file": out["name"], "offset": offset, "length": chunk}, binary=True))
                if not data:
                    raise SajtvaktError("download_failed", f"Tomt svar för {out['name']} vid {offset}")
                f.write(data)
                h.update(data)
                offset += len(data)
        if h.hexdigest() != out["sha256"]:
            raise SajtvaktError("checksum", f"Kontrollsumman stämmer inte för {out['name']}")

    def push_backup(self, backup_dir: str, *, mode: str = "restore", budget: int = 20,
                    progress: Callable[[dict], None] | None = None) -> dict:
        """Skicka en nedladdad backup till sajten. mode = 'restore' eller 'migrate'."""
        with open(os.path.join(backup_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        start = self.request("restore_start", {"manifest": manifest, "mode": mode})
        if not start.get("ok", True) or "job" not in start:
            raise SajtvaktError(start.get("error", "restore_failed"), start.get("message", ""))
        jid, chunk = start["job"], int(start["chunk_size"])
        try:
            for out in manifest["outputs"]:
                if out["name"] == "manifest.json":
                    continue
                path = os.path.join(backup_dir, out["name"])
                with open(path, "rb") as f:
                    offset, failures = 0, 0
                    while offset < out["size"]:
                        f.seek(offset)
                        data = f.read(chunk)
                        try:
                            r = self.request("restore_upload", {"job": jid, "file": out["name"], "offset": offset}, raw=data)
                            offset, failures = r["received"], 0
                        except SajtvaktError as e:
                            if e.http == 413 and chunk > 256 * 1024:
                                chunk //= 2  # Webbservern tar inte emot så stora bitar – halvera.
                                continue
                            if e.code == "bad_offset":
                                offset = int(e.data.get("expected", offset))
                                continue
                            raise
                        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                            failures += 1
                            if failures > 3:
                                raise SajtvaktError("network", f"Uppladdningen avbröts: {e}") from None
                            time.sleep(5 * failures)
                            continue
                        if progress:
                            progress({"phase": "upload", "file": out["name"], "received": offset, "size": out["size"]})
            while True:
                job = self._retry(lambda: self.request("restore_step", {"job": jid, "budget": budget}, timeout=budget + 400), server_errors=True)
                if progress:
                    progress(job)
                if job["status"] == "error":
                    raise SajtvaktError("restore_failed", job.get("error") or "okänt fel")
                if job["status"] == "done":
                    return job
        except Exception:
            # Be sajten städa. Pluginet vägrar själv om något redan är inbytt eller ett steg fortfarande kör.
            try:
                self.request("restore_cancel", {"job": jid})
            except Exception:
                pass
            raise

    def update(self, items: list[dict], refresh: bool = True, ref: str = ""):
        return self.request("update", {"items": items, "refresh": refresh, "ref": ref}, timeout=900)

    def rollback(self, snapshot: str = "", rescue: bool = False, ref: str = ""):
        return self.request("rollback", {"snapshot": snapshot, "ref": ref}, rescue=rescue)
