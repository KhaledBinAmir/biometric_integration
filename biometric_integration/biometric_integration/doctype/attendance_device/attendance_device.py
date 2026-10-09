# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations

from frappe.model.document import Document

from biometric_integration.utils.device_cache import invalidate_device_cache

_BRAND_BLOB_FIELD = {
    "EBKN": "ebkn_enroll_data",
    "ZKTeco": "zkteco_enroll_data",
}


class AttendanceDevice(Document):
    def after_insert(self):
        # Clear any cached "unregistered" verdict so the device is accepted on its next poll.
        invalidate_device_cache(self.name)
        _enqueue_initial_enrollments(self)

    def on_update(self):
        before = self.get_doc_before_save()
        if before and before.disabled and not self.disabled:
            _enqueue_initial_enrollments(self)

    def on_trash(self):
        # Drop cached registration / sync state for the deleted serial.
        invalidate_device_cache(self.name)


def _enqueue_initial_enrollments(device: "AttendanceDevice") -> None:
    """When a device is first registered or re-enabled, put its assigned users
    back on it and add whoever else belongs there per the employee sync mode.

    This used to queue Enroll User for every user with stored enrollment data,
    whatever their company, which on a multi-company site put everyone on every
    new device."""
    if device.disabled or device.disable_employee_sync or device.brand not in _BRAND_BLOB_FIELD:
        return
    from biometric_integration.services.user_sync import push_device_roster

    push_device_roster(device.name)
