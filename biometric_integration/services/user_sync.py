# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
User sync service: reacts to Employee lifecycle events and propagates changes
to biometric devices.

Which devices an employee belongs on is a site-wide decision, set by
`Attendance Integration Settings.employee_sync_mode`:

  Disabled         → nothing is assigned automatically; device links are kept by
                     hand on the Attendance Device User
  Company Devices  → every enabled device owned by the employee's company
  All Devices      → every enabled device

An employee is in scope when they are Active and have an Attendance Device ID
(their PIN). Assignment is add-only: sync puts people on devices but never takes
them off. A per-person exception (someone who also clocks at another company's
device) is a row on their Attendance Device User, and sync leaves it alone.

  Employee save (in scope)         → link missing target devices, push the user
  Employee goes inactive/left      → queue Delete User on all their devices
  Employee reactivated             → re-push to all their devices
  Employee name changes            → queue Update User on all their devices
  Attendance Device User linked    → queue Update User on all their devices
  Daily / "Sync Employees" button  → reconcile_employee_devices() for everyone
  Device registered / re-enabled   → push_device_roster(): its assigned users
                                     back on it, then the reconcile for it
"""

from __future__ import annotations

from typing import Optional

import frappe

from biometric_integration.biometric_integration.doctype.attendance_device_command.attendance_device_command import (
    add_command,
)
from biometric_integration.utils.device_cache import invalidate_employee_pin

_INACTIVE_STATUSES = {"Left", "Inactive"}
_BRAND_BLOB_FIELD = {"ZKTeco": "zkteco_enroll_data", "EBKN": "ebkn_enroll_data"}

SYNC_DISABLED = "Disabled"
SYNC_COMPANY = "Company Devices"
SYNC_ALL = "All Devices"


class SyncConflict(Exception):
    """The employee cannot be put on a device as they are; the message says why."""


def get_sync_mode() -> str:
    """The site's employee sync mode. Unset reads as Disabled, so a site that
    predates the setting never starts assigning people until someone chooses."""
    mode = frappe.db.get_single_value("Attendance Integration Settings", "employee_sync_mode")
    return mode if mode in (SYNC_COMPANY, SYNC_ALL) else SYNC_DISABLED


# ---------------------------------------------------------------------------
# Employee hooks
# ---------------------------------------------------------------------------


def validate_employee(doc, method=None) -> None:
    """Frappe doc_events validate hook — runs before save.

    Device PINs must be numeric, but only for employees that will actually be
    put on a device: elsewhere attendance_device_id may hold an RF tag id.
    The targets are kept on the doc for on_update, so a save resolves them once.
    """
    doc.flags.biometric_targets = _resolve_target_devices(doc)
    pin = _pin(doc)
    if not pin or pin.isdigit() or not doc.flags.biometric_targets:
        return
    frappe.throw(
        frappe._(
            "Attendance Device ID must be a numeric value. "
            "ZKTeco and EBKN devices only support integer user IDs."
        ),
        title=frappe._("Invalid Device ID"),
    )


def on_employee_update(doc, method=None) -> None:
    """Frappe doc_events on_update hook — called on every Employee save."""
    before = doc.get_doc_before_save()

    # Keep the PIN→Employee cache honest when the device-ID mapping changes, so a
    # freshly-mapped employee's punches are attributed immediately (the mapping is
    # otherwise cached in Redis for up to 5 minutes).
    pin_after = _pin(doc)
    pin_before = _pin(before) if before else ""
    if pin_after != pin_before:
        for pin in (pin_before, pin_after):
            if pin:
                invalidate_employee_pin(pin)

    # --- Status / name changes on devices the person is already on ---
    if before:
        user_doc = _find_device_user(doc.name)
        if user_doc:
            status_before = before.status or "Active"
            status_after = doc.status or "Active"
            name_before = before.employee_name or ""
            name_after = doc.employee_name or ""

            if status_before != status_after:
                if status_after in _INACTIVE_STATUSES:
                    _delete_from_all_devices(user_doc)
                elif status_after == "Active" and status_before in _INACTIVE_STATUSES:
                    _re_enroll_on_all_devices(user_doc)

            if name_before != name_after and status_after not in _INACTIVE_STATUSES:
                _update_user_info(user_doc)

    # --- Put the person on every device they belong on (add-only) ---
    # Runs on every save, not only when something changed: it only acts on
    # devices the person is missing from, so a save after a new device was
    # added (or after a failed earlier attempt) closes the gap by itself.
    targets = doc.flags.biometric_targets
    if targets is None:
        targets = _resolve_target_devices(doc)
    if not targets:
        return

    relevant_change = not before or any(
        (before.get(f) or "") != (doc.get(f) or "") for f in ("attendance_device_id", "company", "status")
    )
    frappe.db.savepoint("bi_employee_sync")
    try:
        ensure_on_devices(doc, targets=targets)
    except SyncConflict as conflict:
        # Logged when the save touched the PIN, company or status, so a standing
        # conflict does not add an Error Log on every unrelated edit.
        if relevant_change:
            frappe.log_error(title="biometric_integration: employee not synced", message=str(conflict))
    except Exception as exc:
        # A sync problem must not stop HR saving the employee: undo only the sync
        # work, log it, and let the daily reconcile try again. If that undo is
        # impossible (the work was committed, or the transaction is gone), the
        # save must fail visibly rather than report success.
        try:
            frappe.db.rollback(save_point="bi_employee_sync")
        except Exception:
            raise exc from None
        frappe.log_error(title="biometric_integration: employee sync failed", message=doc.name)


# ---------------------------------------------------------------------------
# Called from attendance_device_user.py when employee link is set
# ---------------------------------------------------------------------------


def on_employee_linked(user_doc) -> None:
    """Sync updated employee name to all devices when user gets linked to employee."""
    _update_user_info(user_doc)


# ---------------------------------------------------------------------------
# Device assignment
# ---------------------------------------------------------------------------


def _pin(employee_doc) -> str:
    return str(employee_doc.get("attendance_device_id") or "").strip()


def _resolve_target_devices(employee_doc, mode: Optional[str] = None) -> dict:
    """Return {device_id: brand} for every device this employee belongs on.

    Empty when the employee is out of scope: sync disabled, not Active, or no
    PIN. Disabled devices and devices with Disable Employee Sync are never
    targets, which keeps a unit that is switched off (awaiting RMA, being
    repurposed) out of every sync.
    """
    mode = mode or get_sync_mode()
    if mode == SYNC_DISABLED or not _pin(employee_doc):
        return {}
    if (employee_doc.get("status") or "Active") != "Active":
        return {}

    filters = {"disabled": 0, "disable_employee_sync": 0}
    if mode == SYNC_COMPANY:
        company = employee_doc.get("company")
        if not company:
            return {}
        filters["company"] = company
    return {
        d.name: d.brand
        for d in frappe.get_all("Attendance Device", filters=filters, fields=["name", "brand"])
        if d.brand
    }


def ensure_on_devices(employee_doc, targets: Optional[dict] = None, dry_run: bool = False) -> list:
    """Link the employee's Attendance Device User to every target device it is
    missing from and push the user there. Returns the devices that were (or, in a
    dry run, would be) added. Raises SyncConflict when the employee cannot be
    synced as they are. Does not commit; the caller does.

    Add-only: devices already on the Attendance Device User stay, including ones
    outside the employee's company. A person with a stored enrollment gets
    Enroll User on each new device (queued by the Attendance Device User's own
    device-list hook); everyone else gets Update User, which creates them on the
    device by PIN and name so they can enrol a finger there.
    """
    if targets is None:
        targets = _resolve_target_devices(employee_doc)
    if not targets:
        return []

    pin = _pin(employee_doc)
    _check_conflicts(employee_doc, pin)

    # ignore_permissions below: this runs as whoever saved the Employee (often an
    # HR user with no rights on Attendance Device User); the sync is the app's
    # own bookkeeping, not an action on that user's behalf.

    existing_name = frappe.db.get_value("Attendance Device User", {"user_id": pin})
    if existing_name:
        user_doc = frappe.get_doc("Attendance Device User", existing_name)
        linked = {row.attendance_device for row in user_doc.get("devices", [])}
        missing = {d: b for d, b in targets.items() if d not in linked}
        needs_link = not user_doc.employee
        if dry_run or not (missing or needs_link):
            return list(missing)
        if needs_link:
            # e.g. a device user auto-created by the PIN's first punch; linking
            # it also pushes the employee's name to their devices.
            user_doc.employee = employee_doc.name
            user_doc.employee_name = employee_doc.employee_name
        for device_id, brand in missing.items():
            user_doc.append("devices", {"attendance_device": device_id, "brand": brand})
        user_doc.save(ignore_permissions=True)
    else:
        missing = dict(targets)
        if dry_run:
            return list(missing)
        user_doc = frappe.get_doc(
            {
                "doctype": "Attendance Device User",
                "user_id": pin,
                "employee": employee_doc.name,
                "employee_name": employee_doc.employee_name,
                "devices": [{"attendance_device": d, "brand": b} for d, b in missing.items()],
            }
        )
        user_doc.insert(ignore_permissions=True)

    for device_id, brand in missing.items():
        if not user_doc.get(_BRAND_BLOB_FIELD.get(brand, "")):
            add_command(device_id, user_doc.name, brand, "Update User")
    return list(missing)


def _check_conflicts(employee_doc, pin: str) -> None:
    """Raise SyncConflict when this employee cannot be put on a device under
    this PIN.

    Devices only take integer user ids. Attendance Device User is unique per
    PIN and per employee: pushing a PIN that belongs to someone else's device
    user would rename that person on the device, and an employee whose device
    user carries an older PIN would need a second one, which the unique
    employee link refuses.
    """
    if not pin.isdigit():
        raise SyncConflict(
            f"Employee {employee_doc.name} has Attendance Device ID {pin!r}; devices only accept "
            "numbers. Not synced."
        )
    owner = frappe.db.get_value("Attendance Device User", {"user_id": pin}, "employee")
    if owner and owner != employee_doc.name:
        raise SyncConflict(
            f"Employee {employee_doc.name} has Attendance Device ID {pin}, but the device user "
            f"with that PIN belongs to {owner}. Not synced."
        )
    own = frappe.db.get_value("Attendance Device User", {"employee": employee_doc.name}, "user_id")
    if own and own != pin:
        raise SyncConflict(
            f"Employee {employee_doc.name} has Attendance Device ID {pin}, but their device user "
            f"has PIN {own}. Not synced; fix the PIN on one of the two."
        )


def reconcile_employee_devices(
    dry_run: bool = False, device: Optional[str] = None, commit: bool = True
) -> dict:
    """Put every in-scope employee on every device they are missing from.

    Scheduled daily and behind the "Sync Employees" button. Idempotent and
    add-only; a second run right after the first changes nothing. `device`
    limits the run to one device (used when a device is registered or
    re-enabled). As a job it commits per employee, so a failure late in a long
    run keeps the work before it; called from a device's own save (commit=False)
    it leaves the commit to that request. Returns a summary for the UI.
    """
    mode = get_sync_mode()
    summary = {
        "mode": mode,
        "dry_run": bool(dry_run),
        "employees": 0,
        "added": {},
        "people": [],
        "no_pin": [],
        "conflicts": [],
    }
    if mode == SYNC_DISABLED:
        return summary

    device_filters = {"disabled": 0, "disable_employee_sync": 0}
    if device:
        device_filters["name"] = device
    devices = frappe.get_all(
        "Attendance Device", filters=device_filters, fields=["name", "device_name", "brand", "company"]
    )
    if not devices:
        return summary
    summary["device_names"] = {d.name: d.device_name or d.name for d in devices}

    emp_filters = {"status": "Active"}
    if mode == SYNC_COMPANY:
        companies = sorted({d.company for d in devices if d.company})
        if not companies:
            return summary
        emp_filters["company"] = ["in", companies]

    for emp in frappe.get_all(
        "Employee",
        filters=emp_filters,
        fields=["name", "employee_name", "company", "status", "attendance_device_id"],
        order_by="name asc",
    ):
        if not _pin(emp):
            summary["no_pin"].append(f"{emp.name} {emp.employee_name}")
            continue
        targets = {
            d.name: d.brand for d in devices if d.brand and (mode == SYNC_ALL or d.company == emp.company)
        }
        if not targets:
            continue
        summary["employees"] += 1
        # One bad record must not stop everyone else being synced. Roll back
        # only this employee's work: this can run inside a device's insert.
        frappe.db.savepoint("bi_sync_employee")
        try:
            added = ensure_on_devices(emp, targets=targets, dry_run=dry_run)
        except SyncConflict as conflict:
            summary["conflicts"].append(str(conflict))
            continue
        except Exception:
            try:
                frappe.db.rollback(save_point="bi_sync_employee")
            except Exception:
                pass  # the work committed before failing; nothing left to undo
            frappe.log_error(title="biometric_integration: employee sync failed", message=emp.name)
            summary["conflicts"].append(f"{emp.name} {emp.employee_name}: failed, see Error Log")
            continue
        if commit and not dry_run:
            frappe.db.commit()  # nosemgrep -- long job: keep each employee's sync even if a later one fails
        if added:
            summary["people"].append(f"{emp.name} {emp.employee_name}")
            for d in added:
                summary["added"][d] = summary["added"].get(d, 0) + 1
    return summary


def reconcile_daily() -> None:
    """Scheduler entry point."""
    reconcile_employee_devices()


def run_sync_job(user: str) -> None:
    """Background job behind the Sync Employees button; tells the user when done."""
    summary = reconcile_employee_devices()
    frappe.publish_realtime("biometric_employee_sync_done", summary, user=user)


def push_device_roster(device_id: str) -> None:
    """Put a registered or re-enabled device's own users back on it, then add
    whoever else belongs there per the sync mode.

    A device that comes back from repair is usually wiped while its users are
    still assigned to it in ERPNext, so the reconcile alone (which only adds
    missing assignments) would leave it empty. Only users assigned to this
    device (or set to Allow In All Devices) are pushed, and only if their
    employee is not inactive.
    """
    device = frappe.db.get_value("Attendance Device", device_id, ["brand"], as_dict=True)
    blob_field = _BRAND_BLOB_FIELD.get(device.brand) if device else None
    if not blob_field:
        return
    assigned = set(
        frappe.get_all(
            "Attendance Device Link",
            filters={"parenttype": "Attendance Device User", "attendance_device": device_id},
            pluck="parent",
        )
    )
    assigned |= set(
        frappe.get_all("Attendance Device User", filters={"allow_in_all_devices": 1}, pluck="name")
    )
    for name in sorted(assigned):
        user = frappe.db.get_value("Attendance Device User", name, ["employee", blob_field], as_dict=True)
        if not user:
            continue
        if user.employee and frappe.db.get_value("Employee", user.employee, "status") in _INACTIVE_STATUSES:
            continue
        add_command(device_id, name, device.brand, "Enroll User" if user.get(blob_field) else "Update User")
    reconcile_employee_devices(device=device_id, commit=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_device_user(employee_name: str) -> Optional[object]:
    doc_name = frappe.db.get_value("Attendance Device User", {"employee": employee_name})
    if not doc_name:
        return None
    return frappe.get_doc("Attendance Device User", doc_name)


def _get_user_devices(user_doc) -> dict:
    """Return {device_id: brand} for all active devices this user should be on."""
    if user_doc.allow_in_all_devices:
        devices = frappe.get_all("Attendance Device", filters={"disabled": 0}, fields=["name", "brand"])
        return {d.name: d.brand for d in devices}
    return {row.attendance_device: row.brand for row in user_doc.get("devices", []) if row.attendance_device}


def _delete_from_all_devices(user_doc) -> None:
    for device_id, brand in _get_user_devices(user_doc).items():
        add_command(device_id, user_doc.name, brand, "Delete User")


def _re_enroll_on_all_devices(user_doc) -> None:
    """Put a reactivated person back on their devices. Leaving deleted them from
    the device, so someone without a stored enrollment needs Update User to
    exist there again; with one, Enroll User restores the fingers as well."""
    for device_id, brand in _get_user_devices(user_doc).items():
        blob_field = _BRAND_BLOB_FIELD.get(brand, "")
        command = "Enroll User" if blob_field and user_doc.get(blob_field) else "Update User"
        add_command(device_id, user_doc.name, brand, command)


def _update_user_info(user_doc) -> None:
    """Queue Update User on all devices (ZKTeco: USERINFO, EBKN: SET_USER_PROFILE)."""
    for device_id, brand in _get_user_devices(user_doc).items():
        add_command(device_id, user_doc.name, brand, "Update User")
