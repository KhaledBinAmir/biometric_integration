# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
User sync service: reacts to Employee lifecycle events and propagates changes
to biometric devices.

  Employee goes inactive/left   → queue Delete User on all devices
  Employee reactivated          → queue Enroll User on all devices (if template exists)
  Employee name changes         → queue Update User on ZKTeco devices
  Attendance Device User gets employee link → queue Update User on ZKTeco devices
"""

from __future__ import annotations

from typing import Optional

import frappe

from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
    add_command,
)

_INACTIVE_STATUSES = {"Left", "Inactive"}
_BRAND_BLOB_FIELD = {"ZKTeco": "zkteco_enroll_data", "EBKN": "ebkn_enroll_data"}


# ---------------------------------------------------------------------------
# Employee hook
# ---------------------------------------------------------------------------

def on_employee_update(doc, method=None) -> None:
    """Frappe doc_events hook — called on every Employee save."""
    before = doc.get_doc_before_save()
    if not before:
        return

    user_doc = _find_device_user(doc.name)
    if not user_doc:
        return

    status_before = before.status or "Active"
    status_after = doc.status or "Active"
    name_before = before.employee_name or ""
    name_after = doc.employee_name or ""

    status_changed = status_before != status_after
    name_changed = name_before != name_after

    if status_changed:
        if status_after in _INACTIVE_STATUSES:
            _delete_from_all_devices(user_doc)
        elif status_after == "Active" and status_before in _INACTIVE_STATUSES:
            _re_enroll_on_all_devices(user_doc)

    if name_changed and status_after not in _INACTIVE_STATUSES:
        _update_user_info_on_zkteco(user_doc)


# ---------------------------------------------------------------------------
# Called from attendance_device_user.py when employee link is set
# ---------------------------------------------------------------------------

def on_employee_linked(user_doc) -> None:
    """Sync updated employee name to ZKTeco devices when user gets linked to employee."""
    _update_user_info_on_zkteco(user_doc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_device_user(employee_name: str) -> Optional[object]:
    doc_name = frappe.db.get_value("Attendance Device User", {"employee": employee_name})
    if not doc_name:
        return None
    return frappe.get_doc("Attendance Device User", doc_name)


def _get_user_devices(user_doc) -> dict:
    """Return {device_id: brand} for all devices this user should be on."""
    if user_doc.allow_in_all_devices:
        devices = frappe.get_all(
            "Attendance Device", filters={"disabled": 0}, fields=["name", "brand"]
        )
        return {d.name: d.brand for d in devices}
    return {
        row.attendance_device: row.brand
        for row in user_doc.get("devices", [])
        if row.attendance_device
    }


def _delete_from_all_devices(user_doc) -> None:
    for device_id, brand in _get_user_devices(user_doc).items():
        add_command(device_id, user_doc.name, brand, "Delete User")
    frappe.db.commit()


def _re_enroll_on_all_devices(user_doc) -> None:
    for device_id, brand in _get_user_devices(user_doc).items():
        blob_field = _BRAND_BLOB_FIELD.get(brand, "")
        if blob_field and user_doc.get(blob_field):
            add_command(device_id, user_doc.name, brand, "Enroll User")
    frappe.db.commit()


def _update_user_info_on_zkteco(user_doc) -> None:
    """Queue Update User on ZKTeco devices so the device shows the correct name."""
    for device_id, brand in _get_user_devices(user_doc).items():
        if brand == "ZKTeco":
            add_command(device_id, user_doc.name, brand, "Update User")
    frappe.db.commit()
