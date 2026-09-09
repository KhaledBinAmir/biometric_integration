# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Seed the command guard settings on sites that predate the fields.

`command_dedupe_minutes` is a safety net (it caps runaway command generation) and
an explicit 0 switches it off. A Single doctype that predates the field reads back
as 0, which would look identical to "deliberately disabled" — so seed both fields
with their intended defaults once, and leave any non-zero operator choice alone.
"""

import frappe
from frappe.utils import cint

DEFAULTS = {"command_dedupe_minutes": 10, "command_retention_days": 30}


def execute():
    settings = frappe.get_single("Attendance Integration Settings")
    for field, default in DEFAULTS.items():
        if not cint(settings.get(field)):
            settings.db_set(field, default, update_modified=False)
    frappe.db.commit()
