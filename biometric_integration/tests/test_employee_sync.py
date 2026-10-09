# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Employee Sync Mode: which devices an employee is put on.

Needs this app installed on the test site:
    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_employee_sync
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from biometric_integration.patches.v2_3 import drop_employee_device_fields
from biometric_integration.services import user_sync

CO_A = "_Test Company"
CO_B = "_Test Company 1"
CO_NONE = "_Test Company 2"  # owns no device
SETTINGS = "Attendance Integration Settings"


class TestEmployeeSync(IntegrationTestCase):
    def setUp(self):
        frappe.db.savepoint("employee_sync")
        # The sync path commits after every step; keep everything in the savepoint.
        self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
        self._no_commit.start()
        self.mode(user_sync.SYNC_DISABLED)
        frappe.db.set_single_value(SETTINGS, "command_dedupe_minutes", 0)
        self.device("ZZSYNC-A1", CO_A)
        self.device("ZZSYNC-A2", CO_A)
        self.device("ZZSYNC-B", CO_B)

    def tearDown(self):
        self._no_commit.stop()
        frappe.db.rollback(save_point="employee_sync")

    # --- helpers -----------------------------------------------------------

    def mode(self, mode):
        frappe.db.set_single_value(SETTINGS, "employee_sync_mode", mode)

    def device(self, serial, company):
        return frappe.get_doc(
            {
                "doctype": "Attendance Device",
                "serial": serial,
                "device_name": serial,
                "brand": "ZKTeco",
                "company": company,
            }
        ).insert(ignore_permissions=True)

    def employee(self, company, pin, status="Active"):
        return frappe.get_doc(
            {
                "doctype": "Employee",
                "first_name": "Sync",
                "last_name": f"ZZ{pin or 'nopin'}",
                "gender": "Other",
                "date_of_birth": "1990-01-01",
                "date_of_joining": "2020-01-01",
                "company": company,
                "status": status,
                "attendance_device_id": pin,
            }
        ).insert(ignore_permissions=True)

    def links(self, pin):
        adu = frappe.db.get_value("Attendance Device User", {"user_id": pin})
        if not adu:
            return []
        return sorted(
            frappe.get_all(
                "Attendance Device Link",
                filters={"parent": adu, "parenttype": "Attendance Device User"},
                pluck="attendance_device",
            )
        )

    def commands(self, pin, command_type):
        return sorted(
            frappe.get_all(
                "Attendance Device Command",
                filters={"attendance_device_user": pin, "command_type": command_type},
                pluck="attendance_device",
            )
        )

    # --- modes -------------------------------------------------------------

    def test_company_mode_puts_new_employee_on_company_devices_only(self):
        self.mode(user_sync.SYNC_COMPANY)
        self.employee(CO_A, "990101")
        self.assertEqual(self.links("990101"), ["ZZSYNC-A1", "ZZSYNC-A2"])
        self.assertEqual(self.commands("990101", "Update User"), ["ZZSYNC-A1", "ZZSYNC-A2"])

    def test_disabled_mode_assigns_nothing(self):
        self.employee(CO_A, "990102")
        self.assertFalse(frappe.db.exists("Attendance Device User", {"user_id": "990102"}))

    def test_all_devices_mode_ignores_company(self):
        self.mode(user_sync.SYNC_ALL)
        self.employee(CO_A, "990103")
        self.assertEqual(self.links("990103"), ["ZZSYNC-A1", "ZZSYNC-A2", "ZZSYNC-B"])

    def test_skips_disabled_and_sync_off_devices(self):
        frappe.db.set_value("Attendance Device", "ZZSYNC-A2", "disable_employee_sync", 1)
        frappe.db.set_value("Attendance Device", "ZZSYNC-B", "disabled", 1)
        self.mode(user_sync.SYNC_ALL)
        self.employee(CO_A, "990104")
        self.assertEqual(self.links("990104"), ["ZZSYNC-A1"])

    def test_inactive_employee_is_not_assigned(self):
        self.mode(user_sync.SYNC_COMPANY)
        self.employee(CO_A, "990105", status="Inactive")
        self.assertEqual(self.links("990105"), [])

    # --- add-only ----------------------------------------------------------

    def test_exception_device_outside_the_company_is_kept(self):
        self.mode(user_sync.SYNC_COMPANY)
        emp = self.employee(CO_A, "990106")
        adu = frappe.get_doc("Attendance Device User", "990106")
        adu.append("devices", {"attendance_device": "ZZSYNC-B", "brand": "ZKTeco"})
        adu.save(ignore_permissions=True)

        emp.reload()
        emp.save(ignore_permissions=True)
        user_sync.reconcile_employee_devices()

        self.assertEqual(self.links("990106"), ["ZZSYNC-A1", "ZZSYNC-A2", "ZZSYNC-B"])
        self.assertEqual(self.commands("990106", "Delete User"), [])

    # --- reconcile ---------------------------------------------------------

    def test_reconcile_previews_then_adds_then_is_a_no_op(self):
        self.employee(CO_A, "990107")  # created while sync is Disabled
        self.mode(user_sync.SYNC_COMPANY)

        preview = user_sync.reconcile_employee_devices(dry_run=True)
        self.assertIn("990107", " ".join(preview["people"]))
        self.assertFalse(frappe.db.exists("Attendance Device User", {"user_id": "990107"}))

        user_sync.reconcile_employee_devices()
        self.assertEqual(self.links("990107"), ["ZZSYNC-A1", "ZZSYNC-A2"])
        queued = frappe.db.count("Attendance Device Command", {"attendance_device_user": "990107"})

        again = user_sync.reconcile_employee_devices()
        self.assertNotIn("990107", " ".join(again["people"]))
        self.assertEqual(
            frappe.db.count("Attendance Device Command", {"attendance_device_user": "990107"}), queued
        )

    def test_reconcile_reports_employees_without_pin(self):
        emp = self.employee(CO_A, None)
        self.mode(user_sync.SYNC_COMPANY)
        summary = user_sync.reconcile_employee_devices(dry_run=True)
        self.assertIn(emp.name, " ".join(summary["no_pin"]))

    def test_new_device_gets_its_company_employees(self):
        self.mode(user_sync.SYNC_COMPANY)
        self.employee(CO_A, "990108")
        self.employee(CO_B, "990109")
        self.device("ZZSYNC-A3", CO_A)
        self.assertIn("ZZSYNC-A3", self.links("990108"))
        self.assertIn("ZZSYNC-A3", self.commands("990108", "Update User"))
        self.assertNotIn("ZZSYNC-A3", self.links("990109"))

    # --- lifecycle and conflicts -------------------------------------------

    def test_reactivation_without_enrollment_recreates_the_user(self):
        self.mode(user_sync.SYNC_COMPANY)
        emp = self.employee(CO_A, "990110")
        emp.status = "Left"
        emp.relieving_date = "2020-06-01"
        emp.save(ignore_permissions=True)
        self.assertEqual(self.commands("990110", "Delete User"), ["ZZSYNC-A1", "ZZSYNC-A2"])

        frappe.db.delete("Attendance Device Command", {"attendance_device_user": "990110"})
        emp.status = "Active"
        emp.relieving_date = None
        emp.save(ignore_permissions=True)
        self.assertEqual(self.commands("990110", "Update User"), ["ZZSYNC-A1", "ZZSYNC-A2"])

    def test_pin_owned_by_another_employee_is_not_synced(self):
        # Employee PINs are unique, so this happens when the owner's PIN moved on
        # and their device user (PIN 990111) stayed behind for the next person.
        self.mode(user_sync.SYNC_COMPANY)
        owner = self.employee(CO_A, "990111")
        other = self.employee(CO_B, "990112")
        frappe.db.set_value("Employee", owner.name, "attendance_device_id", "990115")
        frappe.db.set_value("Employee", other.name, "attendance_device_id", "990111")
        other.reload()
        self.assertRaises(user_sync.SyncConflict, user_sync.ensure_on_devices, other)
        adu = frappe.db.get_value("Attendance Device User", {"user_id": "990111"}, "employee")
        self.assertEqual(adu, owner.name)

    def test_reconcile_reports_a_non_numeric_pin_instead_of_pushing_it(self):
        self.employee(CO_A, "AB14")  # accepted while sync is Disabled
        self.mode(user_sync.SYNC_COMPANY)
        summary = user_sync.reconcile_employee_devices()
        self.assertIn("AB14", " ".join(summary["conflicts"]))
        self.assertFalse(frappe.db.exists("Attendance Device User", {"user_id": "AB14"}))

    def test_unlinked_device_user_with_the_pin_gets_linked(self):
        # A PIN's first punch auto-creates a device user with no employee.
        frappe.get_doc(
            {
                "doctype": "Attendance Device User",
                "user_id": "990120",
                "devices": [
                    {"attendance_device": "ZZSYNC-A1", "brand": "ZKTeco"},
                    {"attendance_device": "ZZSYNC-A2", "brand": "ZKTeco"},
                ],
            }
        ).insert(ignore_permissions=True)
        self.mode(user_sync.SYNC_COMPANY)
        emp = self.employee(CO_A, "990120")
        self.assertEqual(frappe.db.get_value("Attendance Device User", "990120", "employee"), emp.name)

    # --- device registration and re-enabling --------------------------------

    def test_reenabled_device_gets_its_assigned_users_back(self):
        self.mode(user_sync.SYNC_COMPANY)
        self.employee(CO_A, "990121")
        frappe.db.delete("Attendance Device Command", {"attendance_device_user": "990121"})
        device = frappe.get_doc("Attendance Device", "ZZSYNC-A1")
        device.disabled = 1
        device.save(ignore_permissions=True)
        device.disabled = 0
        device.save(ignore_permissions=True)  # back from repair, wiped
        self.assertEqual(self.commands("990121", "Update User"), ["ZZSYNC-A1"])

    def test_reenabled_device_restores_manual_assignments_when_sync_is_disabled(self):
        frappe.get_doc(
            {
                "doctype": "Attendance Device User",
                "user_id": "990122",
                "devices": [{"attendance_device": "ZZSYNC-B", "brand": "ZKTeco"}],
            }
        ).insert(ignore_permissions=True)
        device = frappe.get_doc("Attendance Device", "ZZSYNC-B")
        device.disabled = 1
        device.save(ignore_permissions=True)
        device.disabled = 0
        device.save(ignore_permissions=True)
        self.assertEqual(self.commands("990122", "Update User"), ["ZZSYNC-B"])

    def test_pin_change_does_not_break_the_employee_save(self):
        self.mode(user_sync.SYNC_COMPANY)
        emp = self.employee(CO_A, "990113")
        emp.attendance_device_id = "990114"
        emp.save(ignore_permissions=True)  # must not raise on the unique employee link
        self.assertEqual(self.links("990113"), ["ZZSYNC-A1", "ZZSYNC-A2"])
        self.assertFalse(frappe.db.exists("Attendance Device User", {"user_id": "990114"}))

    def test_non_numeric_pin_is_refused_only_when_synced(self):
        self.mode(user_sync.SYNC_COMPANY)
        self.assertRaises(frappe.ValidationError, self.employee, CO_A, "AB12")
        self.employee(CO_NONE, "AB13")  # no device for this company: an RF tag id is fine

    # --- migration ---------------------------------------------------------

    def test_patch_refuses_while_a_script_reads_the_removed_fields(self):
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": "ZZ Sync Field Reader",
                "dt": "Employee",
                "enabled": 1,
                "script": "frappe.ui.form.on('Employee', {refresh(frm) { frm.doc.biometric_device; }});",
            }
        ).insert(ignore_permissions=True)
        self.assertRaises(frappe.ValidationError, drop_employee_device_fields.execute)

    def test_patch_guard_ignores_longer_names(self):
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": "ZZ Sync Similar Name",
                "dt": "Employee",
                "enabled": 1,
                "script": "// reads biometric_device_user and biometric_devices only",
            }
        ).insert(ignore_permissions=True)
        drop_employee_device_fields._refuse_while_still_read()  # must not raise

    def test_patch_snapshot_keeps_the_old_values(self):
        emp = self.employee(CO_A, "990123")
        drop_employee_device_fields._snapshot(["status"])  # any existing column will do
        content = frappe.get_doc(
            "File",
            {"file_name": "employee_device_fields_before_v2_3.json", "attached_to_doctype": SETTINGS},
        ).get_content()
        self.assertIn(emp.name, content)
