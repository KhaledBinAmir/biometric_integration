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
) -> bool:
    """Create an Employee Checkin from a device attendance event.

    Returns True on success or a duplicate (idempotent), False on failure.
    """
    try:
        employee_id = get_employee_by_pin(str(device_pin))

        if not employee_id:
            settings = frappe.get_cached_doc("Attendance Integration Settings")
            if not settings.do_not_skip_unknown_employee_checkin:
                return False
            # Proceed with blank employee if setting allows it

        checkin = frappe.new_doc("Employee Checkin")
        checkin.employee = employee_id
        checkin.log_type = log_type
        checkin.time = timestamp
        checkin.device_id = device_id

        checkin.insert(
            ignore_mandatory=not bool(employee_id),
            ignore_permissions=True,
        )
        frappe.db.commit()
        return True

    except frappe.exceptions.ValidationError as ve:
        if "already has a log with the same timestamp" in str(ve):
            return True  # duplicate — treat as success
        frappe.log_error(
            title="Checkin Validation Error",
            message=frappe.get_traceback(),
            reference_doctype="Employee",
            reference_name=employee_id if "employee_id" in dir() else None,
        )
        return False

    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            title="Failed to Create Employee Checkin",
            message=frappe.get_traceback(),
        )
        return False
