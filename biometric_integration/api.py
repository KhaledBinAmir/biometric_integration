# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Whitelisted API methods called by the Frappe UI (form JS).
Device traffic is handled by renderers.py (page_renderer hook), NOT here.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, get_url


@frappe.whitelist()
def get_endpoint_urls() -> dict:
    """Return the server addresses devices should be configured with.

    ZKTeco: device is configured with hostname only — firmware appends /iclock/*
    paths automatically. EBKN: full URL including /ebkn path.

    When the HTTP listener is active, plain-HTTP alternatives are shown.
    A best-effort public IP lookup (api.ipify.org) adds a raw IP:port line.
    """
    frappe.only_for("System Manager")
    import urllib.request
    from urllib.parse import urlparse

    base = get_url().rstrip("/")
    parsed = urlparse(base)
    host = parsed.hostname

    # HTTP listener port stored in site_config by configurator.py
    listener_port = frappe.conf.get("biometric_listener_port")

    # Best-effort public IP detection (only relevant when listener is active)
    public_ip = None
    if listener_port:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
                public_ip = resp.read().decode().strip() or None
        except Exception:
            pass
        if public_ip == host:
            public_ip = None  # redundant — skip

    # --- ZKTeco: hostname only (firmware adds /iclock/* paths) ---
    zkteco_lines = [host]
    if listener_port:
        zkteco_lines.append(f"{host}:{listener_port}")
        if public_ip:
            zkteco_lines.append(f"{public_ip}:{listener_port}")

    # --- EBKN: full URL with /ebkn path ---
    ebkn_lines = [f"{base}/ebkn"]
    if listener_port:
        ebkn_lines.append(f"http://{host}:{listener_port}/ebkn")
        if public_ip:
            ebkn_lines.append(f"http://{public_ip}:{listener_port}/ebkn")

    return {
        "zkteco": "\n".join(zkteco_lines),
        "ebkn": "\n".join(ebkn_lines),
    }


@frappe.whitelist()
def check_proxy_compatibility() -> dict:
    """Check whether this server supports UI-based nginx proxy configuration."""
    from biometric_integration.proxy.detector import check_proxy_compatibility as _check

    return _check()


@frappe.whitelist()
def enable_proxy(port: int) -> dict:
    """Enable the nginx HTTP listener on the given port."""
    frappe.only_for("System Manager")
    from biometric_integration.proxy.configurator import enable_listener_logic

    ok, message = enable_listener_logic(frappe.local.site, int(port))
    return {"success": ok, "message": message}


@frappe.whitelist()
def disable_proxy() -> dict:
    """Disable the nginx HTTP listener."""
    frappe.only_for("System Manager")
    from biometric_integration.proxy.configurator import disable_listener_logic

    ok, message = disable_listener_logic(frappe.local.site)
    return {"success": ok, "message": message}


@frappe.whitelist()
def get_proxy_status() -> dict:
    """Return current proxy status (enabled, port)."""
    from biometric_integration.proxy.configurator import get_status_logic

    return get_status_logic(frappe.local.site)


@frappe.whitelist()
def get_generated_nginx_config(port: int = 8998) -> str:
    """Return a ready-to-use nginx server block for manual installation."""
    frappe.only_for("System Manager")
    from biometric_integration.proxy.template import get_server_block

    return get_server_block(frappe.local.site, int(port))


@frappe.whitelist(methods=["POST"])
def sync_employees(dry_run: int = 1) -> dict:
    """Put every in-scope employee on the devices they belong on (per the
    Employee Sync Mode). dry_run=1 only reports what would be added; the real
    run goes to a background job and reports back over realtime, so a large
    site cannot hit the request timeout halfway through."""
    frappe.only_for("System Manager")
    from biometric_integration.services.user_sync import reconcile_employee_devices

    if cint(dry_run):
        return reconcile_employee_devices(dry_run=True)
    frappe.enqueue(
        "biometric_integration.services.user_sync.run_sync_job",
        queue="long",
        user=frappe.session.user,
        job_id="biometric_employee_sync",
        deduplicate=True,
    )
    return {"queued": True}


@frappe.whitelist()
def enqueue_all_enrollments(device_id: str) -> str:
    """Put this device's assigned users back on it, plus whoever belongs there
    per the Employee Sync Mode."""
    frappe.only_for("System Manager")
    from biometric_integration.biometric_integration.doctype.attendance_device.attendance_device import (
        _enqueue_initial_enrollments,
    )

    device = frappe.get_doc("Attendance Device", device_id)
    _enqueue_initial_enrollments(device)
    return f"Enrollment commands queued for device {device.device_name}."


@frappe.whitelist()
def enqueue_user_enrollments(user_id: str) -> str:
    """Queue Enroll User commands for all assigned devices of a user."""
    frappe.only_for("System Manager")
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
        add_command,
    )
    from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
        _get_user_devices,
    )

    user_doc = frappe.get_doc("Attendance Device User", user_id)
    devices = _get_user_devices(user_doc)
    count = 0
    _BRAND_BLOB_FIELD = {"ZKTeco": "zkteco_enroll_data", "EBKN": "ebkn_enroll_data"}
    for device_id, brand in devices.items():
        if user_doc.get(_BRAND_BLOB_FIELD.get(brand, "")):
            add_command(device_id, user_doc.name, brand, "Enroll User")
            count += 1
    return f"Queued {count} Enroll User command(s)."


@frappe.whitelist(methods=["POST"])
def create_device_command(device_id: str, command_type: str) -> str:
    """Create and return the name of a new Attendance Device Command."""
    frappe.only_for("System Manager")
    ALLOWED = {
        "Get Enroll Data",
        "Enroll User",
        "Delete User",
        "Update User",
        "Sync User List",
        "Restart Device",
        "Unlock Door",
        "Set Device Time",
        "Pull From Device",
        "Refresh Device Info",
    }
    if command_type not in ALLOWED:
        frappe.throw(f"Unsupported command type: {command_type}")
    brand = frappe.db.get_value("Attendance Device", device_id, "brand")
    if not brand:
        frappe.throw(f"Device not found: {device_id}")
    cmd = frappe.new_doc("Attendance Device Command")
    cmd.attendance_device = device_id
    cmd.brand = brand
    cmd.command_type = command_type
    cmd.status = "Pending"
    cmd.insert(ignore_permissions=True)  # the command's after_insert commits
    return cmd.name


@frappe.whitelist(methods=["POST"])
def create_repull_command(device_id: str, start_time: str, end_time: str | None = None) -> str:
    """Queue a ZKTeco 'Re-pull Attendance' command for a date range.

    The device re-uploads its stored attendance logs for [start_time, end_time];
    they flow through the normal checkin pipeline (duplicates are ignored).
    """
    frappe.only_for("System Manager")
    brand = frappe.db.get_value("Attendance Device", device_id, "brand")
    if not brand:
        frappe.throw(f"Device not found: {device_id}")
    if brand != "ZKTeco":
        frappe.throw(frappe._("Re-pull Attendance is only supported for ZKTeco devices."))
    cmd = frappe.new_doc("Attendance Device Command")
    cmd.attendance_device = device_id
    cmd.brand = brand
    cmd.command_type = "Re-pull Attendance"
    cmd.repull_start = start_time
    cmd.repull_end = end_time
    cmd.status = "Pending"
    cmd.insert(ignore_permissions=True)  # the command's after_insert commits
    return cmd.name


@frappe.whitelist()
def get_device_form_settings() -> dict:
    """Return Attendance Integration Settings values needed by device/settings forms."""
    settings = frappe.get_cached_doc("Attendance Integration Settings")
    return {
        "push_timezone_to_device": cint(settings.push_timezone_to_device),
    }


@frappe.whitelist()
def get_command_status(cmd_name: str) -> dict:
    """Return the current status and closed_on of a command (for real-time polling)."""
    status, closed_on = frappe.db.get_value(
        "Attendance Device Command", cmd_name, ["status", "closed_on"]
    ) or ("", None)
    return {"status": status, "closed_on": str(closed_on) if closed_on else None}


@frappe.whitelist()
def enqueue_user_deletions(user_id: str) -> str:
    """Queue Delete User commands for all assigned devices of a user."""
    frappe.only_for("System Manager")
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
        add_command,
    )
    from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
        _get_user_devices,
    )

    user_doc = frappe.get_doc("Attendance Device User", user_id)
    devices = _get_user_devices(user_doc)
    for device_id, brand in devices.items():
        add_command(device_id, user_doc.name, brand, "Delete User")
    return f"Queued {len(devices)} Delete User command(s)."


def attendance_device_companies() -> list[str]:
    """Companies with at least one enabled attendance device.

    Answers the ``attendance_device_companies`` hook, so another app (e.g. a
    payroll app that tracks check-ins by default where people can punch) can
    ask without importing this one.
    """
    return frappe.get_all(
        "Attendance Device",
        filters={"disabled": 0, "company": ["is", "set"]},
        pluck="company",
        distinct=True,
    )
