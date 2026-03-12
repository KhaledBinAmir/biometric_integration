# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Employee Checkin creation service.

Employee ID mapping is always via Employee.attendance_device_id.
This is the standard HRMS field and is efficiently indexed.
"""

from __future__ import annotations
from datetime import datetime

import frappe

from biometric_integration.utils.device_cache import get_employee_by_pin


def create_employee_checkin(
    device_pin: str,
    timestamp: datetime,
    device_id: str | None = None,
    log_type: str | None = None,
    biometric_method: str | None = None,
) -> bool:
    """Create an Employee Checkin from a device attendance event.

    Returns True on success or a duplicate (idempotent), False on failure.
    Also queues a Get Enroll Data command if this PIN has no user record yet,
    so biometrics are lazily synced on the first punch.
    """
    try:
        employee_id = get_employee_by_pin(str(device_pin))

        if not employee_id:
            settings = frappe.get_cached_doc("Attendance Integration Settings")
            if not settings.do_not_skip_unknown_employee_checkin:
                _ensure_device_user_synced(str(device_pin), device_id)
                return False
            # Proceed with blank employee if setting allows it

        checkin = frappe.new_doc("Employee Checkin")
        checkin.employee = employee_id
        checkin.log_type = log_type
        checkin.time = timestamp
        checkin.device_id = device_id
        if biometric_method:
            checkin.biometric_method = biometric_method
        if device_id:
            checkin.attendance_device = device_id

        checkin.insert(
            ignore_mandatory=not bool(employee_id),
            ignore_permissions=True,
        )
        frappe.db.commit()

        _ensure_device_user_synced(str(device_pin), device_id)
        return True

    except frappe.exceptions.ValidationError as ve:
        if "already has a log with the same timestamp" in str(ve):
            return True  # duplicate — treat as success
        frappe.log_error(
            title="Checkin Validation Error",
            message=frappe.get_traceback(),
            reference_doctype="Employee",
            reference_name=locals().get("employee_id"),
        )
        return False

    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            title="Failed to Create Employee Checkin",
            message=frappe.get_traceback(),
        )
        return False


def _ensure_device_user_synced(pin: str, device_id: str | None) -> None:
    """Lazily create an Attendance Device User and queue Get Enroll Data
    on the first punch from a PIN we have no record for.

    Called on every checkin — exits immediately (single cache hit) when the
    user is already known and linked to this device.
    """
    if not device_id:
        return

    # Fast path: Redis cache says this pin+device combo is already synced
    cache_key = f"biometric:user_synced:{pin}:{device_id}"
    if frappe.cache.get_value(cache_key):
        return

    try:
        employee_id = get_employee_by_pin(pin)

        if frappe.db.exists("Attendance Device User", pin):
            user_doc = frappe.get_doc("Attendance Device User", pin)
            # Link employee if not yet mapped
            if not user_doc.employee and employee_id:
                user_doc.employee = employee_id
                user_doc.save(ignore_permissions=True)
            # Check if already linked to this device
            if any(row.attendance_device == device_id for row in (user_doc.devices or [])):
                frappe.cache.set_value(cache_key, "1", expires_in_sec=3600)
                return
        else:
            # First encounter for this PIN — create a stub user record
            user_doc = frappe.new_doc("Attendance Device User")
            user_doc.user_id = pin
            user_doc.employee = employee_id
            user_doc.insert(ignore_permissions=True)

        # Link this device to the user
        brand = frappe.db.get_value("Attendance Device", device_id, "brand")
        if not brand:
            return
        user_doc.append("devices", {"attendance_device": device_id})
        user_doc.save(ignore_permissions=True)

        # Queue Get Enroll Data unless one is already pending
        already_pending = frappe.db.exists("Attendance Device Command", {
            "attendance_device": device_id,
            "attendance_device_user": user_doc.name,
            "command_type": "Get Enroll Data",
            "status": "Pending",
        })
        if not already_pending:
            cmd = frappe.new_doc("Attendance Device Command")
            cmd.attendance_device = device_id
            cmd.attendance_device_user = user_doc.name
            cmd.command_type = "Get Enroll Data"
            cmd.status = "Pending"
            cmd.insert(ignore_permissions=True)

        frappe.db.commit()
        frappe.cache.set_value(cache_key, "1", expires_in_sec=3600)

    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            title="Device User Sync Failed",
            message=frappe.get_traceback(),
        )
