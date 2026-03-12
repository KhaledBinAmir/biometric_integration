# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

import frappe
from frappe.model.document import Document


class AttendanceDeviceLog(Document):
    @staticmethod
    def clear_old_logs(days=30):
        from frappe.query_builder import Interval
        from frappe.query_builder.functions import Now

        table = frappe.qb.DocType("Attendance Device Log")
        frappe.db.delete(table, filters=(table.modified < (Now() - Interval(days=days))))


def maybe_log(
    device_id: str,
    log_type: str,
    direction: str,
    summary: str,
    user_pin: str = None,
    raw_data: str = None,
    force: bool = False,
) -> None:
    """Insert an Attendance Device Log.

    When force=True the log is written regardless of the enable_device_log setting
    (used for unregistered-device events so admins can discover unknown serials).
    Always fails silently to never disrupt device communication.
    """
    import frappe
    try:
        if not force:
            enabled = frappe.db.get_single_value("Attendance Integration Settings", "enable_device_log")
            if not enabled:
                return
        from frappe.utils import now_datetime
        # Only link to Attendance Device if the record actually exists
        device_link = device_id if frappe.db.exists("Attendance Device", device_id) else None
        frappe.get_doc({
            "doctype": "Attendance Device Log",
            "device_serial": device_id,
            "attendance_device": device_link,
            "log_type": log_type,
            "direction": direction,
            "timestamp": now_datetime(),
            "user_pin": user_pin,
            "summary": summary,
            "raw_data": raw_data,
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass  # logging must never break device communication
