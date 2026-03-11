# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations
from frappe.model.document import Document
import frappe


_BRAND_BLOB_FIELD = {
    "EBKN": "ebkn_enroll_data",
    "ZKTeco": "zkteco_enroll_data",
}


class AttendanceDevice(Document):
    def after_insert(self):
        _enqueue_initial_enrollments(self)

    def on_update(self):
        before = self.get_doc_before_save()
        if before and before.disabled and not self.disabled:
            _enqueue_initial_enrollments(self)

    def on_trash(self):
        pass


def _enqueue_initial_enrollments(device: "AttendanceDevice") -> None:
    """When a device is first registered or re-enabled, enqueue Enroll User commands
    for all existing users that have enrollment data for this device's brand."""
    if device.disabled or device.disable_employee_sync:
        return
    brand = device.brand
    blob_field = _BRAND_BLOB_FIELD.get(brand)
    if not blob_field:
        return

    users = frappe.get_all(
        "Attendance Device User",
        filters={blob_field: ["is", "set"]},
        pluck="name",
    )
    # Also include users with allow_in_all_devices
    all_device_users = frappe.get_all(
        "Attendance Device User",
        filters={"allow_in_all_devices": 1, blob_field: ["is", "set"]},
        pluck="name",
    )
    all_users = list(set(users) | set(all_device_users))

    for user_id in all_users:
        _add_command(device.name, user_id, brand, "Enroll User")


def _add_command(device_id: str, user_id: str, brand: str, command_type: str) -> None:
    from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import add_command
    add_command(device_id, user_id, brand, command_type)
