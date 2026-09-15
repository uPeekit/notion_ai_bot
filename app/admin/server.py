from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.config import Settings
from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta
from app.notion.discovery import Discovery

log = logging.getLogger(__name__)

_PAGE_HTML = (Path(__file__).parent / "page.html").read_bytes()  # read once at import
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Generous cap for a hand-edited descriptions document; also bounds the blocking rfile.read()
# below so a hostile/huge Content-Length can't force an unbounded read.
MAX_BODY_BYTES = 512 * 1024


def _parse_content_length(raw: str | None) -> int | None:
    """Returns the number of body bytes to read, or None when the header is missing (treated
    as an empty body), non-numeric, negative, or over MAX_BODY_BYTES — the caller must reject
    with 400 without ever calling rfile.read() in the last three cases."""
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        return None
    if length < 0 or length > MAX_BODY_BYTES:
        return None
    return length


def _loopback_host(host_header: str | None) -> bool:
    """DNS-rebinding guard: only accept requests a loopback-only browser tab would send."""
    if not host_header:
        return False
    if host_header.startswith("["):
        host = host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    else:
        host = host_header.split(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def _targets_payload(discovery: Discovery, *, inbox_locked: bool) -> dict[str, Any]:
    snap = discovery.last
    if snap is None:
        return {"targets": [], "inbox_locked": inbox_locked}
    return {
        "fetched_at": snap.fetched_at.isoformat(),
        "inbox_locked": inbox_locked,
        "targets": [
            {
                "id": t.id,
                "kind": t.kind,
                "name": t.name,
                "path": t.path,
                "description": t.description,
                "is_inbox": t.is_inbox,
                "fields": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "type": f.type,
                        "required": f.required,
                        "description": f.description,
                    }
                    for f in t.fields
                ],
            }
            for t in snap.targets
        ],
    }


def _merge_target(existing: TargetMeta, posted: dict[str, Any]) -> TargetMeta:
    fields = dict(existing.fields)
    posted_fields = posted.get("fields") or {}
    for fid, fbody in posted_fields.items():
        cur = fields.get(fid, FieldMeta())
        fields[fid] = FieldMeta(
            name=cur.name,
            description=str(fbody.get("description", cur.description)),
            required=bool(fbody.get("required", cur.required)),
        )
    return TargetMeta(
        name=existing.name,
        description=str(posted.get("description", existing.description)),
        fields=fields,
        inbox=bool(posted.get("inbox", existing.inbox)),
    )


def _apply_descriptions(descriptions: Descriptions, discovery: Discovery, body: Any) -> int:
    """Validates the whole posted document against the last snapshot, merges it into the
    existing yaml (load -> merge -> save, since Descriptions.save replaces the file), and
    returns how many targets actually changed. Raises ValueError on any invalid input; the
    caller turns that into a 400 with nothing written."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    posted_targets = body.get("targets")
    if not isinstance(posted_targets, dict):
        raise ValueError("'targets' must be an object")

    snap = discovery.last
    known_targets = {t.id: t for t in snap.targets} if snap else {}

    for tid, tbody in posted_targets.items():
        target = known_targets.get(tid)
        if target is None:
            raise ValueError(f"unknown target id: {tid}")
        if not isinstance(tbody, dict):
            raise ValueError(f"invalid body for target: {tid}")
        posted_fields = tbody.get("fields") or {}
        if not isinstance(posted_fields, dict):
            raise ValueError(f"invalid fields for target: {tid}")
        known_field_ids = {f.id for f in target.fields}
        for fid in posted_fields:
            if fid not in known_field_ids:
                raise ValueError(f"field {fid!r} does not belong to target {tid!r}")

    meta = descriptions.load()
    merged = dict(meta)
    changed = 0
    for tid, tbody in posted_targets.items():
        existing = merged.get(tid, TargetMeta())
        updated = _merge_target(existing, tbody)
        if updated != existing:
            changed += 1
        merged[tid] = updated

    if sum(1 for tm in merged.values() if tm.inbox) > 1:
        raise ValueError("at most one target may be flagged as inbox")

    if changed:
        descriptions.save(merged)
    return changed


class _AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        settings: Settings,
        discovery: Discovery,
        descriptions: Descriptions,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.app_settings = settings
        self.discovery = discovery
        self.descriptions = descriptions


class _Handler(BaseHTTPRequestHandler):
    server: _AdminHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:  # route stdlib's default access log
        log.info("%s - %s", self.address_string(), fmt % args)

    def _rejects_host(self) -> bool:
        return not _loopback_host(self.headers.get("Host"))

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self._rejects_host():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        if self.path == "/":
            self._send_html(HTTPStatus.OK, _PAGE_HTML)
        elif self.path == "/api/targets":
            inbox_locked = bool(self.server.app_settings.inbox_target_id)
            self._send_json(
                HTTPStatus.OK, _targets_payload(self.server.discovery, inbox_locked=inbox_locked)
            )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self._rejects_host():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        if self.path != "/api/descriptions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        length = _parse_content_length(self.headers.get("Content-Length"))
        if length is None:
            log.warning("rejected POST /api/descriptions: bad Content-Length header")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
            saved = _apply_descriptions(self.server.descriptions, self.server.discovery, body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            log.warning("rejected POST /api/descriptions: %s", e)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        self.server.discovery.invalidate()
        self._send_json(HTTPStatus.OK, {"saved": saved})


class AdminServer:
    """The local admin page: loopback-only HTTP server on a daemon thread. Reads from the
    live Discovery snapshot and writes through Descriptions; never touches Notion itself."""

    def __init__(
        self, settings: Settings, discovery: Discovery, descriptions: Descriptions
    ) -> None:
        self._settings = settings
        self._discovery = discovery
        self._descriptions = descriptions
        self._httpd: _AdminHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("admin server is not started")
        return self._httpd.server_address[1]

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = _AdminHTTPServer(
            ("127.0.0.1", self._settings.admin_ui_port),
            _Handler,
            self._settings,
            self._discovery,
            self._descriptions,
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="admin-http", daemon=True
        )
        self._thread.start()
        log.info("admin page listening on http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None
