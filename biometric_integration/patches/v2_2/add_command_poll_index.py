# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Add the composite index the device-poll query actually needs.

Every device poll asks "what is the next non-terminal command for THIS device,
oldest first". The doctype only indexed `attendance_device` (and Frappe's stock
`creation`/`modified`), so MariaDB had two bad choices: walk every historical
row for the device and filter by status, or walk the whole table in `creation`
order looking for a match — which, for a device with nothing pending, means
scanning the entire table on every single poll. With 38 devices polling every
10s against 660k rows that pinned all 8 cores (MKE, Sep 2026).

(attendance_device, status, creation) answers the query from the index alone:
seek straight to this device's non-terminal rows, already in creation order,
stop at the first hit.
"""

import frappe


def execute():
    frappe.db.add_index(
        "Attendance Device Command",
        ["attendance_device", "status", "creation"],
        index_name="adc_device_status_creation",
    )
