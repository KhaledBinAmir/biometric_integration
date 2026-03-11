# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Rename 'Attendance Device Settings' → 'Attendance Integration Settings'.

Runs pre_model_sync so the old DocType record is updated before Frappe
tries to create the new one from the renamed JSON. Uses frappe.rename_doc
which handles tabSingles rows and all cross-doctype link references.
"""

import frappe


def execute():
    if frappe.db.exists("DocType", "Attendance Device Settings"):
        frappe.rename_doc(
            "DocType",
            "Attendance Device Settings",
            "Attendance Integration Settings",
            force=True,
        )
        frappe.db.commit()
