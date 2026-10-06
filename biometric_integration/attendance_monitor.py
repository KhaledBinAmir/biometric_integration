# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Attendance Monitor — net-time-on-site computation from raw biometric punches.

The devices stamp an unreliable IN/OUT flag (status 255 on every scan), so we
never trust it. Instead we pair a day's punches by CHRONOLOGICAL ORDER:

    scan 1 = IN  (arrival)
    scan 2 = OUT (break start)
    scan 3 = IN  (break end)
    scan 4 = OUT (departure)

Work time  = sum of each IN->OUT pair (odd->even), i.e. (s2-s1)+(s4-s3).
Break time = the OUT->IN gaps between pairs (even->odd), excluded from work.

Repeat scans within a short window (double-taps / double reads) collapse to the
first punch of the burst. An odd punch count means a missed scan -> the day is
flagged for supervisor correction rather than silently miscomputed.

These are pure helpers (no Frappe dependency) so they are unit-testable and are
reused by both the monitoring API here and the Shift-Type attendance engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def dedup_punches(times: list[datetime], window_seconds: int = 180) -> list[datetime]:
    """Sort ascending and drop any punch within `window_seconds` of the last kept
    one (collapses double-taps / double reads to the first of the burst)."""
    kept: list[datetime] = []
    for t in sorted(times):
        if not kept or (t - kept[-1]).total_seconds() >= window_seconds:
            kept.append(t)
    return kept


def compute_day(times: list[datetime], window_seconds: int = 180,
                expected_punches: Optional[int] = 4, mode: str = "pairs",
                is_today: bool = False) -> dict:
    """Turn one employee-day's raw punches into work/break seconds + segments.

    mode:
      "pairs"      — 4-scan model: work = sum of IN->OUT pairs, break = the
                     gaps between pairs (VGH construction crew).
      "span"       — first-in / last-out: work = last - first, no break
                     tracking (e.g. Vincitool, contract-hours style).

    Returns:
        {
          "scans":   [datetime, ...]            # deduped, ordered
          "work_seconds":  int,
          "break_seconds": int,
          "segments": [{"type": "work"|"break"|"unknown", "start": dt, "end": dt}, ...],
          "complete": bool,
          "flag": None | "missing_punch" | "unexpected_count",
        }
    """
    scans = dedup_punches(times, window_seconds)
    n = len(scans)

    if mode == "span":
        if n == 0:
            return {"scans": [], "work_seconds": 0, "break_seconds": 0,
                    "segments": [], "complete": False, "flag": None}
        work = int((scans[-1] - scans[0]).total_seconds())
        return {
            "scans": scans,
            "work_seconds": work,
            "break_seconds": 0,
            "segments": [{"type": "work", "start": scans[0], "end": scans[-1]}] if n >= 2 else [],
            "complete": n >= 2,
            # A single punch is only "missing" once the day is over. Today it just
            # means the worker is still on site (checked in, not yet out).
            "flag": None if (n >= 2 or is_today) else "missing_punch",
        }

    segments: list[dict] = []
    work = 0
    brk = 0

    # Pair (IN, OUT) = (scans[0],scans[1]), (scans[2],scans[3]), ...
    i = 0
    while i + 1 < n:
        start, end = scans[i], scans[i + 1]
        work += int((end - start).total_seconds())
        segments.append({"type": "work", "start": start, "end": end})
        # break = gap to the next IN, if there is another pair
        if i + 3 < n:
            b_start, b_end = scans[i + 1], scans[i + 2]
            brk += int((b_end - b_start).total_seconds())
            segments.append({"type": "break", "start": b_start, "end": b_end})
        i += 2

    # Odd count: the trailing punch can't be paired — the stretch leading up to
    # it is unclassifiable (was it work or break?). Surface it as "unknown" so
    # the UI shows an honest gray gap instead of clean background.
    if n >= 3 and n % 2 == 1:
        segments.append({"type": "unknown", "start": scans[-2], "end": scans[-1]})

    complete = (n % 2 == 0) and n > 0
    # Flag ONLY a genuinely unpaired punch on a finished day. Breaks are not
    # scanned here, so an even count (IN/OUT, no breaks) is normal — never flagged
    # on count alone (the old "unexpected_count" is dropped). And an odd count for
    # TODAY just means the worker is still clocked in, so it is not flagged either.
    flag = None
    if n % 2 == 1 and not is_today:
        flag = "missing_punch"          # odd punches on a past day -> a scan was missed

    return {
        "scans": scans,
        "work_seconds": work,
        "break_seconds": brk,
        "segments": segments,
        "complete": complete,
        "flag": flag,
    }


def _h(seconds: int) -> float:
    return round(seconds / 3600.0, 2)


# ---------------------------------------------------------------------------
# Whitelisted API (consumed by the desk Vue page now, a mobile view later)
# ---------------------------------------------------------------------------

import frappe
from frappe.utils import cint, get_datetime, getdate

_ALLOWED_ROLES = ("Site Supervisor", "HR User", "HR Manager", "System Manager")


def _guard():
    if not set(_ALLOWED_ROLES) & set(frappe.get_roles()):
        frappe.throw(frappe._("Not permitted"), frappe.PermissionError)


def _permitted_companies() -> list[str] | None:
    """Companies the session user is restricted to via User Permission.

    Returns None when unrestricted (no Company User Permissions), else the list.
    """
    rows = frappe.get_all(
        "User Permission",
        filters={"user": frappe.session.user, "allow": "Company"},
        pluck="for_value",
    )
    return rows or None


def _check_company(company: str) -> None:
    allowed = _permitted_companies()
    if allowed is not None and company not in allowed:
        frappe.throw(frappe._("Not permitted for company {0}").format(company),
                     frappe.PermissionError)


def _check_employee(employee: str) -> None:
    """Corrections may only touch employees of a permitted company."""
    company = frappe.db.get_value("Employee", employee, "company")
    _check_company(company)


@frappe.whitelist()
def get_monitor_companies() -> list[str]:
    """Companies selectable in the monitor: the user's permitted list, or all."""
    _guard()
    allowed = _permitted_companies()
    if allowed is not None:
        return allowed
    return frappe.get_all("Company", pluck="name", order_by="name")


@frappe.whitelist()
def get_monitor_config() -> dict:
    """UI bootstrap: what the current user may do + how the page behaves."""
    _guard()
    return {
        "can_correct": _corrections_enabled(),
        "companies": get_monitor_companies(),
    }


def _corrections_enabled() -> bool:
    return bool(cint(frappe.db.get_single_value(
        "Attendance Integration Settings", "allow_checkin_corrections")))


def _assert_corrections_enabled():
    if not _corrections_enabled():
        frappe.throw(frappe._("Check-in corrections are disabled for this site."),
                     frappe.PermissionError)


def _run_processor(employee: str, day):
    """Invoke the configured attendance-processor Server Script for one
    employee-day, if one is set. Blank → rely on Frappe's native Shift Type
    auto-attendance (or nothing). The script receives a `doc` (Employee Checkin)
    carrying the employee + the day to rebuild."""
    script = frappe.db.get_single_value("Attendance Integration Settings", "attendance_processor")
    if not script:
        return
    stub = frappe.new_doc("Employee Checkin")
    stub.employee = employee
    stub.time = f"{getdate(day)} 12:00:00"
    frappe.get_doc("Server Script", script).execute_doc(stub)


def _release_day_lock(employee: str, day):
    """Cancel + remove the day's Attendance and unlink its check-ins, so an edit
    or delete isn't blocked by 'attendance record is linked to this checkin'.
    The processor rebuilds a fresh Attendance afterwards.

    Messages are muted: HRMS emits an 'Unlinked Attendance record from Employee
    Checkins' msgprint on cancel, which would otherwise pop a modal on every
    correction.
    """
    day = getdate(day)
    prev_mute = frappe.flags.mute_messages
    frappe.flags.mute_messages = True
    try:
        for a in frappe.get_all("Attendance",
                                filters={"employee": employee, "attendance_date": day,
                                         "docstatus": ["!=", 2]},
                                fields=["name", "docstatus"]):
            # preserve HR-set leave/holiday/half-day days — only clear worked ones
            status = frappe.db.get_value("Attendance", a.name, "status")
            if status not in ("Present", "Absent", None, ""):
                continue
            if a.docstatus == 1:
                frappe.get_doc("Attendance", a.name).cancel()
            frappe.delete_doc("Attendance", a.name, force=True, ignore_permissions=True)
        # unlink the day's check-ins
        for c in frappe.get_all("Employee Checkin",
                                filters={"employee": employee,
                                         "time": ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]],
                                         "attendance": ["is", "set"]},
                                pluck="name"):
            frappe.db.set_value("Employee Checkin", c, "attendance", None, update_modified=False)
    finally:
        frappe.flags.mute_messages = prev_mute


@frappe.whitelist()
def get_attendance_monitor(from_date, to_date=None, company="VGH B.V.",
                           window_seconds=180, expected_punches=4,
                           include_absent=0, mode=None):
    """Per employee-per-day net-time-on-site for a company's crew.

    Returns rows: {employee, employee_name, date, scans[iso], work_hours,
    break_hours, complete, flag, checkins:[{name,time}]}. Scoped by company
    (User Permission on Company further limits what the caller can see).

    include_absent: on a single-day query, also return a row (empty scans,
    flag "no_scans") for every active employee without a punch that day, so
    supervisors see who did not show up.
    """
    _guard()
    _check_company(company)
    from_date = getdate(from_date)
    to_date = getdate(to_date or from_date)
    window_seconds = int(window_seconds)
    expected_punches = int(expected_punches) if expected_punches else None
    include_absent = int(include_absent or 0) and from_date == to_date

    emps = frappe.get_all("Employee", filters={"company": company, "status": "Active"},
                          fields=["name", "employee_name", "department", "default_shift"])
    emp_names = {e.name: e.employee_name for e in emps}
    emp_dept = {e.name: (e.department or "") for e in emps}
    emp_default_shift = {e.name: e.default_shift for e in emps}
    if not emp_names:
        return []

    mode_of = _shift_mode_resolver(list(emp_names), emp_default_shift, from_date, to_date)

    rows = _punches({"employee": ["in", list(emp_names)],
                     "time": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]})
    # group by (employee, date)
    buckets: dict = {}
    for r in rows:
        buckets.setdefault((r.employee, r.time.date()), []).append(r)

    out = []
    for (emp, day), items in sorted(buckets.items(), key=lambda x: (emp_names.get(x[0][0], ""), x[0][1])):
        out.append(_row_dict(emp, emp_names.get(emp, emp), emp_dept.get(emp, ""), day, items,
                             mode or mode_of(emp, day), window_seconds, expected_punches))

    if include_absent:
        scanned = {emp for (emp, _day) in buckets}
        leave_map = _leaves_on(list(emp_names), from_date)
        for emp in sorted(set(emp_names) - scanned, key=lambda e: emp_names.get(e, e)):
            leave = leave_map.get(emp)
            out.append({
                "employee": emp,
                "employee_name": emp_names.get(emp, emp),
                "department": emp_dept.get(emp, ""),
                "date": str(from_date),
                "mode": mode or mode_of(emp, from_date),
                "scans": [],
                "checkins": [],
                "superseded": [],
                "work_hours": 0.0,
                "break_hours": 0.0,
                "segments": [],
                "complete": False,
                "flag": "on_leave" if leave else "no_punches",
                "leave_type": leave,
            })

    # Holiday label is the same for the whole company/day — stamp it on every
    # row so the UI can show one banner (only meaningful on a single-day view).
    holiday = _holiday_on(company, from_date) if from_date == to_date else None
    if holiday:
        for r in out:
            r["holiday"] = holiday
    return out


def _row_dict(emp, name, dept, day, items, mode, window_seconds=180, expected_punches=4):
    """Build one employee-day monitor row from `items`, the day's punches as
    _punches returns them.

    `checkins` are the punches that count, a MANUAL one from an Attendance
    Request included (it carries `request`; the page shows it greyed and changes
    it only through that request). A punch an approved shift correction replaced
    is listed under `superseded` (greyed on the page) and left out of the totals,
    as the roster's pay leaves it out."""
    items = sorted(items, key=lambda p: p.time)
    counted = [p for p in items if not p.get("superseded_by")]
    is_today = getdate(day) == getdate()
    res = compute_day([p.time for p in counted], window_seconds, expected_punches, mode=mode,
                      is_today=is_today)
    return {
        "employee": emp,
        "employee_name": name or emp,
        "department": dept or "",
        "date": str(day),
        "mode": mode,
        "scans": [t.isoformat() for t in res["scans"]],
        "checkins": [{"name": p.name, "time": p.time.isoformat(), "request": p.request}
                     for p in counted],
        "superseded": [{"name": p.name, "time": p.time.isoformat(), "superseded_by": p.superseded_by}
                       for p in items if p.get("superseded_by")],
        "work_hours": _h(res["work_seconds"]),
        "break_hours": _h(res["break_seconds"]),
        "segments": [{"type": s["type"], "start": s["start"].isoformat(), "end": s["end"].isoformat()}
                     for s in res["segments"]],
        "complete": res["complete"],
        "flag": res["flag"],
    }


MANUAL_PREFIX = "MANUAL-"


def _punches(filters: dict) -> list:
    """Employee Checkins matching `filters`, oldest first, `time` as a datetime,
    with what Attendance Requests did to them:

    - `superseded_by`: an approved shift correction (pidyen_roster) replaced the
      punch. It stays in Employee Checkin as the device recorded it, but no
      longer counts. The column comes with pidyen_roster; without it nothing is
      superseded.
    - `request`: the Attendance Request a MANUAL-<request> punch came from.
    """
    fields = ["name", "employee", "time", "device_id"]
    if frappe.db.has_column("Employee Checkin", "superseded_by"):
        fields.append("superseded_by")
    rows = frappe.get_all("Employee Checkin", filters=filters, fields=fields, order_by="time asc")
    for r in rows:
        r.time = get_datetime(r.time)
        r.superseded_by = r.get("superseded_by")
        r.request = _request_of(r.device_id)
    return rows


def _request_of(device_id) -> Optional[str]:
    device_id = device_id or ""
    return device_id[len(MANUAL_PREFIX):] if device_id.startswith(MANUAL_PREFIX) else None


def _assert_editable(name: str) -> None:
    """The monitor does not move or delete a punch an Attendance Request owns:
    one a shift correction replaced (changing it changes nothing) or one a request
    added (its cancel removes it again)."""
    fields = ["device_id"]
    if frappe.db.has_column("Employee Checkin", "superseded_by"):
        fields.append("superseded_by")
    row = frappe.db.get_value("Employee Checkin", name, fields, as_dict=True) or {}
    if row.get("superseded_by"):
        frappe.throw(
            frappe._("This punch was replaced by the correction in Attendance Request {0} and no longer "
                     "counts. To change the shift's times, cancel or amend that request.").format(
                row.superseded_by),
            title=frappe._("Punch replaced by a correction"),
        )
    request = _request_of(row.get("device_id"))
    if request:
        frappe.throw(
            frappe._("This punch comes from Attendance Request {0}. To change it, cancel or amend "
                     "that request.").format(request),
            title=frappe._("Punch from a request"),
        )


def _employee_day_row(employee: str, day) -> dict:
    """Recompute a single employee-day row (same shape as get_attendance_monitor
    rows) so a correction can patch that row in place — no full reload. Uses the
    monitor's default window/expected-punches so the numbers match the grid."""
    day = getdate(day)
    emp = frappe.db.get_value("Employee", employee,
                              ["employee_name", "department", "company", "default_shift"], as_dict=True) or {}
    items = _punches({"employee": employee, "time": ["between", [f"{day} 00:00:00", f"{day} 23:59:59"]]})
    mode = _shift_mode_resolver([employee], {employee: emp.get("default_shift")}, day, day)(employee, day)
    holiday = _holiday_on(emp.get("company"), day)

    if items:
        row = _row_dict(employee, emp.get("employee_name"), emp.get("department"), day, items, mode)
    else:
        leave = _leaves_on([employee], day).get(employee)
        row = {
            "employee": employee, "employee_name": emp.get("employee_name") or employee,
            "department": emp.get("department") or "", "date": str(day), "mode": mode,
            "scans": [], "checkins": [], "superseded": [], "work_hours": 0.0, "break_hours": 0.0, "segments": [],
            "complete": False, "flag": "on_leave" if leave else "no_punches", "leave_type": leave,
        }
    if holiday:
        row["holiday"] = holiday
    return row


def _shift_mode_resolver(employees: list[str], emp_default_shift: dict, from_date, to_date):
    """Build a fast `mode_of(employee, day) -> "pairs"|"span"` closure.

    An employee's attendance mode follows their Shift Type's native
    "Working Hours Calculation Based On" field:
      "First Check-in and Last Check-out"    → span  (whole first->last, no break)
      "Every Valid Check-in and Check-out"   → pairs (sum of IN->OUT pairs)
    The shift for a day is the active Shift Assignment covering it, else the
    employee's default shift. No shift / blank field → pairs (safe default).
    """
    shift_mode: dict = {}
    for st in frappe.get_all("Shift Type", fields=["name", "working_hours_calculation_based_on"]):
        shift_mode[st.name] = ("span" if st.working_hours_calculation_based_on
                               == "First Check-in and Last Check-out" else "pairs")

    assigns = frappe.get_all(
        "Shift Assignment",
        filters={"employee": ["in", employees], "docstatus": 1, "status": "Active",
                 "start_date": ["<=", to_date]},
        fields=["employee", "shift_type", "start_date", "end_date"],
        order_by="start_date asc",
    ) if employees else []
    by_emp: dict = {}
    for a in assigns:
        by_emp.setdefault(a.employee, []).append(a)

    def mode_of(emp, day):
        shift = None
        for a in by_emp.get(emp, []):
            if a.start_date <= day and (not a.end_date or a.end_date >= day):
                shift = a.shift_type  # last match wins → most recent covering assignment
        shift = shift or emp_default_shift.get(emp)
        return shift_mode.get(shift, "pairs")

    return mode_of


def _leaves_on(employees: list[str], day) -> dict:
    """{employee: leave_type} for approved Leave Applications covering `day`."""
    if not employees:
        return {}
    rows = frappe.get_all(
        "Leave Application",
        filters={"employee": ["in", employees], "status": "Approved",
                 "from_date": ["<=", day], "to_date": [">=", day]},
        fields=["employee", "leave_type"],
    )
    return {r.employee: r.leave_type for r in rows}


def _holiday_on(company: str, day):
    """Holiday description if `day` is a holiday in the company's default list."""
    hl = frappe.db.get_value("Company", company, "default_holiday_list")
    if not hl:
        return None
    return frappe.db.get_value("Holiday", {"parent": hl, "holiday_date": day}, "description")


@frappe.whitelist()
def add_checkin(employee, time, device_id=None):
    """Supervisor quick-correction: add a missing punch.

    The insert fires Employee Checkin's After-Insert events (incl. a processor
    bound there), so no extra processor call is needed here.
    """
    _guard()
    _assert_corrections_enabled()
    _check_employee(employee)
    doc = frappe.new_doc("Employee Checkin")
    doc.employee = employee
    doc.time = get_datetime(time)
    if device_id:
        doc.device_id = device_id
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return _employee_day_row(employee, getdate(doc.time))


@frappe.whitelist()
def update_checkin(name, time):
    """Supervisor quick-correction: move a punch to the correct time.

    Uses the doc API (not db.set_value) so validation + doc_events fire. The
    day's Attendance is released first (so the edit isn't blocked by a linked
    submitted Attendance), then the processor rebuilds it from the new punches.
    """
    _guard()
    _assert_corrections_enabled()
    row = frappe.db.get_value("Employee Checkin", name, ["employee", "time"], as_dict=True)
    _check_employee(row.employee)
    _assert_editable(name)
    employee = row.employee
    old_day = getdate(row.time)
    new_dt = get_datetime(time)
    new_day = getdate(new_dt)

    # Release the day's Attendance + unlink its check-ins FIRST, then load the
    # checkin fresh so its `attendance` link is already cleared (a stale link to
    # a just-deleted Attendance would fail save's link validation).
    _release_day_lock(employee, old_day)
    if new_day != old_day:
        _release_day_lock(employee, new_day)

    doc = frappe.get_doc("Employee Checkin", name)
    doc.time = new_dt
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    _run_processor(employee, new_day)
    if new_day != old_day:
        _run_processor(employee, old_day)
    frappe.db.commit()
    return _employee_day_row(employee, new_day)


@frappe.whitelist()
def delete_checkin(name):
    """Supervisor quick-correction: remove a stray/duplicate punch."""
    _guard()
    _assert_corrections_enabled()
    doc = frappe.get_doc("Employee Checkin", name)
    _check_employee(doc.employee)
    _assert_editable(name)
    employee = doc.employee
    day = getdate(doc.time)

    _release_day_lock(employee, day)
    frappe.delete_doc("Employee Checkin", name, ignore_permissions=True)
    frappe.db.commit()

    _run_processor(employee, day)
    frappe.db.commit()
    return _employee_day_row(employee, day)
