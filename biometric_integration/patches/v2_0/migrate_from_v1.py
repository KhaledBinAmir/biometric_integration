# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Migration patch: copy data from v1 doctypes to v2 doctypes.

v1 doctypes (old names):
  Biometric Device             → Attendance Device
  Biometric Device User        → Attendance Device User
  Biometric Device User Detail → Attendance Device Link (child)
  Biometric Device Command     → Attendance Device Command
  Biometric Integration Settings → Attendance Device Settings

Run only once via patches.txt.
Old doctypes are NOT deleted — validate migration, then clean up manually.
"""

import frappe


def execute():
    _migrate_devices()
    _migrate_users()
    _migrate_settings()
    frappe.db.commit()


def _migrate_devices():
    if not frappe.db.table_exists("tabBiometric Device"):
        return
    for old in frappe.get_all("Biometric Device", fields=["*"]):
        if frappe.db.exists("Attendance Device", old.get("serial")):
            continue
        try:
            frappe.get_doc({
                "doctype": "Attendance Device",
                "serial": old.serial,
                "device_name": old.device_name,
                "brand": old.brand if old.brand in ("ZKTeco", "EBKN") else "ZKTeco",
                "disabled": old.disabled,
                "last_synced_id": old.last_synced_id or 0,
                "last_contact": old.get("last_synced_time"),
                "disable_employee_sync": old.get("disable_syncing_employees", 0),
                "max_command_attempts": old.get("maximum_sync_attempt") or 3,
                "has_pending_command": old.get("has_pending_command", 0),
                "is_push_configured": old.get("push_protocol_configured", 0),
                "device_ip": old.get("device_ip"),
                "device_port": old.get("device_port"),
                "project": old.get("project"),
                "branch": old.get("branch"),
            }).insert(ignore_permissions=True, ignore_if_duplicate=True)
        except Exception:
            frappe.log_error(
                title=f"v2 Migration: Attendance Device '{old.serial}' failed",
                message=frappe.get_traceback(),
            )


def _migrate_users():
    if not frappe.db.table_exists("tabBiometric Device User"):
        return
    for old in frappe.get_all("Biometric Device User", fields=["*"]):
        if frappe.db.exists("Attendance Device User", old.get("user_id")):
            continue
        try:
            doc = frappe.get_doc({
                "doctype": "Attendance Device User",
                "user_id": old.user_id,
                "employee": old.employee,
                "allow_in_all_devices": old.get("allow_user_in_all_devices", 0),
                "zkteco_enroll_data": old.get("zkteco_enroll_data"),
                "ebkn_enroll_data": old.get("ebkn_enroll_data"),
            })

            # Migrate child rows
            old_links = frappe.get_all(
                "Biometric Device User Detail",
                filters={"parent": old.name},
                fields=["biometric_device", "brand", "enroll_data_source"],
            )
            for row in old_links:
                if frappe.db.exists("Attendance Device", row.biometric_device):
                    doc.append("devices", {
                        "attendance_device": row.biometric_device,
                        "brand": row.brand,
                        "enroll_data_source": row.enroll_data_source,
                    })

            doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
        except Exception:
            frappe.log_error(
                title=f"v2 Migration: Attendance Device User '{old.user_id}' failed",
                message=frappe.get_traceback(),
            )


def _migrate_settings():
    if not frappe.db.table_exists("tabBiometric Integration Settings"):
        return
    try:
        old = frappe.get_single("Biometric Integration Settings")
        settings = frappe.get_single("Attendance Device Settings")
        settings.maximum_command_attempts = old.get("maximum_no_of_attempts_for_commands") or 3
        settings.force_close_after_days = old.get("force_close_after") or 30
        settings.do_not_skip_unknown_employee_checkin = old.get("do_not_skip_unknown_employee_checkin", 0)
        settings.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            title="v2 Migration: Attendance Device Settings failed",
            message=frappe.get_traceback(),
        )
