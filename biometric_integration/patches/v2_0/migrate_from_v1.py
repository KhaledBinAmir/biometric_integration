# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Migration patch: copy data from v1 doctypes to v2 doctypes, then drop v1.

v1 doctypes (old names)            v2 doctypes (new names)
-----------------------------------------------------------------
Biometric Device               →   Attendance Device
Biometric Device User          →   Attendance Device User
Biometric Device User Detail   →   Attendance Device Link (child)
Biometric Device Command       →   Attendance Device Command
Biometric Integration Settings →   Attendance Integration Settings

Safety guarantees
-----------------
* Fully idempotent — safe to run on a v2 setup (all v1 tables absent → no-ops).
* Each record migration is try/except; failures are logged but never block others.
* v1 doctypes are dropped ONLY when zero migration errors were recorded.
* If any error occurs the patch raises at the end so the bench migrate output
  surfaces the problem clearly.

Recommended: take a database backup before upgrading.
"""

import frappe


def execute():
    errors: list[str] = []

    _migrate_devices(errors)
    _migrate_users(errors)
    _migrate_commands(errors)
    _migrate_settings(errors)
    frappe.db.commit()

    if errors:
        # Surface every failed record so the admin can investigate.
        error_summary = "\n".join(errors)
        frappe.log_error(
            title="v2 Migration: completed with errors — v1 tables NOT dropped",
            message=error_summary,
        )
        frappe.db.commit()
        raise Exception(
            f"v2 migration finished with {len(errors)} error(s). "
            "v1 tables were NOT dropped. Review Error Log before retrying."
        )

    # Only drop v1 doctypes when every migration step succeeded.
    _drop_v1_doctypes()
    frappe.db.commit()


# ── Device ────────────────────────────────────────────────────────────────────

def _migrate_devices(errors: list):
    if not frappe.db.table_exists("Biometric Device"):
        return
    for old in frappe.get_all("Biometric Device", fields=["*"]):
        serial = old.get("serial")
        if not serial or frappe.db.exists("Attendance Device", serial):
            continue
        try:
            frappe.get_doc({
                "doctype": "Attendance Device",
                "serial": serial,
                "device_name": old.device_name,
                "brand": old.brand if old.brand in ("ZKTeco", "EBKN") else "ZKTeco",
                "disabled": old.disabled,
                "last_synced_id": old.last_synced_id or 0,
                "last_contact": old.get("last_synced_time"),
                "disable_employee_sync": old.get("disable_syncing_employees", 0),
                "has_pending_command": old.get("has_pending_command", 0),
                "is_push_configured": old.get("push_protocol_configured", 0),
                "device_ip": old.get("device_ip"),
                "device_port": old.get("device_port"),
                "project": old.get("project"),
                "branch": old.get("branch"),
            }).insert(ignore_permissions=True, ignore_if_duplicate=True)
        except Exception:
            msg = f"Attendance Device '{serial}': {frappe.get_traceback()}"
            frappe.log_error(title="v2 Migration: Attendance Device failed", message=msg)
            errors.append(f"Device '{serial}' failed")


# ── User ──────────────────────────────────────────────────────────────────────

def _migrate_users(errors: list):
    if not frappe.db.table_exists("Biometric Device User"):
        return
    for old in frappe.get_all("Biometric Device User", fields=["*"]):
        user_id = old.get("user_id")
        if not user_id or frappe.db.exists("Attendance Device User", user_id):
            continue
        try:
            doc = frappe.get_doc({
                "doctype": "Attendance Device User",
                "user_id": user_id,
                "employee": old.employee,
                "allow_in_all_devices": old.get("allow_user_in_all_devices", 0),
                "zkteco_enroll_data": old.get("zkteco_enroll_data"),
                "ebkn_enroll_data": old.get("ebkn_enroll_data"),
            })
            old_links = frappe.get_all(
                "Biometric Device User Detail",
                filters={"parent": old.name},
                fields=["biometric_device", "brand", "enroll_data_source"],
            )
            for row in old_links:
                # v1 Biometric Device autoname = field:serial — same value as
                # Attendance Device name, so direct lookup works.
                if frappe.db.exists("Attendance Device", row.biometric_device):
                    doc.append("devices", {
                        "attendance_device": row.biometric_device,
                        "brand": row.brand,
                        "enroll_data_source": row.enroll_data_source,
                    })
            doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
        except Exception:
            msg = f"Attendance Device User '{user_id}': {frappe.get_traceback()}"
            frappe.log_error(title="v2 Migration: Attendance Device User failed", message=msg)
            errors.append(f"User '{user_id}' failed")


# ── Command ───────────────────────────────────────────────────────────────────

_VALID_COMMAND_TYPES = {
    "Get Enroll Data", "Enroll User", "Delete User", "Update User",
}


def _migrate_commands(errors: list):
    if not frappe.db.table_exists("Biometric Device Command"):
        return

    # Build lookup caches to avoid N+1 queries
    device_serial_map = {
        r.name: r.serial
        for r in frappe.get_all("Biometric Device", fields=["name", "serial"])
    }
    user_id_map = {
        r.name: r.user_id
        for r in frappe.get_all("Biometric Device User", fields=["name", "user_id"])
    }

    for old in frappe.get_all("Biometric Device Command", fields=["*"]):
        try:
            serial = device_serial_map.get(old.get("biometric_device"))
            if not serial or not frappe.db.exists("Attendance Device", serial):
                continue  # device wasn't migrated — skip

            user_id = user_id_map.get(old.get("biometric_device_user"))
            att_user = user_id if (user_id and frappe.db.exists("Attendance Device User", user_id)) else None

            cmd_type = old.get("command_type")
            if cmd_type not in _VALID_COMMAND_TYPES:
                continue

            # Idempotency: skip if an identical command already exists in v2
            # (patch re-run guard — match on device + user + type + initiated_on)
            if frappe.db.exists("Attendance Device Command", {
                "attendance_device": serial,
                "attendance_device_user": att_user or ["is", "not set"],
                "command_type": cmd_type,
                "initiated_on": old.get("initiated_on"),
            }):
                continue

            frappe.get_doc({
                "doctype": "Attendance Device Command",
                "attendance_device": serial,
                "attendance_device_user": att_user,
                "brand": old.get("brand"),
                "command_type": cmd_type,
                "status": old.get("status") or "Pending",
                "initiated_on": old.get("initiated_on"),
                "closed_on": old.get("closed_on"),
                "no_of_attempts": old.get("no_of_attempts") or 0,
                "device_response": old.get("device_response"),
            }).insert(ignore_permissions=True)
        except Exception:
            msg = f"Attendance Device Command '{old.name}': {frappe.get_traceback()}"
            frappe.log_error(title="v2 Migration: Attendance Device Command failed", message=msg)
            errors.append(f"Command '{old.name}' failed")


# ── Settings ──────────────────────────────────────────────────────────────────

def _migrate_settings(errors: list):
    if not frappe.db.exists("DocType", "Biometric Integration Settings"):
        return
    try:
        old = frappe.get_single("Biometric Integration Settings")
        settings = frappe.get_single("Attendance Integration Settings")
        settings.maximum_command_attempts = old.get("maximum_no_of_attempts_for_commands") or 3
        settings.force_close_after_days = old.get("force_close_after") or 30
        settings.do_not_skip_unknown_employee_checkin = old.get("do_not_skip_unknown_employee_checkin", 0)
        settings.save(ignore_permissions=True)
    except Exception:
        msg = frappe.get_traceback()
        frappe.log_error(title="v2 Migration: Attendance Integration Settings failed", message=msg)
        errors.append("Settings migration failed")


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _drop_v1_doctypes():
    """Hard-drop v1 doctypes and their SQL tables.

    Called only when zero migration errors were recorded.
    Child tables are dropped before parents to avoid FK issues.
    """
    v1_doctypes = [
        "Biometric Device User Detail",   # child — must come first
        "Biometric Device Command",
        "Biometric Device User",
        "Biometric Device",
        "Biometric Integration Settings",
    ]
    for dt in v1_doctypes:
        if not frappe.db.exists("DocType", dt):
            continue
        try:
            frappe.delete_doc(
                "DocType", dt,
                force=True,
                ignore_permissions=True,
                delete_permanently=True,
            )
        except Exception:
            # Non-fatal: log but don't raise — remaining doctypes can still be dropped.
            frappe.log_error(
                title=f"v2 Migration: Could not drop v1 DocType '{dt}'",
                message=frappe.get_traceback(),
            )
