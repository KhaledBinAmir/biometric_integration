# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Attendance Monitor and Attendance Request punches.

A shift correction (pidyen_roster) keeps the punches it replaces and marks them
`superseded_by` the request; an approved request adds MANUAL-<request> punches.
The monitor shows both, counts only the punches that still count, and leaves
both kinds to their request.

Needs the `superseded_by` column (pidyen_roster). Runs on the shared test site
even where this app is not installed (it only reads HRMS doctypes):
    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_attendance_monitor_corrections
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from biometric_integration import attendance_monitor

REQUEST = "ZZB-ARQ-0001"


class TestMonitorCorrections(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.has_column("Employee Checkin", "superseded_by"):
            raise unittest.SkipTest("Employee Checkin.superseded_by is missing (pidyen_roster)")
        cls.company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
        cls.employee = (
            frappe.get_doc(
                {
                    "doctype": "Employee",
                    "first_name": "Monitor",
                    "last_name": "ZZB",
                    "gender": "Other",
                    "date_of_birth": "1990-01-01",
                    "date_of_joining": "2020-01-01",
                    "company": cls.company,
                    "status": "Active",
                }
            )
            .insert(ignore_permissions=True)
            .name
        )
        cls.day = getdate(add_days(today(), -2))

    def setUp(self):
        frappe.db.savepoint("monitor_corrections")
        self.clock_in = self.punch("09:00:00")
        self.replaced = self.punch("16:00:00", superseded_by=REQUEST)
        self.manual = self.punch("17:00:00", device_id=f"MANUAL-{REQUEST}")

    def tearDown(self):
        frappe.db.rollback(save_point="monitor_corrections")

    def punch(self, time, device_id=None, superseded_by=None):
        doc = frappe.get_doc(
            {"doctype": "Employee Checkin", "employee": self.employee, "time": f"{self.day} {time}"}
        )
        doc.device_id = device_id
        doc.flags.ignore_links = True
        doc.insert(ignore_permissions=True)
        if superseded_by:
            frappe.db.set_value("Employee Checkin", doc.name, "superseded_by", superseded_by)
        return doc.name

    def row(self):
        rows = attendance_monitor.get_attendance_monitor(self.day, company=self.company, mode="pairs")
        return next(r for r in rows if r["employee"] == self.employee)

    def test_a_replaced_punch_is_shown_but_not_counted(self):
        for row in (self.row(), attendance_monitor._employee_day_row(self.employee, self.day)):
            self.assertEqual([c["name"] for c in row["checkins"]], [self.clock_in, self.manual])
            self.assertEqual(
                [(c["name"], c["superseded_by"]) for c in row["superseded"]], [(self.replaced, REQUEST)]
            )
            # 09:00 to 17:00; counting the replaced 16:00 would leave an odd punch.
            self.assertEqual(row["work_hours"], 8.0)
            self.assertIsNone(row["flag"])
            self.assertEqual([c["request"] for c in row["checkins"]], [None, REQUEST])

    def test_request_punches_are_not_edited_here(self):
        # The endpoints commit after a change: never here, whatever happens.
        with (
            patch.object(attendance_monitor, "_corrections_enabled", return_value=True),
            patch.object(frappe.db, "commit"),
        ):
            for name, pattern in ((self.replaced, "replaced by the correction"), (self.manual, "comes from")):
                with self.assertRaisesRegex(frappe.ValidationError, pattern):
                    attendance_monitor.update_checkin(name, f"{self.day} 15:00:00")
                with self.assertRaisesRegex(frappe.ValidationError, pattern):
                    attendance_monitor.delete_checkin(name)
        self.assertTrue(frappe.db.exists("Employee Checkin", self.replaced))
        self.assertTrue(frappe.db.exists("Employee Checkin", self.manual))
