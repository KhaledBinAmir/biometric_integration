# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Command processor: fetches the next pending Attendance Device Command and builds
the brand-specific payload to return to the polling device.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional, Union

import frappe
from frappe.utils import cint, now, now_datetime, get_datetime, add_to_date

# A command stays "Sent" (not re-emitted) for this long after it is handed to a
# device, giving the device time to acknowledge before we re-send. If no ack
# arrives within the window it is re-sent (reliability); once the device reports
# a result the ack handlers move it to Success/Failed.
COMMAND_RESEND_SECONDS = 60


def _claim_next_command(device_sn: str) -> Optional[str]:
    """Atomically claim the next command to emit for a device.

    Picks the oldest command that is Pending, or Sent-but-unacknowledged past the
    resend window, and flips it to Sent (stamping sent_on + bumping attempts) via a
    compare-and-swap so two concurrent device polls can't grab the same command.
    Returns the claimed command name, or None (queue empty or lost the race).
    """
    settings = frappe.get_cached_doc("Attendance Integration Settings")
    max_attempts = cint(settings.maximum_command_attempts) or 3
    cutoff = add_to_date(now_datetime(), seconds=-COMMAND_RESEND_SECONDS)
    claimable = (
        "(status='Pending' OR (status='Sent' AND (sent_on IS NULL OR sent_on < %(cutoff)s)))"
    )
    # Bounded loop: fail any candidate that has hit the attempt cap (the raw-SQL
    # claim below bypasses the doctype's before_save auto-fail), then take the next.
    for _ in range(10):
        rows = frappe.db.sql(
            f"""SELECT name, no_of_attempts FROM `tabAttendance Device Command`
                WHERE attendance_device=%(sn)s AND {claimable}
                ORDER BY creation ASC LIMIT 1""",
            {"sn": device_sn, "cutoff": cutoff},
        )
        if not rows:
            # Queue drained — clear the sticky "pending command" indicator.
            if frappe.db.get_value("Attendance Device", device_sn, "has_pending_command"):
                frappe.db.set_value("Attendance Device", device_sn, "has_pending_command", 0,
                                    update_modified=False)
                frappe.db.commit()
            return None

        name, attempts = rows[0][0], cint(rows[0][1])
        if attempts >= max_attempts:
            frappe.db.set_value("Attendance Device Command", name,
                                {"status": "Failed", "closed_on": now_datetime()},
                                update_modified=False)
            frappe.db.commit()
            continue  # look for the next claimable command

        # Compare-and-swap: only the worker whose UPDATE changes the row wins,
        # so concurrent device polls can't emit the same command twice.
        frappe.db.sql(
            f"""UPDATE `tabAttendance Device Command`
                SET status='Sent', sent_on=%(now)s, no_of_attempts=COALESCE(no_of_attempts,0)+1
                WHERE name=%(name)s AND {claimable}""",
            {"name": name, "now": now_datetime(), "cutoff": cutoff},
        )
        claimed = frappe.db._cursor.rowcount
        frappe.db.commit()
        if claimed:
            return name
        # Lost the race for this row — try the next candidate.
    return None


def _finish(cmd_doc: Any, status: str, response: Optional[str] = None) -> None:
    """Move a command to a terminal state (used by commands that complete on send)."""
    cmd_doc.status = status
    cmd_doc.closed_on = now_datetime()
    if response is not None:
        cmd_doc.device_response = response
    cmd_doc.save(ignore_permissions=True)
    frappe.db.commit()


def process_device_command(device_sn: str) -> Optional[Union[str, dict]]:
    """Return the next command payload for the device, or None if none.

    The command is claimed atomically and marked Sent before its payload is built,
    so it is emitted once and not re-sent until the resend window elapses without an
    acknowledgement. Commands that complete on send (Restart, Re-pull, ZKTeco Set
    Time) set their own terminal status; the rest stay Sent until the device acks.

    ZKTeco commands return a string (ADMS protocol lines).
    EBKN commands return a dict with cmd_code/trans_id/body.
    """
    command_name = _claim_next_command(device_sn)
    if not command_name:
        return None

    cmd_doc = frappe.get_doc("Attendance Device Command", command_name)
    # cmd_doc is now Sent, sent_on stamped, no_of_attempts bumped (by the claim).

    if cmd_doc.command_type == "Restart Device":
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        _finish(cmd_doc, "Success")
        if brand == "EBKN":
            return {"trans_id": cmd_doc.name, "cmd_code": "RESET_FK", "body": ""}
        return f"C:{cmd_doc.name}:REBOOT"  # ZKTeco

    if cmd_doc.command_type == "Unlock Door":
        # Stays Sent — device reports the result via devicecmd (ZKTeco) or
        # send_cmd_result (EBKN), which moves it to Success/Failed.
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand == "EBKN":
            return {"trans_id": cmd_doc.name, "cmd_code": "OPEN_DOOR", "body": json.dumps({"door_no": 1})}
        return f"C:{cmd_doc.name}:CONTROL DEVICE 1"  # ZKTeco: door relay open

    if cmd_doc.command_type == "Sync User List":
        # Stays Sent — result arrives via querydata (ZKTeco) / send_cmd_result (EBKN).
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand == "EBKN":
            return {"trans_id": cmd_doc.name, "cmd_code": "GET_USER_ID_LIST", "body": ""}
        # ZKTeco: classic all-users query (widest firmware support — the newer
        # `DATA QUERY tablename=user,...` form is rejected Return=-1004 by classic
        # firmware). The device uploads USER records, ingested by _handle_operlog_user
        # (/iclock/cdata) or _handle_querydata (/iclock/querydata) depending on firmware.
        return f"C:{cmd_doc.name}:DATA QUERY USERINFO"

    if cmd_doc.command_type == "Set Device Time":
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand == "EBKN":
            time_str = _ebkn_now_for_device(device_sn)
            return {
                "trans_id": cmd_doc.name,
                "cmd_code": "SET_TIME",
                "body": json.dumps({"time": time_str}),
            }
        # ZKTeco devices sync clock via /iclock/rtdata?type=time on their own
        # cadence — there is no out-of-band SET_TIME equivalent. Mark Success
        # so the user gets feedback instead of a stuck command.
        _finish(cmd_doc, "Success", "ZKTeco devices auto-sync via /iclock/rtdata; no command sent.")
        return None

    if cmd_doc.command_type == "Re-pull Attendance":
        # Ask the device to re-upload stored attendance logs for a date range.
        # ZKTeco answers `DATA QUERY ATTLOG` by POSTing the records to
        # /iclock/cdata?table=ATTLOG (ingested by _process_attlog; duplicates are
        # rejected). Done-on-send so it is emitted exactly once.
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand != "ZKTeco":
            _finish(cmd_doc, "Failed", "Re-pull Attendance is only supported for ZKTeco devices.")
            return None
        start_s = (
            get_datetime(cmd_doc.repull_start).strftime("%Y-%m-%d %H:%M:%S")
            if cmd_doc.repull_start else "2000-01-01 00:00:00"
        )
        end_s = (
            get_datetime(cmd_doc.repull_end).strftime("%Y-%m-%d %H:%M:%S")
            if cmd_doc.repull_end else now_datetime().strftime("%Y-%m-%d %H:%M:%S")
        )
        _finish(cmd_doc, "Success", f"Sent: DATA QUERY ATTLOG StartTime={start_s} EndTime={end_s}")
        return f"C:{cmd_doc.name}:DATA QUERY ATTLOG StartTime={start_s}\tEndTime={end_s}"

    if cmd_doc.command_type == "Refresh Device Info":
        # Read-only capability probe. ZKTeco answers a `GET OPTIONS <keys>` by
        # POSTing the requested option values back (to /iclock/cdata?table=options
        # or /iclock/querydata?type=options depending on firmware); both paths land
        # in _store_device_capabilities, which updates fp_version / firmware_version
        # / supported_biometrics on the device. Safe against a live device — it does
        # NOT touch enrolled users or templates. Done-on-send: the option upload is
        # asynchronous and (on most firmware) carries no cmdid to correlate back, so
        # we can't tie the eventual values to this command row.
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand != "ZKTeco":
            _finish(cmd_doc, "Failed", "Refresh Device Info is only supported for ZKTeco devices.")
            return None
        _finish(cmd_doc, "Success",
                "Sent: GET OPTIONS (device reports fp/firmware/bio capabilities asynchronously)")
        # Request exactly the option keys _store_device_capabilities knows how to
        # parse. ~ prefix marks read-only/optional options in the ADMS spec.
        keys = "~ZKFPVersion,~ZKFaceVersion,FirmVer,~DeviceName,MAC,~MaxUserCount,FaceFunOn,FingerFunOn,FvFunOn,PvFunOn"
        return f"C:{cmd_doc.name}:GET OPTIONS {keys}"

    if cmd_doc.command_type == "Pull From Device":
        # Brand-agnostic "capture everything off this device" — pulls each user AND
        # their enrolled biometric templates into ERPNext (the backup + the source
        # for restoring/copying to another device via Enroll User). Each brand reaches
        # the same outcome by its own protocol:
        brand = frappe.db.get_value("Attendance Device", device_sn, "brand")
        if brand == "EBKN":
            # No bulk replay: enumerate the device's users; _process_ebkn_user_id_list
            # then queues a Get Enroll Data per user (per-user pull works on EBKN),
            # which captures each template. Stays Sent → send_cmd_result resolves it.
            return {"trans_id": cmd_doc.name, "cmd_code": "GET_USER_ID_LIST", "body": ""}
        # ZKTeco: a CHECK makes the device re-upload its full user table + every
        # locally-enrolled fingerprint as OPERLOG records (USER / FP lines) via
        # /iclock/cdata — the normal ingest path stores them. The replay only
        # happens if the handshake asks for everything, so request a one-off full
        # resync (stamps -> 0); the next handshake clears the flag and returns to
        # incremental, so this never becomes a permanent re-dump loop.
        frappe.db.set_value("Attendance Device", device_sn, "force_full_resync", 1,
                            update_modified=False)
        frappe.db.commit()
        _finish(cmd_doc, "Success", "Sent: CHECK (device replays users + fingerprints via OPERLOG)")
        return f"C:{cmd_doc.name}:CHECK"

    # Enroll / Delete / Update User, Get Enroll Data — stay Sent until the device
    # acks. The claim already bumped attempts, so just build and return the payload.
    try:
        return _build_payload(cmd_doc)
    except Exception as exc:
        _handle_build_failure(cmd_doc, exc)
        return None


def force_close_stale_commands() -> None:
    """Scheduled daily task: mark old uncompleted commands as Failed."""
    settings = frappe.get_cached_doc("Attendance Integration Settings")
    days = cint(settings.force_close_after_days) or 30
    cutoff = frappe.utils.add_to_date(now_datetime(), days=-days)

    stale = frappe.get_all(
        "Attendance Device Command",
        filters={"status": ["in", ["Pending", "Sent"]], "initiated_on": ["<", cutoff]},
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value(
            "Attendance Device Command", name,
            {"status": "Failed", "closed_on": now_datetime()},
            update_modified=False,
        )
    if stale:
        frappe.db.commit()


# ---------------------------------------------------------------------------
# Build logic
# ---------------------------------------------------------------------------

def _build_payload(cmd_doc: Any) -> Optional[Union[str, dict]]:
    user_doc = frappe.get_doc("Attendance Device User", cmd_doc.attendance_device_user)
    brand = cmd_doc.brand
    if brand == "ZKTeco":
        return _zkteco(cmd_doc, user_doc)
    if brand == "EBKN":
        return _ebkn(cmd_doc, user_doc)
    raise ValueError(f"Unsupported brand: {brand}")


# ZKTeco `Pri` values (device keypad rights). Sent with every USERINFO push so a
# device admin is never silently demoted to a normal user by a routine sync.
_ZK_PRI = {"Normal User": 0, "Enroller": 2, "Admin": 6, "Super Admin": 14}
# EBKN privilege codes for SET_USER_PROFILE.
_EBKN_PRI = {"Normal User": 0, "Enroller": 1, "Admin": 2, "Super Admin": 3}


def _zk_pri(user_doc) -> int:
    """Device privilege for this user, defaulting to a normal (non-admin) user."""
    return _ZK_PRI.get((user_doc.get("privilege") if user_doc else None) or "Normal User", 0)


def _ebkn_pri(user_doc) -> int:
    return _EBKN_PRI.get((user_doc.get("privilege") if user_doc else None) or "Normal User", 0)


def _zk_passwd(user_doc, fallback: str = "") -> str:
    """Keypad password for this user. The field wins; otherwise keep whatever the
    captured enrollment blob had. Sent on every USERINFO push — previously this was
    hardcoded empty, so each sync wiped the user's device password."""
    return str((user_doc.get("device_password") if user_doc else None) or fallback or "")


def _zkteco(cmd_doc: Any, user_doc: Any) -> Optional[str]:
    cmd_id = cmd_doc.name
    pin = user_doc.user_id
    name = user_doc.employee_name or ""

    if cmd_doc.command_type == "Delete User":
        return f"C:{cmd_id}:DATA DELETE USERINFO PIN={pin}"

    if cmd_doc.command_type == "Get Enroll Data":
        # Classic ADMS dialect. USERINFO returns the profile; the fingerprint table
        # is FINGERTMP (the same name the working DATA UPDATE uses) — NOT `FPTMP`,
        # which returns Return=-1004 ("table not supported") on this firmware.
        # FINGERTMP takes a mandatory FID, so we sweep the 10 finger slots; the
        # device answers each enrolled finger by POSTing an
        # `FP PIN=.. FID=.. Size=.. TMP=..` OPERLOG record, ingested by the adapter.
        # (Bulk capture of all users+fingers is the "Pull From Device"/CHECK command
        # plus OPERLOGStamp=0; this per-user query is a targeted supplement.)
        lines = [f"C:{cmd_id}:DATA QUERY USERINFO PIN={pin}"]
        lines += [f"C:{cmd_id}:DATA QUERY FINGERTMP PIN={pin}\tFID={fid}" for fid in range(10)]
        # Newer firmware (fp_version >= 10, e.g. the ZAM70 v13 fleet) stores a
        # LOCALLY-enrolled fingerprint in the unified BIODATA table, which the
        # classic FINGERTMP sweep never surfaces (it returns Return=0 but empty).
        # Also ask for BIODATA using the friendly `Type=<n>\tPIN=<pin>` form — the
        # `tablename=biodata,filter=...` form returns Return=-1004 on this fleet.
        # Classic devices reject this single line (-1004) harmlessly; v13 devices
        # answer with the unified template (ingested by the biodata handlers).
        lines.append(f"C:{cmd_id}:DATA QUERY BIODATA Type=1\tPIN={pin}")
        return "\n".join(lines)

    if cmd_doc.command_type == "Update User":
        return f"C:{cmd_id}:DATA UPDATE USERINFO\tPIN={pin}\tName={name}\tPri={_zk_pri(user_doc)}\tPasswd={_zk_passwd(user_doc)}\tCard=0"

    if cmd_doc.command_type == "Enroll User":
        blob = _load_blob(user_doc.zkteco_enroll_data)
        if not blob:
            raise FileNotFoundError(f"ZKTeco enroll data missing for user {user_doc.name}")

        try:
            enroll = json.loads(blob.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            enroll = None

        if enroll and enroll.get("version") == 2:
            # Current format — full JSON with all biometrics + credentials
            card = enroll.get("card", "0")
            passwd = enroll.get("passwd", "")
            # Version-match gate: a fingerprint template only matches on a device
            # whose FP algorithm version equals the version it was captured on.
            # target_fp = manual override if set, else the auto-detected version.
            dev = frappe.db.get_value(
                "Attendance Device", cmd_doc.attendance_device,
                ["fp_version", "fp_version_override"], as_dict=True,
            ) or {}
            target_fp = cint(dev.get("fp_version_override")) or cint(dev.get("fp_version"))
            _s = frappe.get_cached_doc("Attendance Integration Settings")
            _raw = _s.get("enforce_fp_version_match")
            enforce = 1 if _raw is None else cint(_raw)  # default ON when unset

            lines = [
                f"C:{cmd_id}:DATA UPDATE USERINFO\tPIN={pin}\tName={name}\tPri={_zk_pri(user_doc)}\tPasswd={_zk_passwd(user_doc, passwd)}\tCard={card}",
            ]
            skipped = []
            for bio in enroll.get("biometrics", []):
                btype = bio.get("type", 1)
                if btype == 1:
                    # Fingerprint algorithm-version check. When the target version is
                    # known and the template was captured on a different version, the
                    # device would silently reject the template (Return=-1). If the
                    # gate is enforced, skip pushing it (user is still registered and
                    # can enrol locally); otherwise push anyway but warn.
                    mv = cint(bio.get("majorver", 0))
                    if target_fp and mv and mv != target_fp:
                        if enforce:
                            skipped.append(f"FID={bio.get('no', 0)} (v{mv}≠v{target_fp})")
                            continue
                        frappe.log_error(
                            title="Biometric Enrollment Algorithm Mismatch (pushed anyway)",
                            message=(
                                f"User {user_doc.name} (PIN {pin}): fingerprint template "
                                f"majorver={mv} but target device {cmd_doc.attendance_device} "
                                f"fp_version={target_fp}. Enforcement OFF — pushing; device "
                                f"may reject with Return=-1."
                            ),
                        )
                    # Push in the format the template was CAPTURED in. A unified
                    # (biodata, majorver>=10) template pushed via classic FINGERTMP is
                    # accepted with Return=0 but stored structurally wrong — the device
                    # then rejects every match as "Illegal Fingerprint" (proven live on
                    # the SenseFP M2 swap, 2026-08-07: pushed finger = illegal, the same
                    # finger locally enrolled = fine). Classic-captured templates keep
                    # the classic FINGERTMP push (verified Return=0 on legacy firmware,
                    # which conversely rejects unified biodata with Return=-1).
                    if mv >= 10:
                        lines.append(
                            f"C:{cmd_id}:DATA UPDATE biodata"
                            f"\tpin={pin}"
                            f"\tno={bio.get('no', 0)}"
                            f"\tindex={bio.get('index', 0)}"
                            f"\tvalid={bio.get('valid', 1)}"
                            f"\tduress={bio.get('duress', 0)}"
                            f"\ttype={btype}"
                            f"\tmajorver={mv}"
                            f"\tminorver={bio.get('minorver', 0)}"
                            f"\tformat={bio.get('format', 0)}"
                            f"\ttmp={bio['tmp']}"
                        )
                    else:
                        lines.append(
                            f"C:{cmd_id}:DATA UPDATE FINGERTMP"
                            f"\tPIN={pin}"
                            f"\tFID={bio.get('no', 0)}"
                            f"\tSize={bio.get('size', 0)}"
                            f"\tValid={bio.get('valid', 1)}"
                            f"\tTMP={bio['tmp']}"
                        )
                else:
                    # Face / palm / other modalities exist only on newer firmware,
                    # which uses the unified template (lowercase fields + `format`,
                    # 0=ZK; capitalised names or a `Size` field → Return=-1).
                    lines.append(
                        f"C:{cmd_id}:DATA UPDATE biodata"
                        f"\tpin={pin}"
                        f"\tno={bio['no']}"
                        f"\tindex={bio.get('index', 0)}"
                        f"\tvalid={bio.get('valid', 1)}"
                        f"\tduress={bio.get('duress', 0)}"
                        f"\ttype={btype}"
                        f"\tmajorver={bio.get('majorver', 0)}"
                        f"\tminorver={bio.get('minorver', 0)}"
                        f"\tformat={bio.get('format', 0)}"
                        f"\ttmp={bio['tmp']}"
                    )
            if skipped:
                # Record why fingerprints were withheld so the operator isn't left
                # wondering why a "successful" enroll can't fingerprint-match. The
                # device ack later appends its own result to this note.
                msg = (
                    f"Skipped {len(skipped)} fingerprint(s) on version mismatch "
                    f"(device fp_version={target_fp}): {', '.join(skipped)}. "
                    f"User registered — enrol the finger directly on this device."
                )
                frappe.log_error(title="Biometric Enrollment Version Gate", message=msg)
                try:
                    frappe.db.set_value("Attendance Device Command", cmd_id,
                                        "device_response", msg, update_modified=False)
                    frappe.db.commit()
                except Exception:
                    pass
            return "\n".join(lines)
        elif enroll and "fid" in enroll:
            # Intermediate format — single FP JSON {"fid", "size", "tmp"}
            lines = [
                f"C:{cmd_id}:DATA UPDATE USERINFO\tPIN={pin}\tName={name}\tPri={_zk_pri(user_doc)}\tPasswd={_zk_passwd(user_doc)}\tCard=0",
                f"C:{cmd_id}:DATA UPDATE BIODATA\tPIN={pin}\tFID={enroll['fid']}\tSize={enroll['size']}\tValid=1\tTMP={enroll['tmp']}",
            ]
            return "\n".join(lines)
        else:
            # Legacy format — raw base64 string, assume single fingerprint FID=0
            tmp = blob.decode("utf-8").strip()
            size = len(base64.b64decode(tmp + "=="))
            return "\n".join([
                f"C:{cmd_id}:DATA UPDATE USERINFO\tPIN={pin}\tName={name}\tPri={_zk_pri(user_doc)}\tPasswd={_zk_passwd(user_doc)}\tCard=0",
                f"C:{cmd_id}:DATA UPDATE BIODATA\tPIN={pin}\tFID=0\tSize={size}\tValid=1\tTMP={tmp}",
            ])
    return None


def _ebkn(cmd_doc: Any, user_doc: Any) -> Optional[dict]:
    # EBKN user ids are numeric, zero-padded to 8. Attendance Device User is
    # shared with ZKTeco where alphanumeric PINs occur — fail with a clear
    # message instead of an opaque int() ValueError.
    raw_pin = str(user_doc.user_id or "").strip()
    if not raw_pin.isdigit():
        raise ValueError(
            f"EBKN commands need a numeric user id; user {user_doc.name} has {raw_pin!r}"
        )
    uid = raw_pin.zfill(8)

    if cmd_doc.command_type == "Delete User":
        return {
            "trans_id": cmd_doc.name,
            "cmd_code": "DELETE_USER",
            "body": json.dumps({"user_id": uid}),
        }

    if cmd_doc.command_type == "Get Enroll Data":
        return {
            "trans_id": cmd_doc.name,
            "cmd_code": "GET_USER_INFO",
            "body": json.dumps({"user_id": uid}),
        }

    if cmd_doc.command_type == "Update User":
        # SET_USER_PROFILE: name + privilege only, no biometrics required.
        # Registers the user on the device so they can clock in with their PIN.
        # NOTE (needs hardware verification): SET_USER_PROFILE is not in the
        # BS_FkWeb command table — the documented equivalents are SET_USER_NAME
        # ({user_id, user_name}) and SET_USER_PRIVILEGE ({user_id,
        # user_privilege: "USER"|...}). If a device leaves this Pending forever
        # (never sends send_cmd_result), switch to those.
        return {
            "trans_id": cmd_doc.name,
            "cmd_code": "SET_USER_PROFILE",
            "body": json.dumps({"user_id": uid, "user_name": user_doc.employee_name or "", "privilege": _ebkn_pri(user_doc)}),
        }

    if cmd_doc.command_type == "Enroll User":
        blob = _load_blob(user_doc.ebkn_enroll_data)
        if not blob:
            raise FileNotFoundError(f"EBKN enroll data missing for user {user_doc.name}")
        return {"trans_id": cmd_doc.name, "cmd_code": "SET_USER_INFO", "body": blob}

    return None


def _ebkn_now_for_device(device_sn: str) -> str:
    """Return current time as EBKN's YYYYMMDDhhmmss in the device's configured zone.

    Uses Attendance Device.device_timezone if set, else the site timezone. The
    string is a plain wall-clock value — EBKN's SET_TIME has no UTC marker.
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz_name = (
            frappe.db.get_value("Attendance Device", device_sn, "device_timezone")
            or frappe.utils.get_system_timezone()
        )
        now_local = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now_local = datetime.now()
    return now_local.strftime("%Y%m%d%H%M%S")


def _load_blob(url: str) -> Optional[bytes]:
    if not url:
        return None
    file_name = frappe.db.get_value("File", {"file_url": url}, "name")
    if not file_name:
        return None
    content = frappe.get_doc("File", file_name).get_content()
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _handle_build_failure(cmd_doc: Any, exc: Exception) -> None:
    try:
        frappe.db.rollback()
        cmd_doc.reload()
        cmd_doc.no_of_attempts = (cmd_doc.no_of_attempts or 0) + 1
        line = f"[{now()}] Build Failed: {exc}"
        cmd_doc.device_response = (
            f"{cmd_doc.device_response}\n{line}" if cmd_doc.device_response else line
        )
        settings = frappe.get_cached_doc("Attendance Integration Settings")
        max_att = cint(settings.maximum_command_attempts) or 3
        if cmd_doc.no_of_attempts >= max_att:
            cmd_doc.status = "Failed"
            cmd_doc.closed_on = now_datetime()
        else:
            cmd_doc.status = "Pending"
        cmd_doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.log_error(
            title="Command Build Failed",
            message=frappe.get_traceback(),
            reference_doctype="Attendance Device Command",
            reference_name=cmd_doc.name,
        )
    except Exception as inner:
        frappe.db.rollback()
        frappe.log_error(title="Command Error Handler Failed", message=str(inner))
