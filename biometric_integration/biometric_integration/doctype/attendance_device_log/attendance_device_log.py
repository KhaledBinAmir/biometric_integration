# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from frappe.model.document import Document


class AttendanceDeviceLog(Document):
    pass


def maybe_log(
    device_id: str,
    log_type: str,
    direction: str,
    summary: str,
    user_pin: str = None,
    raw_data: str = None,
) -> None:
    """Insert an Attendance Device Log only if logging is enabled in settings.
    Called from adapters — fails silently to never disrupt device communication.
    """
    import frappe
    try:
        enabled = frappe.db.get_single_value("Attendance Integration Settings", "enable_device_log")
        if not enabled:
            return
        from frappe.utils import now_datetime
        frappe.get_doc({
            "doctype": "Attendance Device Log",
            "attendance_device": device_id,
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
