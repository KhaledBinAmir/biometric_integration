# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from typing import Any, Dict, Optional

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


def update_zkteco_enrollment(
    user_doc: "AttendanceDeviceUser",
    device_sn: str,
    *,
    biometric: Optional[Dict[str, Any]] = None,
    card: Optional[str] = None,
    passwd: Optional[str] = None,
) -> None:
    """Merge a biometric template or credential update into the ZKTeco enrollment JSON.

    The enrollment file stores all data needed to fully restore a user to any device:
      {
        "version": 2,
        "card": "0",
        "passwd": "",
        "biometrics": [
          {"type": 1, "no": 0, "index": 0, "size": 512, "valid": 1,
           "duress": 0, "majorver": 10, "minorver": 0, "tmp": "base64..."},
          {"type": 9, "no": 0, "index": 0, "size": 2048, "valid": 1,
           "duress": 0, "majorver": 1,  "minorver": 0, "tmp": "base64..."}
        ]
      }

    biometric type values: 1=Fingerprint, 2=NIR Face, 8=Palm vein, 9=Visible Face
    """
    existing = _load_zkteco_enrollment(user_doc)

    if biometric:
        bio_type = biometric.get("type", 1)
        bio_no = biometric.get("no", 0)
        updated = False
        for i, b in enumerate(existing["biometrics"]):
            if b.get("type") == bio_type and b.get("no") == bio_no:
                existing["biometrics"][i] = biometric
                updated = True
                break
        if not updated:
            existing["biometrics"].append(biometric)

    if card is not None:
        existing["card"] = card
    if passwd is not None:
        existing["passwd"] = passwd

    _save_zkteco_enrollment(user_doc, device_sn, existing)


def save_enrollment_data(
    user_doc: "AttendanceDeviceUser",
    brand: str,
    device_sn: str,
    data: bytes,
) -> None:
    """Save raw enrollment blob for EBKN (binary format).

    For ZKTeco, use update_zkteco_enrollment() instead — it merges into a
    JSON structure that accumulates all fingers, face, card and password.
    """
    blob_field = _BRAND_BLOB_FIELD.get(brand)
    if not blob_field:
        frappe.log_error(f"save_enrollment_data: unsupported brand '{brand}'")
        return

    _write_enrollment_file(user_doc, brand, device_sn, blob_field, data)


def _load_zkteco_enrollment(user_doc: "AttendanceDeviceUser") -> Dict[str, Any]:
    """Load existing ZKTeco enrollment JSON, or return a fresh skeleton."""
    skeleton: Dict[str, Any] = {
        "version": 2,
        "card": "0",
        "passwd": "",
        "biometrics": [],
    }
    file_url = user_doc.get("zkteco_enroll_data")
    if not file_url:
        return skeleton
    file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not file_name:
        return skeleton
    try:
        raw = frappe.get_doc("File", file_name).get_content()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        parsed = json.loads(raw.decode("utf-8"))
        if parsed.get("version") == 2 and "biometrics" in parsed:
            return parsed
    except Exception:
        pass
    return skeleton


def _save_zkteco_enrollment(
    user_doc: "AttendanceDeviceUser",
    device_sn: str,
    data: Dict[str, Any],
) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    _write_enrollment_file(user_doc, "ZKTeco", device_sn, "zkteco_enroll_data", payload)


def _write_enrollment_file(
    user_doc: "AttendanceDeviceUser",
    brand: str,
    device_sn: str,
    blob_field: str,
    data: bytes,
) -> None:
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
            _sync_on_employee_link(doc, before)


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


def _sync_on_employee_link(
    doc: "AttendanceDeviceUser",
    before_doc: "AttendanceDeviceUser",
) -> None:
    """When employee is newly linked, push updated name to ZKTeco devices."""
    if not before_doc.employee and doc.employee:
        from biometric_integration.services.user_sync import on_employee_linked
        on_employee_linked(doc)


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
