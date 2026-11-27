# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""The name typed on the device, and the one-way PIN mapping.

The device reports the name it shows on its own keypad. It is kept in
``device_user_name`` so the ERP list shows exactly what the terminal shows, and
it survives linking the record to an Employee (whose name is a separate field).

    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_device_user_name
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from biometric_integration.adapters.zkteco import ZKTecoAdapter
from biometric_integration.services.command_processor import process_device_command

SERIAL = "ZZNAME-A"
SETTINGS = "Attendance Integration Settings"


def user_line(pin, name, pri=0):
    """A `tabledata` user row, tab-separated as the firmware sends it."""
    return (
        f"user uid={pin}\tcardno=\tpin={pin}\tpassword=\tgroup=1\tstarttime=0"
        f"\tendtime=0\tname={name}\tprivilege={pri}\tdisable=0\tverify=0"
    )


class TestDeviceUserName(IntegrationTestCase):
    def setUp(self):
        frappe.db.savepoint("device_user_name")
        # The request handlers commit; keep everything in the savepoint.
        self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
        self._no_commit.start()
        frappe.db.set_single_value(SETTINGS, "employee_sync_mode", "Disabled")
        frappe.db.set_single_value(SETTINGS, "command_dedupe_minutes", 0)
        frappe.get_doc(
            {
                "doctype": "Attendance Device",
                "serial": SERIAL,
                "device_name": SERIAL,
                "brand": "ZKTeco",
            }
        ).insert(ignore_permissions=True)

    def tearDown(self):
        self._no_commit.stop()
        frappe.db.rollback(save_point="device_user_name")

    # --- helpers -----------------------------------------------------------

    def upload(self, body):
        environ = EnvironBuilder(
            method="POST",
            path="/iclock/cdata",
            query_string=f"SN={SERIAL}&table=tabledata&tablename=user&count=1",
            data=body.encode(),
        ).get_environ()
        return ZKTecoAdapter(Request(environ)).dispatch().get_data(as_text=True)

    def user(self, pin):
        return frappe.get_doc("Attendance Device User", {"user_id": pin})

    def employee(self, pin, first="Rosa", last="Morel"):
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        return frappe.get_doc(
            {
                "doctype": "Employee",
                "first_name": first,
                "last_name": last,
                "gender": "Other",
                "date_of_birth": "1990-01-01",
                "date_of_joining": "2020-01-01",
                "company": company,
                "status": "Active",
                "attendance_device_id": pin,
            }
        ).insert(ignore_permissions=True)

    # --- tests -------------------------------------------------------------

    def test_name_typed_on_the_device_is_stored_and_shown(self):
        self.assertEqual(self.upload(user_line("990501", "Rosa Morel")), "user=1")
        doc = self.user("990501")
        self.assertEqual(doc.device_user_name, "Rosa Morel")
        self.assertEqual(doc.display_name, "Rosa Morel")
        self.assertFalse(doc.employee)

    def test_rename_on_the_device_updates_the_visible_name(self):
        self.upload(user_line("990502", "Rosa M."))
        self.upload(user_line("990502", "Rosa Morel"))
        doc = self.user("990502")
        self.assertEqual(doc.device_user_name, "Rosa Morel")
        self.assertEqual(doc.display_name, "Rosa Morel")

    def test_device_name_does_not_overwrite_the_employee_name(self):
        employee = self.employee("990503")
        frappe.get_doc(
            {
                "doctype": "Attendance Device User",
                "user_id": "990503",
                "employee": employee.name,
            }
        ).insert(ignore_permissions=True)

        self.upload(user_line("990503", "Keypad Rosa"))

        doc = self.user("990503")
        self.assertEqual(doc.device_user_name, "Keypad Rosa")
        self.assertEqual(doc.employee_name, employee.employee_name)
        self.assertEqual(doc.display_name, "Keypad Rosa")

    def test_privilege_reported_by_the_device_is_stored(self):
        # Frappe 16 has no is_dirty(); the save used to raise inside the per-line
        # except, so the privilege was logged and dropped.
        self.assertEqual(self.upload(user_line("990506", "Rosa Morel", pri=14)), "user=1")
        self.assertEqual(self.user("990506").privilege, "Super Admin")

    def test_employee_link_is_read_only(self):
        field = frappe.get_meta("Attendance Device User").get_field("employee")
        self.assertTrue(field.read_only)
        self.assertIn("Attendance Device ID", field.description)

    def test_update_user_falls_back_to_the_device_name(self):
        frappe.get_doc(
            {
                "doctype": "Attendance Device User",
                "user_id": "990504",
                "device_user_name": "Rosa Morel",
            }
        ).insert(ignore_permissions=True)

        command = frappe.get_doc(
            {
                "doctype": "Attendance Device Command",
                "attendance_device": SERIAL,
                "attendance_device_user": "990504",
                "brand": "ZKTeco",
                "command_type": "Update User",
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)

        line = process_device_command(SERIAL)
        self.assertIn(f"C:{command.name}:", line)
        self.assertIn("Name=Rosa Morel", line)
