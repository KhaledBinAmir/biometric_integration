# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
EBKN FkWeb adapter.

Protocol:
  All requests are POST to /ebkn
  Routing is via custom headers: request_code, dev_id, blk_no, total_blk, trans_id
  Body is JSON + raw binary blobs (BIN_1, BIN_2, ...)

  request_code values:
    realtime_glog      — real-time attendance event
    receive_cmd        — device polls for a pending command
    send_cmd_result    — device reports command execution result
    realtime_enroll_data — new enrollment notification

Block sequencing:
  blk_no=1..N  → intermediate blocks; buffer in Redis
  blk_no=0     → final block; assemble and process
  blk_no absent or payload not chunked → single-block payload

Redis is used for block buffering (multi-worker safe; 5-minute TTL).
"""

from __future__ import annotations

import base64
import json
import struct
import typing
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Tuple

import frappe
from werkzeug.wrappers import Response

from biometric_integration.adapters.base import AbstractDeviceAdapter
from biometric_integration.services.checkin import create_employee_checkin
from biometric_integration.services.command_processor import process_device_command
from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
    get_or_create_user_by_pin,
    save_enrollment_data,
)
from biometric_integration.biometric_integration.doctype.attendance_device_log.attendance_device_log import maybe_log
from biometric_integration.biometric_integration.doctype.attendance_integration_settings.attendance_integration_settings import get_erp_employee_id
from biometric_integration.utils.device_cache import is_registered_device

Reply = Tuple[bytes, int, Dict[str, str]]

REQ_RECV_CMD = "receive_cmd"
REQ_SEND_CMD_RESULT = "send_cmd_result"
REQ_REALTIME_GLOG = "realtime_glog"
REQ_REALTIME_ENROLL = "realtime_enroll_data"


class EBKNAdapter(AbstractDeviceAdapter):

    def dispatch(self) -> Response:
        # EBKN uses underscore headers; Werkzeug lowercases all header names.
        # When going through Nginx without underscore_in_headers on, headers
        # may have been renamed by the proxy. We handle both raw and X-prefixed variants.
        headers = self.request.headers
        request_code = _get_header(headers, "request_code", "x-request-code")
        dev_id = _get_header(headers, "dev_id", "x-dev-id")
        blk_no_raw = _get_header(headers, "blk_no", "x-blk-no")
        trans_id = _get_header(headers, "trans_id", "x-trans-id") or "0"
        cmd_return_code = _get_header(headers, "cmd_return_code", "x-cmd-return-code") or ""

        if not request_code or not dev_id:
            return _build_response("ERROR", trans_id=trans_id)

        blk_no = int(blk_no_raw) if blk_no_raw is not None else None
        raw = self.raw_body

        # --- Block sequencing ---
        if blk_no == 1:
            _cache_start(dev_id, request_code)
            _cache_append(dev_id, request_code, raw)
            return _build_response("OK", trans_id=trans_id)
        if blk_no is not None and blk_no > 1:
            _cache_append(dev_id, request_code, raw)
            return _build_response("OK", trans_id=trans_id)
        if blk_no == 0:
            _cache_append(dev_id, request_code, raw)
            full_payload = _cache_read_clear(dev_id, request_code)
            if full_payload is None:
                return _build_response("ERROR", trans_id=trans_id)
        else:
            full_payload = raw  # single block

        # --- Parse JSON + binaries ---
        try:
            meta, bins = _extract_json_and_bins(full_payload)
            meta = _inline_bins(meta, bins)
        except Exception as exc:
            frappe.log_error(title="EBKN Payload Parse Error", message=str(exc))
            return _build_response("ERROR", trans_id=trans_id)

        meta["device_id"] = dev_id

        # --- Route ---
        ctx = {
            "dev_id": dev_id,
            "trans_id": trans_id,
            "cmd_return_code": cmd_return_code.upper(),
            "blk_no_raw": blk_no_raw,
            "full_payload": full_payload,
            "raw_dump": self.raw_dump(),
        }

        handler = {
            REQ_REALTIME_GLOG: _handle_realtime_glog,
            REQ_RECV_CMD: _handle_receive_cmd,
            REQ_SEND_CMD_RESULT: _handle_send_cmd_result,
            REQ_REALTIME_ENROLL: _handle_realtime_enroll,
        }.get(request_code)

        if handler is None:
            frappe.log_error(title="EBKN Unknown request_code", message=f"request_code={request_code}")
            return _build_response("ERROR", trans_id=trans_id)

        body_bytes, status, resp_headers = handler(meta, ctx)
        return Response(body_bytes, status=status, headers=resp_headers)


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

def _handle_realtime_glog(payload: dict, ctx: dict) -> Reply:
    try:
        dev_id = ctx["dev_id"]
        trans_id = ctx["trans_id"]

        if not is_registered_device(dev_id):
            maybe_log(dev_id, "Error", "IN",
                      f"Attendance from unregistered device dev_id={dev_id} — ignored",
                      raw_data=ctx.get("raw_dump"),
                      force=True)
            return _fail_bytes(), 200, {"response_code": "ERROR"}

        user_id = str(payload.get("user_id", "")).lstrip("0") or str(payload.get("user_id", ""))
        # EBKN sends io_time in UTC; convert to site timezone for storage
        ts = _utc_to_site_local(datetime.strptime(payload["io_time"], "%Y-%m-%d %H:%M:%S"))
        log_type = "IN" if payload.get("io_mode") == 1 else "OUT"

        # EBKN may send verify_type: 1=FP, 4=Face, 15=Card, etc.
        _EBKN_VERIFY = {1: "Fingerprint", 4: "Face", 15: "Card", 6: "Password"}
        verify_raw = payload.get("verify_type") or payload.get("verifyType") or payload.get("auth_type")
        biometric_method = _EBKN_VERIFY.get(int(verify_raw), "Other") if verify_raw is not None else None

        create_employee_checkin(
            device_pin=user_id,
            timestamp=ts,
            device_id=dev_id,
            log_type=log_type,
            biometric_method=biometric_method,
        )
        maybe_log(dev_id, "Attendance", "IN", f"PIN={user_id} {log_type} at {ts}",
                  user_pin=user_id, raw_data=ctx.get("raw_dump"))
        return (_ok_bytes(), 200, _resp_headers(trans_id=trans_id))
    except Exception as exc:
        frappe.log_error(title="EBKN realtime_glog Error", message=str(exc))
        return _fail_bytes(), 400, {"response_code": "ERROR"}


def _handle_receive_cmd(payload: dict, ctx: dict) -> Reply:
    dev_id = ctx["dev_id"]
    trans_id = ctx["trans_id"]
    try:
        _store_ebkn_capabilities(dev_id, payload)
        cmd = process_device_command(dev_id)
        if not cmd:
            return _ok_bytes(), 200, _resp_headers(trans_id=trans_id)
        body_bytes = _format_cmd_body(cmd.get("body"))
        cmd_trans_id = cmd.get("trans_id") or trans_id
        cmd_code = cmd.get("cmd_code", "")
        maybe_log(dev_id, "Command", "OUT", f"cmd_code={cmd_code} trans_id={cmd_trans_id}")
        return body_bytes, 200, _resp_headers(trans_id=cmd_trans_id, cmd_code=cmd_code)
    except Exception as exc:
        frappe.log_error(title="EBKN receive_cmd Error", message=str(exc))
        return _ok_bytes(), 200, _resp_headers(trans_id=trans_id)


def _handle_send_cmd_result(payload: dict, ctx: dict) -> Reply:
    dev_id = ctx["dev_id"]
    trans_id = ctx["trans_id"]
    cmd_return_code = ctx["cmd_return_code"]
    blk_no_raw = ctx["blk_no_raw"]
    full_payload = ctx["full_payload"]

    try:
        cmd_doc = frappe.get_doc("Attendance Device Command", trans_id)
        cmd_doc.no_of_attempts = (cmd_doc.no_of_attempts or 0) + 1
        from frappe.utils import now
        line = f"[{now()}] {cmd_return_code}"
        cmd_doc.device_response = (
            f"{cmd_doc.device_response}\n{line}" if cmd_doc.device_response else line
        )
        from frappe.utils import now_datetime
        if cmd_return_code == "OK":
            # blk_no_raw is None (single-block) or "0" (final assembled block) — both mean done
            if blk_no_raw is None or blk_no_raw == "0":
                cmd_doc.status = "Success"
                cmd_doc.closed_on = now_datetime()
        else:
            # Explicit failure from device — don't retry, mark Failed immediately
            cmd_doc.status = "Failed"
            cmd_doc.closed_on = now_datetime()
        cmd_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # If this was a Get Enroll Data command returning the final blob
        if (
            cmd_doc.command_type == "Get Enroll Data"
            and cmd_return_code == "OK"
            and blk_no_raw is None
        ):
            _store_enrollment_blob(
                dev_id=dev_id,
                user_id=str(payload.get("user_id", "")).lstrip("0") or str(payload.get("user_id", "")),
                blob=full_payload,
            )

        # If this was a Sync User List command, process the returned user IDs
        if (
            cmd_doc.command_type == "Sync User List"
            and cmd_return_code == "OK"
            and (blk_no_raw is None or blk_no_raw == "0")
        ):
            _process_ebkn_user_id_list(dev_id, payload)
    except frappe.DoesNotExistError:
        frappe.log_error(
            title="EBKN send_cmd_result: command not found",
            message=f"trans_id={trans_id} dev_id={dev_id}",
        )
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(title="EBKN send_cmd_result Error", message=str(exc))

    return _ok_bytes(), 200, _resp_headers(trans_id=trans_id)


def _handle_realtime_enroll(payload: dict, ctx: dict) -> Reply:
    dev_id = ctx["dev_id"]
    trans_id = ctx["trans_id"]
    user_id_raw = payload.get("user_id", "")
    user_id = str(user_id_raw).lstrip("0") or str(user_id_raw)

    try:
        if not is_registered_device(dev_id):
            maybe_log(dev_id, "Error", "IN",
                      f"Enrollment from unregistered device dev_id={dev_id} — ignored",
                      raw_data=ctx.get("raw_dump"),
                      force=True)
            return _fail_bytes(), 200, {"response_code": "ERROR"}
        user_doc = get_or_create_user_by_pin(user_id)
        # Ensure device is in user's device list
        if not any(row.attendance_device == dev_id for row in user_doc.get("devices", [])):
            user_doc.append("devices", {
                "attendance_device": dev_id,
                "brand": "EBKN",
                "enroll_data_source": 0,
            })
            user_doc.save(ignore_permissions=True)
            frappe.db.commit()

        # Link employee if not yet linked
        if not user_doc.employee:
            emp = get_erp_employee_id(user_id)
            if emp:
                user_doc.employee = emp
                user_doc.save(ignore_permissions=True)
                frappe.db.commit()

        # Queue Get Enroll Data command to fetch the full template
        _queue_get_enroll_data(dev_id, user_doc.name)
        maybe_log(dev_id, "Enrollment", "IN", f"New enroll event for PIN={user_id}",
                  user_pin=user_id, raw_data=ctx.get("raw_dump"))
        return _ok_bytes(), 200, _resp_headers(trans_id=trans_id)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(title="EBKN realtime_enroll Error", message=str(exc))
        return _fail_bytes(), 400, {"response_code": "ERROR"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _queue_get_enroll_data(dev_id: str, user_doc_name: str) -> None:
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import add_command
    add_command(dev_id, user_doc_name, "EBKN", "Get Enroll Data")


def _store_enrollment_blob(dev_id: str, user_id: str, blob: bytes) -> None:
    """Save EBKN enrollment blob and propagate to other EBKN devices."""
    try:
        doc_name = frappe.db.get_value("Attendance Device User", {"user_id": user_id})
        if not doc_name:
            return
        user_doc = frappe.get_doc("Attendance Device User", doc_name)
        save_enrollment_data(user_doc, "EBKN", dev_id, blob)
        user_doc.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(title="EBKN store_enrollment_blob Error", message=str(exc))


# ---------------------------------------------------------------------------
# Redis block cache (multi-worker safe)
# ---------------------------------------------------------------------------

def _cache_key(dev_id: str, request_code: str) -> str:
    return f"ebkn_block:{dev_id}:{request_code}"


def _cache_start(dev_id: str, request_code: str) -> None:
    """Clear any previous partial data for this sequence."""
    frappe.cache.delete_value(_cache_key(dev_id, request_code))


def _cache_append(dev_id: str, request_code: str, data: bytes) -> None:
    key = _cache_key(dev_id, request_code)
    existing: bytes = frappe.cache.get_value(key) or b""
    frappe.cache.set_value(key, existing + data, expires_in_sec=300)


def _cache_read_clear(dev_id: str, request_code: str) -> bytes | None:
    key = _cache_key(dev_id, request_code)
    data = frappe.cache.get_value(key)
    frappe.cache.delete_value(key)
    return data


# ---------------------------------------------------------------------------
# JSON + binary extraction (EBKN hybrid body format)
# ---------------------------------------------------------------------------

def _extract_json_and_bins(raw: bytes) -> Tuple[dict, Dict[str, bytes]]:
    """Parse EBKN's hybrid body: JSON object followed immediately by raw binary blobs."""
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON opening brace found in EBKN payload")

    brace, end = 0, -1
    for idx, ch in enumerate(text[start:], start=start):
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                end = idx
                break
    if end == -1:
        raise ValueError("Unbalanced JSON braces in EBKN payload")

    meta = json.loads(text[start: end + 1])
    remaining = raw[end + 1:]

    placeholders: List[str] = []

    def _find_bins(obj: Any) -> None:
        if isinstance(obj, dict):
            [_find_bins(v) for v in obj.values()]
        elif isinstance(obj, list):
            [_find_bins(v) for v in obj]
        elif isinstance(obj, str) and obj.startswith("BIN_"):
            placeholders.append(obj)

    _find_bins(meta)

    if not placeholders:
        return meta, {}

    seg_size = len(remaining) // len(placeholders)
    stream = BytesIO(remaining)
    segments: Dict[str, bytes] = {}
    for i, ph in enumerate(placeholders, 1):
        blob = stream.read() if i == len(placeholders) else stream.read(seg_size)
        segments[ph] = blob

    return meta, segments


def _inline_bins(meta: dict, bin_map: Dict[str, bytes]) -> dict:
    """Replace BIN_n placeholders with base64-encoded strings."""
    def _replace(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _replace(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_replace(v) for v in obj]
        if isinstance(obj, str) and obj in bin_map:
            return base64.b64encode(bin_map[obj]).decode()
        return obj
    return _replace(meta)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _resp_headers(trans_id: str = "0", cmd_code: str = "") -> Dict[str, str]:
    h = {"response_code": "OK", "trans_id": str(trans_id), "Content-Type": "application/octet-stream"}
    if cmd_code:
        h["cmd_code"] = cmd_code
    return h


def _ok_bytes() -> bytes:
    return b""


def _fail_bytes() -> bytes:
    return json.dumps({"error": "failed"}).encode()


def _build_response(response_code: str, trans_id: str = "0") -> Response:
    headers = {"response_code": response_code, "trans_id": trans_id}
    return Response(b"", status=200, headers=headers)


def _format_cmd_body(raw: typing.Union[str, bytes, None]) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        payload = raw.encode("utf-8")
        return struct.pack("<I", len(payload) + 1) + payload + b"\x00"
    raise TypeError(f"Unsupported EBKN command body type: {type(raw)}")


def _get_header(headers, *names: str) -> str | None:
    """Try multiple header name variants (underscored, hyphenated, X-prefixed)."""
    for name in names:
        val = headers.get(name)
        if val:
            return val
        # Werkzeug normalises headers to Title-Case internally
        val = headers.get(name.replace("_", "-"))
        if val:
            return val
    return None


def _process_ebkn_user_id_list(dev_id: str, payload: dict) -> None:
    """Process GET_USER_ID_LIST response: create stub users and queue Get Enroll Data.

    EBKN returns a list of user IDs (as ints or strings) under key "user_id_list"
    or similar — the exact key name may vary by firmware. We try common variants.
    """
    from biometric_integration.services.checkin import _ensure_device_user_synced
    user_ids = (
        payload.get("user_id_list")
        or payload.get("userIdList")
        or payload.get("user_ids")
        or []
    )
    if not isinstance(user_ids, list):
        frappe.log_error(
            title="EBKN GET_USER_ID_LIST: unexpected payload format",
            message=f"dev_id={dev_id} payload keys={list(payload.keys())}",
        )
        return

    count = 0
    for raw_id in user_ids:
        pin = str(raw_id).lstrip("0") or str(raw_id)
        try:
            _ensure_device_user_synced(pin, dev_id)
            count += 1
        except Exception as exc:
            frappe.log_error(
                title="EBKN User List Sync Error",
                message=f"dev_id={dev_id} pin={pin}: {exc}",
            )
    maybe_log(dev_id, "Sync", "IN", f"Sync User List: {count} users processed from dev_id={dev_id}")


def _utc_to_site_local(naive_utc: datetime) -> datetime:
    """Convert a naive UTC datetime to a naive site-timezone datetime.

    EBKN devices always send timestamps in UTC.  Frappe stores Employee Checkin
    times as naive datetimes in the site timezone.  This converts UTC → site local
    and strips the tzinfo so Frappe handles it correctly.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        site_tz_name = frappe.utils.get_system_timezone()
        utc_aware = naive_utc.replace(tzinfo=_tz.utc)
        site_aware = utc_aware.astimezone(ZoneInfo(site_tz_name))
        return site_aware.replace(tzinfo=None)
    except Exception:
        return naive_utc  # fall back to as-is if zoneinfo unavailable


def _store_ebkn_capabilities(dev_id: str, payload: dict) -> None:
    """Persist EBKN device capabilities from the receive_cmd body.

    EBKN sends fk_name, fk_time, fk_info:{firmware, supported_enroll_data, fk_bin_data_lib}
    in the body of every receive_cmd poll.  We use a Redis flag to avoid writing to the DB
    on every poll — only writes once per 24 hours.  The cache is bypassed if DB fields are
    still empty, so a fresh device always gets its capabilities stored on first poll.
    """
    cache_key = f"ebkn:caps_stored:{dev_id}"

    updates: dict = {}
    fk_name = payload.get("fk_name")
    if fk_name:
        updates["mac_address"] = str(fk_name)

    fk_info = payload.get("fk_info") or {}
    if isinstance(fk_info, dict):
        firmware = fk_info.get("firmware")
        if firmware:
            updates["firmware_version"] = str(firmware)
        supported = fk_info.get("supported_enroll_data")
        if supported:
            updates["supported_biometrics"] = str(supported)

    if not updates:
        frappe.cache.set_value(cache_key, "1", expires_in_sec=86400)
        return

    # Skip DB write if cached AND all fields are already populated
    if frappe.cache.get_value(cache_key):
        existing = frappe.db.get_value(
            "Attendance Device", dev_id,
            list(updates.keys()),
            as_dict=True,
        ) or {}
        if all(existing.get(k) for k in updates):
            return  # already stored, no change needed

    try:
        frappe.db.set_value("Attendance Device", dev_id, updates)
        frappe.db.commit()
    except Exception:
        pass  # don't break command polling on a capabilities save failure

    frappe.cache.set_value(cache_key, "1", expires_in_sec=86400)
