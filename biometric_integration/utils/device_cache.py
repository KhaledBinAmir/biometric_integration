# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Redis-backed cache helpers for the biometric hot path.

All keys are namespaced under "biometric:" to avoid collisions with other
Frappe apps sharing the same Redis instance.

TTLs are intentionally short — correctness is more important than cache hit
rate. The goal is to absorb the burst of repeated identical reads that happen
within a single device polling cycle, not to cache indefinitely.
"""

from __future__ import annotations

import frappe

# ── TTLs (seconds) ──────────────────────────────────────────────────────────

_TTL_DEVICE_EXISTS   = 60    # re-check DB at most once per minute per device
_TTL_LAST_SYNC_ID    = 30    # short: updated on every ATTLOG commit
_TTL_LAST_CONTACT    = 60    # debounce heartbeat DB writes to 1/minute
_TTL_PIN_TO_EMPLOYEE = 300   # employee mapping changes rarely


# ── Device registration ──────────────────────────────────────────────────────

def is_registered_device(sn: str) -> bool:
    """Return True if sn exists in Attendance Device, using Redis cache."""
    if not sn:
        return False
    key = f"biometric:device_exists:{sn}"
    cached = frappe.cache.get_value(key)
    if cached is not None:
        return cached == "1"
    exists = bool(frappe.db.exists("Attendance Device", sn))
    frappe.cache.set_value(key, "1" if exists else "0", expires_in_sec=_TTL_DEVICE_EXISTS)
    return exists


def invalidate_device_cache(sn: str) -> None:
    """Call when a device is created or deleted."""
    frappe.cache.delete_value(f"biometric:device_exists:{sn}")
    frappe.cache.delete_value(f"biometric:last_contact:{sn}")
    frappe.cache.delete_value(f"biometric:last_sync_id:{sn}")


# ── Last contact (debounced write) ────────────────────────────────────────────

def touch_device(sn: str) -> None:
    """Update last_contact in DB at most once per minute per device.

    Every handshake and registry call hits this. With 100 devices on a 10s
    poll cycle that's 10 writes/second — all identical. Debounce to 1/min.
    """
    key = f"biometric:last_contact:{sn}"
    if frappe.cache.get_value(key):
        return  # already written recently — skip DB round-trip
    from datetime import datetime
    frappe.db.set_value(
        "Attendance Device", sn,
        {"is_push_configured": 1, "last_contact": datetime.now()},
        update_modified=False,
    )
    frappe.cache.set_value(key, "1", expires_in_sec=_TTL_LAST_CONTACT)


# ── Last synced ID ────────────────────────────────────────────────────────────

def get_last_sync_id(sn: str) -> int:
    """Return the last synced attendance log ID for the device."""
    key = f"biometric:last_sync_id:{sn}"
    cached = frappe.cache.get_value(key)
    if cached is not None:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass
    value = int(frappe.db.get_value("Attendance Device", sn, "last_synced_id") or 0)
    frappe.cache.set_value(key, str(value), expires_in_sec=_TTL_LAST_SYNC_ID)
    return value


def set_last_sync_id(sn: str, sync_id: int) -> None:
    """Persist and cache the new sync watermark after ATTLOG processing."""
    frappe.db.set_value(
        "Attendance Device", sn, "last_synced_id", sync_id, update_modified=False,
    )
    frappe.cache.set_value(
        f"biometric:last_sync_id:{sn}", str(sync_id), expires_in_sec=_TTL_LAST_SYNC_ID,
    )


# ── PIN → Employee mapping ────────────────────────────────────────────────────

_NONE_SENTINEL = "__none__"


def get_employee_by_pin(pin: str) -> str | None:
    """Map device PIN to ERPNext Employee name, cached in Redis.

    Cache miss rate is low — employees are mapped once and rarely change.
    Uses a sentinel value to distinguish "not found" from "not cached".
    """
    if not pin:
        return None
    pin_str = pin.strip()
    key = f"biometric:pin_emp:{pin_str}"
    cached = frappe.cache.get_value(key)
    if cached is not None:
        return None if cached == _NONE_SENTINEL else cached
    employee = frappe.db.get_value(
        "Employee", {"attendance_device_id": pin_str}, "name"
    )
    frappe.cache.set_value(
        key, employee if employee else _NONE_SENTINEL, expires_in_sec=_TTL_PIN_TO_EMPLOYEE,
    )
    return employee or None


def invalidate_employee_pin(pin: str) -> None:
    """Call when an employee's attendance_device_id changes."""
    if pin:
        frappe.cache.delete_value(f"biometric:pin_emp:{pin.strip()}")
