# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Whitelisted API methods called by the Frappe UI (form JS).
Device traffic is handled by renderers.py (page_renderer hook), NOT here.
"""

from __future__ import annotations

import frappe
from frappe.utils import get_url


@frappe.whitelist()
def get_endpoint_urls() -> dict:
    """Return the server addresses devices should be configured with.

    ZKTeco: device is configured with hostname + port only — firmware appends
    /iclock/* paths automatically. Show host:port so user can copy it directly
    into the device's "Server Address" field.

    EBKN: full URL including /ebkn path (configured as the push server URL).
    """
    from urllib.parse import urlparse
    base = get_url().rstrip("/")
    parsed = urlparse(base)
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    host = parsed.hostname
    zkteco_addr = f"{host}:{port}" if port not in (80, 443) else host
    return {
        "zkteco": zkteco_addr,
        "ebkn": f"{base}/ebkn",
    }


@frappe.whitelist()
def check_proxy_compatibility() -> dict:
    """Check whether this server supports UI-based nginx proxy configuration."""
    from biometric_integration.proxy.detector import check_proxy_compatibility as _check
    return _check()


@frappe.whitelist()
def enable_proxy(port: int) -> dict:
    """Enable the nginx HTTP listener on the given port."""
    from biometric_integration.proxy.configurator import enable_listener_logic
    ok, message = enable_listener_logic(frappe.local.site, int(port))
    return {"success": ok, "message": message}


@frappe.whitelist()
def disable_proxy() -> dict:
    """Disable the nginx HTTP listener."""
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
    from biometric_integration.proxy.template import get_server_block
    return get_server_block(frappe.local.site, int(port))


@frappe.whitelist()
def enqueue_all_enrollments(device_id: str) -> str:
    """Queue Enroll User commands for all eligible users for a given device."""
    from biometric_integration.biometric_integration.doctype.attendance_device.attendance_device import (
        _enqueue_initial_enrollments,
    )
    device = frappe.get_doc("Attendance Device", device_id)
    _enqueue_initial_enrollments(device)
    return f"Enrollment commands queued for device {device.device_name}."


@frappe.whitelist()
def enqueue_user_enrollments(user_id: str) -> str:
    """Queue Enroll User commands for all assigned devices of a user."""
    from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
        _get_user_devices,
    )
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
        add_command,
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


@frappe.whitelist()
def enqueue_user_deletions(user_id: str) -> str:
    """Queue Delete User commands for all assigned devices of a user."""
    from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
        _get_user_devices,
    )
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
        add_command,
    )
    user_doc = frappe.get_doc("Attendance Device User", user_id)
    devices = _get_user_devices(user_doc)
    for device_id, brand in devices.items():
        add_command(device_id, user_doc.name, brand, "Delete User")
    return f"Queued {len(devices)} Delete User command(s)."
