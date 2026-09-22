# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
User sync service: reacts to Employee lifecycle events and propagates changes
to biometric devices.

  Employee save with create_user_in_device=1  → create/link Device User on every
                                                enabled device of the employee's
                                                company, queue Update User on each
  Employee attendance_device_id changes        → same as above (if checkbox is on)
  Employee goes inactive/left                  → queue Delete User on all devices
  Employee reactivated                         → queue Enroll User on all devices
  Employee name changes                        → queue Update User on all devices
  Attendance Device User gets employee link    → queue Update User on all devices
"""

from __future__ import annotations

from typing import Optional

import frappe

from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
    add_command,
)
from biometric_integration.utils.device_cache import invalidate_employee_pin

_INACTIVE_STATUSES = {"Left", "Inactive"}
_BRAND_BLOB_FIELD = {"ZKTeco": "zkteco_enroll_data", "EBKN": "ebkn_enroll_data"}


# ---------------------------------------------------------------------------
# Employee hooks
# ---------------------------------------------------------------------------

def validate_employee(doc, method=None) -> None:
    """Frappe doc_events validate hook — runs before save."""
    if not doc.get("create_user_in_device"):
        return
    device_id = str(doc.get("attendance_device_id") or "").strip()
    if device_id and not device_id.isdigit():
        frappe.throw(
            frappe._("Attendance Device ID must be a numeric value. "
                     "ZKTeco and EBKN devices only support integer user IDs."),
            title=frappe._("Invalid Device ID"),
        )


def on_employee_update(doc, method=None) -> None:
    """Frappe doc_events on_update hook — called on every Employee save."""
    before = doc.get_doc_before_save()

    # Keep the PIN→Employee cache honest when the device-ID mapping changes, so a
    # freshly-mapped employee's punches are attributed immediately (the mapping is
    # otherwise cached in Redis for up to 5 minutes).
    pin_after = str(doc.get("attendance_device_id") or "").strip()
    pin_before = str(before.get("attendance_device_id") or "").strip() if before else ""
    if pin_after != pin_before:
        for pin in (pin_before, pin_after):
            if pin:
                invalidate_employee_pin(pin)

    if not before:
        # New employee — handle device user creation if checkbox set
        if doc.get("create_user_in_device"):
            _handle_device_user_creation(doc)
        return

    # --- Status / name changes (always active, no toggle) ---
    user_doc = _find_device_user(doc.name)
    if user_doc:
        status_before = before.status or "Active"
        status_after = doc.status or "Active"
        name_before = before.employee_name or ""
        name_after = doc.employee_name or ""

        if status_before != status_after:
            if status_after in _INACTIVE_STATUSES:
                _delete_from_all_devices(user_doc)
            elif status_after == "Active" and status_before in _INACTIVE_STATUSES:
                _re_enroll_on_all_devices(user_doc)

        if name_before != name_after and status_after not in _INACTIVE_STATUSES:
            _update_user_info(user_doc)

    # --- Device user creation via checkbox ---
    checkbox_before = before.get("create_user_in_device")
    checkbox_after = doc.get("create_user_in_device")
    device_id_before = str(before.get("attendance_device_id") or "").strip()
    device_id_after = str(doc.get("attendance_device_id") or "").strip()

    # Trigger when checkbox just turned on, OR device ID changed while checkbox is on
    if checkbox_after and (
        (not checkbox_before)
        or (device_id_after and device_id_before != device_id_after)
    ):
        if device_id_after:
            _handle_device_user_creation(doc)


# ---------------------------------------------------------------------------
# Called from attendance_device_user.py when employee link is set
# ---------------------------------------------------------------------------

def on_employee_linked(user_doc) -> None:
    """Sync updated employee name to all devices when user gets linked to employee."""
    _update_user_info(user_doc)


# ---------------------------------------------------------------------------
# Device user creation
# ---------------------------------------------------------------------------

def _resolve_target_devices(employee_doc) -> dict:
    """Return {device_id: brand} for every device this employee belongs on.

    Device assignment follows COMPANY: an employee belongs on every enabled
    device owned by their own company. A site with two clocks at one location
    (or a second unit added later) then needs no per-employee device picking —
    ticking the enrolment box is enough, and the person appears on all of them.

    `biometric_device` remains an explicit override: it is always included, so
    an employee can be put on a device outside their company when that is
    genuinely wanted. Disabled devices are never targeted, which keeps a unit
    that is switched off (awaiting RMA, or being repurposed) out of every sync.
    """
    targets = {}
    company = employee_doc.get("company")
    if company:
        for d in frappe.get_all(
            "Attendance Device",
            filters={"company": company, "disabled": 0},
            fields=["name", "brand"],
        ):
            if d.brand:
                targets[d.name] = d.brand

    picked = employee_doc.get("biometric_device")
    if picked and picked not in targets:
        brand = frappe.db.get_value("Attendance Device", picked, "brand")
        disabled = frappe.db.get_value("Attendance Device", picked, "disabled")
        if brand and not disabled:
            targets[picked] = brand
    return targets


def _handle_device_user_creation(employee_doc) -> None:
    """Create or link the Attendance Device User and queue Update User per device.

    - Finds existing Device User by attendance_device_id (user_id)
    - Creates one if not found, links employee
    - Adds every target device to the child table (if not already there)
    - Queues Update User on each, so the person exists on the clock by PIN and
      name. Fingerprints are enrolled at the device afterwards and captured back
      automatically; no template is needed to provision a user.
    """
    device_id = str(employee_doc.get("attendance_device_id") or "").strip()
    targets = _resolve_target_devices(employee_doc)
    if not device_id or not targets:
        return

    # Find or create the Device User record
    existing_name = frappe.db.get_value("Attendance Device User", {"user_id": device_id})
    if existing_name:
        user_doc = frappe.get_doc("Attendance Device User", existing_name)
        changed = False
        if not user_doc.employee:
            user_doc.employee = employee_doc.name
            user_doc.employee_name = employee_doc.employee_name
            changed = True
        # Add any missing target device to the child table
        existing_devices = {row.attendance_device for row in user_doc.get("devices", [])}
        for device_id_t, brand_t in targets.items():
            if device_id_t not in existing_devices:
                user_doc.append("devices", {"attendance_device": device_id_t, "brand": brand_t})
                changed = True
        if changed:
            user_doc.save(ignore_permissions=True)
    else:
        user_doc = frappe.get_doc({
            "doctype": "Attendance Device User",
            "user_id": device_id,
            "employee": employee_doc.name,
            "employee_name": employee_doc.employee_name,
            "devices": [
                {"attendance_device": d, "brand": b} for d, b in targets.items()
            ],
        })
        user_doc.insert(ignore_permissions=True)

    for device_id_t, brand_t in targets.items():
        add_command(device_id_t, user_doc.name, brand_t, "Update User")
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_device_user(employee_name: str) -> Optional[object]:
    doc_name = frappe.db.get_value("Attendance Device User", {"employee": employee_name})
    if not doc_name:
        return None
    return frappe.get_doc("Attendance Device User", doc_name)


def _get_user_devices(user_doc) -> dict:
    """Return {device_id: brand} for all active devices this user should be on."""
    if user_doc.allow_in_all_devices:
        devices = frappe.get_all("Attendance Device", filters={"disabled": 0}, fields=["name", "brand"])
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


def _update_user_info(user_doc) -> None:
    """Queue Update User on all devices (ZKTeco: USERINFO, EBKN: SET_USER_PROFILE)."""
    for device_id, brand in _get_user_devices(user_doc).items():
        add_command(device_id, user_doc.name, brand, "Update User")
    frappe.db.commit()
