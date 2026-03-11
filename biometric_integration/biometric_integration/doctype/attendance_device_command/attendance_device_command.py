# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, add_to_date, now_datetime, get_datetime


class AttendanceDeviceCommand(Document):
    def after_insert(self):
        """Set the pending command flag on the device so polling is efficient."""
        if self.attendance_device:
            frappe.db.set_value(
                "Attendance Device", self.attendance_device,
                "has_pending_command", 1, update_modified=False
            )
            frappe.db.commit()

    def before_save(self):
        """Auto-close commands that exceed max attempts or age limit."""
        try:
            settings = frappe.db.get_value(
                "Attendance Device Settings", None,
                ["maximum_command_attempts", "force_close_after_days"],
                as_dict=True,
            ) or {}
            max_attempts = cint(settings.get("maximum_command_attempts")) or 3
            force_days = cint(settings.get("force_close_after_days")) or 30

            if (
                max_attempts
                and cint(self.no_of_attempts) >= max_attempts
                and self.status not in ("Closed", "Success", "Failed")
            ):
                self.status = "Failed"
                self.closed_on = now_datetime()

            if (
                force_days
                and self.initiated_on
                and add_to_date(get_datetime(self.initiated_on), days=force_days) <= now_datetime()
                and self.status not in ("Closed", "Success", "Failed")
            ):
                self.status = "Failed"
                self.closed_on = now_datetime()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "AttendanceDeviceCommand before_save failed")


def add_command(device_id: str, user_id: str, brand: str, command_type: str) -> None:
    """Create an Attendance Device Command unless an equivalent pending one already exists."""
    if frappe.db.exists(
        "Attendance Device Command",
        {
            "attendance_device": device_id,
            "attendance_device_user": user_id,
            "brand": brand,
            "command_type": command_type,
            "status": "Pending",
        },
    ):
        return
    cmd = frappe.get_doc(
        {
            "doctype": "Attendance Device Command",
            "attendance_device": device_id,
            "attendance_device_user": user_id,
            "brand": brand,
            "command_type": command_type,
            "status": "Pending",
        }
    )
    cmd.insert(ignore_permissions=True)
    # after_insert commits the flag update
