# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Drop the Employee `create_user_in_device` and `biometric_device` fields.

Which devices an employee is put on is now a site-wide choice (Attendance
Integration Settings > Employee Sync Mode). The per-employee checkbox was the
step HR forgot, and because it also made the PIN mandatory (checked in the
browser before the server could derive the PIN) new hires failed to save with
it ticked, so they never reached a device. The per-employee device picker had
mostly gone stale. Exceptions now live on the Attendance Device User's device
list, which sync never removes.

Before dropping, the old values are kept in a private File attached to
Attendance Integration Settings, so who was ticked and which device they had
picked can still be looked up.

Refuses to run while an enabled script or notification still reads the
fields: once they are gone `doc.create_user_in_device` raises AttributeError,
and a DocType Event script on Employee would then block every Employee save.
"""

import json
import re

import frappe
from frappe import _
from frappe.query_builder import Criterion

FIELDS = ("create_user_in_device", "biometric_device")
MANDATORY_PIN = "Employee-attendance_device_id-mandatory_depends_on"
SETTINGS = "Attendance Integration Settings"

# (doctype, field holding code, filters for "active")
_READERS = (
    ("Server Script", "script", {"disabled": 0}),
    ("Client Script", "script", {"enabled": 1}),
    ("Notification", "condition", {"enabled": 1}),
)


def execute():
    _refuse_while_still_read()

    columns = [f for f in FIELDS if frappe.db.has_column("Employee", f)]
    if columns:
        _snapshot(columns)

    for fieldname in FIELDS:
        name = f"Employee-{fieldname}"
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
        # Frappe never drops the column of a deleted Custom Field (data safety).
        if frappe.db.has_column("Employee", fieldname):
            frappe.db.sql_ddl(f"ALTER TABLE `tabEmployee` DROP COLUMN `{fieldname}`")

    # Only remove the rule this app shipped; a site that set its own stays.
    if frappe.db.get_value("Property Setter", MANDATORY_PIN, "value") == "eval:doc.create_user_in_device":
        frappe.delete_doc("Property Setter", MANDATORY_PIN, ignore_permissions=True, force=True)

    frappe.clear_cache(doctype="Employee")
    frappe.db.commit()


def _snapshot(columns):
    emp = frappe.qb.DocType("Employee")
    rows = (
        frappe.qb.from_(emp)
        .select(
            emp.name,
            emp.employee_name,
            emp.company,
            emp.status,
            emp.attendance_device_id,
            *[emp[c] for c in columns],
        )
        .where(Criterion.any([(emp[c].isnotnull()) & (emp[c] != "") & (emp[c] != "0") for c in columns]))
        .run(as_dict=True)
    )
    if not rows:
        return
    frappe.get_doc(
        {
            "doctype": "File",
            "file_name": "employee_device_fields_before_v2_3.json",
            "is_private": 1,
            "content": json.dumps(rows, indent=1, default=str),
            "attached_to_doctype": SETTINGS,
            "attached_to_name": SETTINGS,
        }
    ).insert(ignore_permissions=True)


def _refuse_while_still_read():
    pattern = re.compile(r"\b(" + "|".join(FIELDS) + r")\b")
    in_use = []
    for doctype, code_field, active in _READERS:
        if not frappe.db.table_exists(doctype):
            continue
        for fieldname in FIELDS:
            for row in frappe.get_all(
                doctype,
                filters={**active, code_field: ["like", f"%{fieldname}%"]},
                fields=["name", code_field],
            ):
                # LIKE finds candidates; a whole-word match keeps names such as
                # biometric_device_user from blocking the migration.
                found = sorted(set(pattern.findall(row.get(code_field) or "")))
                if found:
                    in_use.append(f"{doctype} '{row.name}' uses {', '.join(found)}")
    if in_use:
        frappe.throw(
            _(
                "Employee fields create_user_in_device and biometric_device are being removed, but "
                "these still read them and would break every Employee save: {0}. Update or disable "
                "them, then run the migration again."
            ).format("; ".join(sorted(set(in_use))))
        )
