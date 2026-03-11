# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations
from typing import Dict, Optional

import frappe
from frappe.model.document import Document

from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import add_command


_BRAND_BLOB_FIELD: Dict[str, str] = {
    "ZKTeco": "zkteco_enroll_data",
    "EBKN": "ebkn_enroll_data",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_or_create_user_by_pin(pin: str, name: Optional[str] = None) -> "AttendanceDeviceUser":
    """Return an AttendanceDeviceUser by PIN, creating one if it does not exist."""
    if not pin:
        raise ValueError("PIN cannot be empty.")
    doc_name = frappe.db.exists("Attendance Device User", {"user_id": pin})
    if doc_name:
        return frappe.get_doc("Attendance Device User", doc_name)
    doc = frappe.new_doc("Attendance Device User")
    doc.user_id = pin
    if name:
        doc.employee_name = name
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc


def save_enrollment_data(
    user_doc: "AttendanceDeviceUser",
    brand: str,
    device_sn: str,
    data: bytes,
) -> None:
    """Save raw enrollment blob as a private file and link it to the user doc.
    Marks the source device in the child table and queues Enroll User commands
    for all other devices of the same brand.
    """
    blob_field = _BRAND_BLOB_FIELD.get(brand)
    if not blob_field:
        frappe.log_error(f"save_enrollment_data: unsupported brand '{brand}'")
        return

    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": f"{brand.lower()}_enroll_{user_doc.user_id}_{device_sn}.bin",
        "is_private": 1,
        "content": data,
        "attached_to_doctype": "Attendance Device User",
        "attached_to_name": user_doc.name,
    })
    file_doc.insert(ignore_permissions=True)

    user_doc.set(blob_field, file_doc.file_url)

    device_in_table = False
    for row in user_doc.get("devices", []):
        if row.attendance_device == device_sn:
            row.enroll_data_source = 1
            device_in_table = True
        elif row.brand == brand:
            row.enroll_data_source = 0

    if not device_in_table:
        user_doc.append("devices", {
            "attendance_device": device_sn,
            "brand": brand,
            "enroll_data_source": 1,
        })

    user_doc.save(ignore_permissions=True)
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Doctype class
# ---------------------------------------------------------------------------

class AttendanceDeviceUser(Document):
    def after_insert(self):
        _trigger_sync(self, is_new=True)

    def on_update(self):
        _trigger_sync(self, is_new=False)

    def on_trash(self):
        for device_id, brand in _get_user_devices(self).items():
            add_command(device_id, self.name, brand, "Delete User")
        frappe.db.commit()


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def _trigger_sync(doc: "AttendanceDeviceUser", is_new: bool) -> None:
    _sync_on_enrollment_change(doc)
    if not is_new:
        before = doc.get_doc_before_save()
        if before:
            _sync_on_device_list_change(doc, before)


def _sync_on_enrollment_change(doc: "AttendanceDeviceUser") -> None:
    for brand, blob_field in _BRAND_BLOB_FIELD.items():
        if doc.has_value_changed(blob_field) and doc.get(blob_field):
            source_device = next(
                (row.attendance_device for row in doc.devices
                 if row.brand == brand and row.enroll_data_source),
                None,
            )
            for device_id in _get_user_devices(doc, brand=brand):
                if device_id != source_device:
                    add_command(device_id, doc.name, brand, "Enroll User")


def _sync_on_device_list_change(
    doc: "AttendanceDeviceUser",
    before_doc: "AttendanceDeviceUser",
) -> None:
    before = _get_user_devices(before_doc)
    after = _get_user_devices(doc)

    for device_id in set(after) - set(before):
        brand = after[device_id]
        if doc.get(_BRAND_BLOB_FIELD.get(brand, "")):
            add_command(device_id, doc.name, brand, "Enroll User")

    for device_id in set(before) - set(after):
        brand = before[device_id]
        add_command(device_id, doc.name, brand, "Delete User")


def _get_user_devices(
    doc: "AttendanceDeviceUser",
    brand: Optional[str] = None,
) -> Dict[str, str]:
    if doc.allow_in_all_devices:
        filters = {"disabled": 0}
        if brand:
            filters["brand"] = brand
        devices = frappe.get_all("Attendance Device", filters=filters, fields=["name", "brand"])
        return {d.name: d.brand for d in devices}
    return {
        row.attendance_device: row.brand
        for row in doc.get("devices", [])
        if row.attendance_device and (not brand or row.brand == brand)
    }
