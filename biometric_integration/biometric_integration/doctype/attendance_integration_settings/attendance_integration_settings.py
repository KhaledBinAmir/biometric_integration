# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from frappe.model.document import Document


class AttendanceIntegrationSettings(Document):
    def on_update(self):
        from biometric_integration.proxy.configurator import (
            enable_listener_logic,
            disable_listener_logic,
        )
        import frappe as _frappe
        site = _frappe.local.site
        if self.proxy_enabled:
            ok, msg = enable_listener_logic(site, int(self.proxy_port or 8998))
            if not ok:
                _frappe.throw(msg)
        else:
            disable_listener_logic(site)


def get_erp_employee_id(device_pin: str) -> str | None:
    """Map a device PIN to an ERP Employee name using the attendance_device_id field.

    This is hardcoded to use Employee.attendance_device_id — the standard HRMS field.
    The query is efficient via the database index that ERPNext maintains on this field.
    """
    if not device_pin:
        return None
    import frappe
    # Normalise: strip leading zeros for numeric pins so "00123" matches "123"
    pin_str = str(device_pin).strip()
    employee = frappe.db.get_value(
        "Employee",
        {"attendance_device_id": pin_str},
        "name",
    )
    return employee or None
