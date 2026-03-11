# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Command processor: fetches the next pending Attendance Device Command and builds
the brand-specific payload to return to the polling device.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional, Union

import frappe
from frappe.utils import cint, now, now_datetime


def process_device_command(device_sn: str) -> Optional[Union[str, dict]]:
    """Return the next pending command payload for the device, or None if none."""
    command_name = frappe.db.get_value(
        "Attendance Device Command",
        {"attendance_device": device_sn, "status": "Pending"},
        "name",
        order_by="creation asc",
    )
    if not command_name:
        return None

    cmd_doc = frappe.get_doc("Attendance Device Command", command_name)
    try:
        payload = _build_payload(cmd_doc)
        if payload:
            cmd_doc.no_of_attempts = (cmd_doc.no_of_attempts or 0) + 1
            cmd_doc.save(ignore_permissions=True)
            frappe.db.commit()
        return payload
    except Exception as exc:
        _handle_build_failure(cmd_doc, exc)
        return None


def force_close_stale_commands() -> None:
    """Scheduled daily task: mark old uncompleted commands as Failed."""
    settings = frappe.get_cached_doc("Attendance Device Settings")
    days = cint(settings.force_close_after_days) or 30
    cutoff = frappe.utils.add_to_date(now_datetime(), days=-days)

    stale = frappe.get_all(
        "Attendance Device Command",
        filters={"status": "Pending", "initiated_on": ["<", cutoff]},
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value(
            "Attendance Device Command", name,
            {"status": "Failed", "closed_on": now_datetime()},
            update_modified=False,
        )
    if stale:
        frappe.db.commit()


# ---------------------------------------------------------------------------
# Build logic
# ---------------------------------------------------------------------------

def _build_payload(cmd_doc: Any) -> Optional[Union[str, dict]]:
    user_doc = frappe.get_doc("Attendance Device User", cmd_doc.attendance_device_user)
    brand = cmd_doc.brand
    if brand == "ZKTeco":
        return _zkteco(cmd_doc, user_doc)
    if brand == "EBKN":
        return _ebkn(cmd_doc, user_doc)
    raise ValueError(f"Unsupported brand: {brand}")


def _zkteco(cmd_doc: Any, user_doc: Any) -> Optional[str]:
    cmd_id = cmd_doc.name
    pin = user_doc.user_id
    name = user_doc.employee_name or ""

    if cmd_doc.command_type == "Delete User":
        return f"C:{cmd_id}:DATA DELETE USERINFO PIN={pin}"

    if cmd_doc.command_type == "Get Enroll Data":
        return "\n".join([
            f"C:{cmd_id}:DATA QUERY USERINFO PIN={pin}",
            f"C:{cmd_id}:DATA QUERY FPTMP PIN={pin}",
        ])

    if cmd_doc.command_type == "Enroll User":
        blob = _load_blob(user_doc.zkteco_enroll_data)
        if not blob:
            raise FileNotFoundError(f"ZKTeco enroll data missing for user {user_doc.name}")
        template_b64 = base64.b64encode(blob).decode()
        return "\n".join([
            f"C:{cmd_id}:DATA UPDATE USERINFO PIN={pin}\tName={name}\tPri=0",
            f"C:{cmd_id}:DATA UPDATE FINGERPRINT PIN={pin}\tFID=0\tSize={len(blob)}\tValid=1\tTMP={template_b64}",
        ])
    return None


def _ebkn(cmd_doc: Any, user_doc: Any) -> Optional[dict]:
    uid = f"{int(user_doc.user_id):0>8}"

    if cmd_doc.command_type == "Delete User":
        return {
            "trans_id": cmd_doc.name,
            "cmd_code": "DELETE_USER",
            "body": json.dumps({"user_id": uid}),
        }

    if cmd_doc.command_type == "Get Enroll Data":
        return {
            "trans_id": cmd_doc.name,
            "cmd_code": "GET_USER_INFO",
            "body": json.dumps({"user_id": uid}),
        }

    if cmd_doc.command_type == "Enroll User":
        blob = _load_blob(user_doc.ebkn_enroll_data)
        if not blob:
            raise FileNotFoundError(f"EBKN enroll data missing for user {user_doc.name}")
        return {"trans_id": cmd_doc.name, "cmd_code": "SET_USER_INFO", "body": blob}

    return None


def _load_blob(url: str) -> Optional[bytes]:
    if not url:
        return None
    file_name = frappe.db.get_value("File", {"file_url": url}, "name")
    if not file_name:
        return None
    content = frappe.get_doc("File", file_name).get_content()
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _handle_build_failure(cmd_doc: Any, exc: Exception) -> None:
    try:
        frappe.db.rollback()
        cmd_doc.reload()
        cmd_doc.no_of_attempts = (cmd_doc.no_of_attempts or 0) + 1
        line = f"[{now()}] Build Failed: {exc}"
        cmd_doc.device_response = (
            f"{cmd_doc.device_response}\n{line}" if cmd_doc.device_response else line
        )
        settings = frappe.get_cached_doc("Attendance Device Settings")
        max_att = cint(settings.maximum_command_attempts) or 3
        if cmd_doc.no_of_attempts >= max_att:
            cmd_doc.status = "Failed"
            cmd_doc.closed_on = now_datetime()
        else:
            cmd_doc.status = "Pending"
        cmd_doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.log_error(
            title="Command Build Failed",
            message=frappe.get_traceback(),
            reference_doctype="Attendance Device Command",
            reference_name=cmd_doc.name,
        )
    except Exception as inner:
        frappe.db.rollback()
        frappe.log_error(title="Command Error Handler Failed", message=str(inner))
