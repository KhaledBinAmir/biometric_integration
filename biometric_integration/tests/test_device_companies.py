# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""The attendance_device_companies hook: companies whose employees can punch.

Needs this app installed on the test site:
    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_device_companies
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from biometric_integration.api import attendance_device_companies

CO_ENABLED = "_Test Company"
CO_DISABLED = "_Test Company 1"  # its only device is disabled


class TestDeviceCompanies(IntegrationTestCase):
    def setUp(self):
        frappe.db.savepoint("device_companies")
        self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
        self._no_commit.start()
        frappe.db.set_value("Attendance Device", {"company": CO_DISABLED}, "disabled", 1)
        self.device("ZZDC-ON", CO_ENABLED)
        self.device("ZZDC-OFF", CO_DISABLED, disabled=1)

    def tearDown(self):
        self._no_commit.stop()
        frappe.db.rollback(save_point="device_companies")

    def device(self, serial, company, disabled=0):
        return frappe.get_doc(
            {
                "doctype": "Attendance Device",
                "serial": serial,
                "device_name": serial,
                "brand": "ZKTeco",
                "company": company,
                "disabled": disabled,
            }
        ).insert(ignore_permissions=True)

    def test_enabled_devices_only(self):
        companies = attendance_device_companies()
        self.assertIn(CO_ENABLED, companies)
        self.assertNotIn(CO_DISABLED, companies)
        self.assertEqual(len(companies), len(set(companies)))

    def test_answers_the_hook(self):
        self.assertIn(
            "biometric_integration.api.attendance_device_companies",
            frappe.get_hooks("attendance_device_companies"),
        )
