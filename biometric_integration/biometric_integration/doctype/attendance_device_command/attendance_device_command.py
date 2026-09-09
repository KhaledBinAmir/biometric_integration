# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, add_to_date, now_datetime, get_datetime

# Suppression window for identical commands (see the circuit breaker in
# add_command). Overridable via Attendance Integration Settings.
DEFAULT_COMMAND_DEDUPE_MINUTES = 10

# Terminal commands are deleted in chunks this size, committing between each, so
# a large backlog never holds a long lock on a live device-polling table.
_PURGE_CHUNK = 5000


class AttendanceDeviceCommand(Document):
    @staticmethod
    def clear_old_logs(days=90):
        from frappe.query_builder import Interval
        from frappe.query_builder.functions import Now

        table = frappe.qb.DocType("Attendance Device Command")
        frappe.db.delete(table, filters=(table.modified < (Now() - Interval(days=days))))

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
                "Attendance Integration Settings", None,
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


# User-provisioning commands are the ones gated by the device's
# `disable_employee_sync` switch (device-control commands like Restart/Unlock/
# Re-pull/Get Enroll Data are NOT gated — those are operator actions).
_EMPLOYEE_SYNC_COMMANDS = {"Enroll User", "Delete User", "Update User"}


def add_command(device_id: str, user_id: str, brand: str, command_type: str) -> None:
    """Create an Attendance Device Command unless an equivalent pending one already exists.

    Provisioning commands (Enroll/Delete/Update User) are suppressed for devices that
    are disabled or have `disable_employee_sync` set, so that switch reliably blocks
    all user push/pull to the device (not just the bulk enroll at registration time).
    """
    if command_type in _EMPLOYEE_SYNC_COMMANDS:
        dev = frappe.db.get_value(
            "Attendance Device", device_id,
            ["disabled", "disable_employee_sync"], as_dict=True,
        )
        if dev and (dev.disabled or dev.disable_employee_sync):
            return

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

    # Circuit breaker: suppress a command identical to one raised moments ago.
    # The Pending check above is not enough — a command that already completed
    # leaves nothing to collide with, so a feedback loop (device echoes a push,
    # the echo re-triggers the push) can re-raise the same command endlessly.
    # That is exactly what buried MKE in Sep 2026: 683 device/user pairs, the
    # same pair queued up to 352 times in 6 hours. This caps any repeat of that
    # class at one command per pair per window, whatever the cause upstream.
    # Unset falls back to the default; an explicit 0 genuinely disables the guard
    # (the field says so), which is why the two are distinguished here.
    raw = frappe.db.get_single_value("Attendance Integration Settings", "command_dedupe_minutes")
    window = DEFAULT_COMMAND_DEDUPE_MINUTES if raw is None or raw == "" else cint(raw)
    if window > 0 and frappe.db.sql(
        """SELECT name FROM `tabAttendance Device Command`
           WHERE attendance_device=%(dev)s AND attendance_device_user=%(usr)s
             AND command_type=%(typ)s AND creation > %(since)s LIMIT 1""",
        {
            "dev": device_id,
            "usr": user_id,
            "typ": command_type,
            "since": add_to_date(now_datetime(), minutes=-window),
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
