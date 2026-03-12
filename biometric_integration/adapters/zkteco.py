# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
ZKTeco ADMS (Attendance Device Management Service) adapter.

Protocol flow:
  GET  /iclock/cdata?SN=<sn>                  → handshake / configuration response
  POST /iclock/cdata?SN=<sn>&table=ATTLOG     → batch attendance logs (positional TSV)
  POST /iclock/cdata?SN=<sn>&table=rtlog      → real-time attendance event (kv TSV)
  POST /iclock/cdata?SN=<sn>&table=OPERLOG    → user / fingerprint data upload
  POST /iclock/cdata?SN=<sn>&table=options    → device uploads its options (ack only)
  POST /iclock/cdata?SN=<sn>&table=rtstate    → door/sensor status (ack only)
  POST /iclock/registry?SN=<sn>               → device registers capabilities
  POST /iclock/push?SN=<sn>                   → device requests config after registration
  GET  /iclock/rtdata?SN=<sn>&type=time       → device requests server time
  GET  /iclock/getrequest?SN=<sn>             → device polls for pending commands
  POST /iclock/devicecmd?SN=<sn>              → device reports command results
  GET  /iclock/ping?SN=<sn>                   → keepalive heartbeat
  POST /iclock/exchange?SN=<sn>&type=...      → encryption key exchange (ack only)
  GET  /iclock/querydata?SN=<sn>              → device uploads query response (ack only)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import frappe
from werkzeug.wrappers import Response

from biometric_integration.adapters.base import AbstractDeviceAdapter
from biometric_integration.services.checkin import create_employee_checkin
from biometric_integration.services.command_processor import process_device_command
from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
    get_or_create_user_by_pin,
    save_enrollment_data,
    update_zkteco_enrollment,
)
from biometric_integration.biometric_integration.doctype.attendance_device_log.attendance_device_log import maybe_log
from biometric_integration.utils.device_cache import (
    is_registered_device,
    touch_device,
    get_last_sync_id,
    set_last_sync_id,
)


class ZKTecoAdapter(AbstractDeviceAdapter):

    def dispatch(self) -> Response:
        path = self.path  # e.g. "/iclock/cdata"
        args = self.request.args

        if "/iclock/cdata" in path:
            return self._handle_cdata(args)
        if "/iclock/getrequest" in path:
            return self._handle_getrequest(args)
        if "/iclock/devicecmd" in path:
            return self._handle_devicecmd(args)
        if "/iclock/registry" in path:
            return self._handle_registry(args)
        if "/iclock/push" in path:
            return self._handle_push(args)
        if "/iclock/rtdata" in path:
            return self._handle_rtdata(args)
        if "/iclock/querydata" in path:
            return self._handle_querydata(args)
        # ping, exchange, edata, file — acknowledge only
        return self.text("OK")

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _handle_cdata(self, args) -> Response:
        if self.method == "GET":
            return self._handshake(args)
        return self._upload(args)

    def _handshake(self, args) -> Response:
        sn = args.get("SN") or args.get("sn")
        if not sn:
            return self.text("ERROR: SN is required.", 400)

        if not is_registered_device(sn):
            maybe_log(sn, "Error", "IN",
                      f"Handshake from unregistered device SN={sn} — rejected",
                      raw_data=self.raw_dump("ERROR: Device not registered."),
                      force=True)
            return self.text("ERROR: Device not registered.")

        touch_device(sn)

        last_sync_id = get_last_sync_id(sn)
        settings = frappe.get_cached_doc("Attendance Integration Settings")

        poll_delay = int(settings.device_poll_delay or 10)
        error_delay = int(settings.device_error_delay or 30)
        trans_times = settings.trans_times or "00:00;14:05"
        trans_interval = int(settings.trans_interval or 1)
        body = (
            f"GET OPTION FROM: {sn}\n"
            f"ATTLOGStamp={last_sync_id}\n"
            "OPERLOGStamp=9999\n"
            "ATTPHOTOStamp=None\n"
            f"ErrorDelay={error_delay}\n"
            f"Delay={poll_delay}\n"
            f"TransTimes={trans_times}\n"
            f"TransInterval={trans_interval}\n"
            "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\n"
            + (f"TimeZone={_get_device_tz_hours(sn)}\n" if settings.push_timezone_to_device else "")
            + "Realtime=1\n"
            "Encrypt=None\n"
        )
        maybe_log(sn, "Handshake", "IN", f"Handshake SN={sn}",
                  raw_data=self.raw_dump(body))
        return self.text(body)

    # ------------------------------------------------------------------
    # Device registration  (POST /iclock/registry)
    # ------------------------------------------------------------------

    def _handle_registry(self, args) -> Response:
        """Device sends capabilities on first connection. Respond with RegistryCode=0."""
        sn = args.get("SN") or args.get("sn")
        if sn and is_registered_device(sn):
            touch_device(sn)
            body_str = self.raw_body.decode("utf-8", errors="ignore")
            _store_registry_capabilities(sn, body_str)
        maybe_log(sn or "unknown", "Handshake", "IN", f"Registry SN={sn}",
                  raw_data=self.raw_dump("RegistryCode=0"))
        return self.text("RegistryCode=0")

    # ------------------------------------------------------------------
    # Config download  (POST /iclock/push)
    # ------------------------------------------------------------------

    def _handle_push(self, args) -> Response:
        """Device downloads full config after registration. Return same params as handshake."""
        sn = args.get("SN") or args.get("sn")
        last_sync_id = get_last_sync_id(sn) if sn else 0
        settings = frappe.get_cached_doc("Attendance Integration Settings")
        poll_delay = int(settings.device_poll_delay or 10)
        error_delay = int(settings.device_error_delay or 30)
        trans_times = settings.trans_times or "00:00;14:05"
        trans_interval = int(settings.trans_interval or 1)
        body = (
            f"ATTLOGStamp={last_sync_id}\n"
            "OPERLOGStamp=9999\n"
            "ATTPHOTOStamp=None\n"
            f"ErrorDelay={error_delay}\n"
            f"Delay={poll_delay}\n"
            f"TransTimes={trans_times}\n"
            f"TransInterval={trans_interval}\n"
            "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\n"
            + (f"TimeZone={_get_device_tz_hours(sn)}\n" if settings.push_timezone_to_device else "")
            + "Realtime=1\n"
            "Encrypt=None\n"
        )
        return self.text(body)

    # ------------------------------------------------------------------
    # Server time  (GET /iclock/rtdata?type=time)
    # ------------------------------------------------------------------

    def _handle_rtdata(self, args) -> Response:
        """Device requests server time for clock sync.

        DateTime is UTC encoded with ZKTeco's custom formula (Appendix 5):
          tt = ((year-2000)*12*31 + (mon-1)*31 + day-1) * 86400 + (hour*60+min)*60 + sec

        The device primarily uses the HTTP Date: response header (always UTC per RFC 7231)
        combined with its configured TimeZone= setting to derive local time.
        ServerTZ is the server's local timezone offset in ±HHMM format (e.g. +0100).
        """
        rt_type = args.get("type", "")
        if rt_type == "time":
            now_utc = datetime.now(timezone.utc)
            dt = _zkteco_encode_time(now_utc)
            tz_offset = _get_frappe_tz_offset()
            return self.text(f"DateTime={dt},ServerTZ={tz_offset}")
        return self.text("OK")

    # ------------------------------------------------------------------
    # Data upload  (POST /iclock/cdata)
    # ------------------------------------------------------------------

    def _upload(self, args) -> Response:
        sn = args.get("SN") or args.get("sn")
        table = args.get("table", "")
        body_str = self.raw_body.decode("utf-8", errors="ignore")

        if not is_registered_device(sn):
            maybe_log(sn or "unknown", "Error", "IN",
                      f"Data from unregistered device SN={sn} (table={table}) — ignored",
                      raw_data=self.raw_dump(),
                      force=True)
            return self.text("ERROR: Device not registered.")

        if table == "ATTLOG":
            return self._process_attlog(sn, body_str)
        if table == "rtlog":
            return self._process_rtlog(sn, body_str)
        if table == "OPERLOG":
            return self._process_operlog(sn, body_str)
        # options, rtstate, tabledata, etc. — acknowledge
        return self.text("OK")

    def _process_attlog(self, sn: str, body: str) -> Response:
        """Batch attendance log upload. Format: PIN\tTIME\tSTATUS\tVERIFY\t...\tID"""
        lines = body.strip().split("\n")
        processed = 0
        latest_id = 0

        for line in lines:
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            try:
                pin = parts[0]
                time_str = parts[1]
                # parts[2] = STATUS: 0=In, 1=Out, 2=BreakOut, 3=BreakIn, 4=OT In, 5=OT Out
                log_type = "OUT" if len(parts) > 2 and parts[2].strip() == "1" else "IN"
                biometric_method = _verify_code_to_method(parts[3].strip() if len(parts) > 3 else "")
                log_id = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0

                ts = _localize_device_timestamp(
                    datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S"), sn
                )
                create_employee_checkin(
                    device_pin=pin,
                    timestamp=ts,
                    device_id=sn,
                    log_type=log_type,
                    biometric_method=biometric_method,
                )
                maybe_log(sn, "Attendance", "IN", f"PIN={pin} {log_type} time={time_str}", user_pin=pin, raw_data=line)
                processed += 1
                if log_id > latest_id:
                    latest_id = log_id
            except Exception as exc:
                frappe.log_error(
                    title="ZKTeco ATTLOG Parse Error",
                    message=f"Line: {line!r}\n{exc}",
                )

        if latest_id > 0 and sn:
            set_last_sync_id(sn, latest_id)
            frappe.db.commit()

        return self.text(f"OK: {processed}")

    def _process_rtlog(self, sn: str, body: str) -> Response:
        """Real-time attendance event. Format: time=X\tpin=Y\tcardno=Z\tevent=N\t..."""
        data = _parse_kv_tsv(body.strip())
        pin = data.get("pin") or data.get("PIN")
        time_str = data.get("time") or data.get("Time")
        if not pin or not time_str:
            return self.text("OK")
        try:
            inout = data.get("inoutstatus") or data.get("InOutStatus", "0")
            log_type = "OUT" if str(inout).strip() == "1" else "IN"
            biometric_method = _verify_code_to_method(data.get("verifytype") or data.get("Verifytype", ""))
            ts = _localize_device_timestamp(
                datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S"), sn
            )
            create_employee_checkin(
                device_pin=pin, timestamp=ts, device_id=sn,
                log_type=log_type, biometric_method=biometric_method,
            )
            maybe_log(sn, "Attendance", "IN", f"RT PIN={pin} {log_type} time={time_str}", user_pin=pin, raw_data=body)
        except Exception as exc:
            frappe.log_error(title="ZKTeco rtlog Parse Error", message=f"Body: {body!r}\n{exc}")
        return self.text("OK")

    def _process_operlog(self, sn: str, body: str) -> Response:
        """Process OPERLOG upload — may contain USER, FP, and ENROLL_USER records.

        Each line is a separate record. We process all of them in a single pass
        rather than returning after the first matching type.
        """
        users_processed = 0
        bios_processed = 0

        for line in body.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                if line.startswith("USER") or line.startswith("ENROLL_USER"):
                    users_processed += _handle_operlog_user(sn, line)
                elif line.startswith("FP "):
                    # Older firmware fingerprint format
                    bios_processed += _handle_operlog_fp(sn, line)
                elif line.lower().startswith("face "):
                    # Older firmware face format
                    bios_processed += _handle_operlog_face(sn, line)
                elif line.lower().startswith("biodata "):
                    # Newer unified biometric format (all types: FP, face, palm, etc.)
                    bios_processed += _handle_operlog_biodata(sn, line)
            except Exception as exc:
                frappe.log_error(
                    title="ZKTeco OPERLOG Line Error",
                    message=f"SN={sn} Line: {line!r}\n{exc}",
                )

        return self.text(f"OK: users={users_processed} bios={bios_processed}")

    # ------------------------------------------------------------------
    # Query data response  (POST /iclock/querydata)
    # ------------------------------------------------------------------

    def _handle_querydata(self, args) -> Response:
        """Device uploads results of a DATA QUERY command.

        For Sync User List (tablename=user), parse the user list and queue
        Get Enroll Data for any PINs not already known to Frappe.
        All other table types are acknowledged and ignored.
        """
        table = args.get("tablename", "")
        cmd_id = args.get("cmdid")
        count_str = args.get("count", "0")

        if table != "user":
            return self.text(f"{table}={count_str}")

        sn = args.get("SN") or args.get("sn")
        body_str = self.raw_body.decode("utf-8", errors="ignore")
        count = 0

        for line in body_str.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Line format: user uid=1 cardno= pin=1 password= group=1 ...
            kv = _parse_kv(line)
            pin = kv.get("pin") or kv.get("PIN")
            if not pin:
                continue
            try:
                _ensure_user_synced_from_query(pin, sn, kv)
                count += 1
            except Exception as exc:
                frappe.log_error(
                    title="ZKTeco querydata User Sync Error",
                    message=f"SN={sn} PIN={pin}: {exc}",
                )

        if cmd_id:
            try:
                frappe.db.set_value("Attendance Device Command", cmd_id, {
                    "status": "Success",
                    "closed_on": frappe.utils.now_datetime(),
                    "device_response": f"Received {count} users",
                })
                frappe.db.commit()
            except Exception:
                pass

        maybe_log(sn or "unknown", "Sync", "IN",
                  f"Sync User List: {count} users received from SN={sn}")
        return self.text(f"user={count}")

    # ------------------------------------------------------------------
    # Command polling  (GET /iclock/getrequest)
    # ------------------------------------------------------------------

    def _handle_getrequest(self, args) -> Response:
        sn = args.get("SN") or args.get("sn")
        if not sn:
            return self.text("ERROR: Missing SN", 400)
        command_str = process_device_command(sn)
        if command_str:
            maybe_log(sn, "Command", "OUT", f"Sending command to {sn}", raw_data=command_str)
        return self.text(command_str or "OK")

    # ------------------------------------------------------------------
    # Command result  (POST /iclock/devicecmd)
    # ------------------------------------------------------------------

    def _handle_devicecmd(self, args) -> Response:
        body_str = self.raw_body.decode("utf-8", errors="ignore")
        for line in body_str.strip().split("\n"):
            from urllib.parse import parse_qs
            params = parse_qs(line)
            cmd_id = (params.get("ID") or [None])[0]
            return_code = (params.get("Return") or [None])[0]

            if not cmd_id:
                cmd_id = args.get("ID")
                return_code = args.get("Return")

            if cmd_id:
                try:
                    cmd_doc = frappe.get_doc("Attendance Device Command", cmd_id)
                    cmd_doc.device_response = (
                        f"{cmd_doc.device_response or ''}\n{line}".strip()
                    )
                    cmd_doc.status = "Success" if return_code == "0" else "Failed"
                    cmd_doc.closed_on = frappe.utils.now_datetime()
                    cmd_doc.save(ignore_permissions=True)
                    frappe.db.commit()
                except Exception as exc:
                    frappe.log_error(
                        title="ZKTeco devicecmd Error",
                        message=f"CmdID={cmd_id}: {exc}",
                    )
        return self.text("OK")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _handle_operlog_user(sn: str, line: str) -> int:
    """Handle a USER or ENROLL_USER line from OPERLOG.

    Creates or updates the Attendance Device User, links employee if possible,
    and adds this device to the user's device list.
    Returns 1 on success, 0 on skip.
    """
    data = _parse_kv(line)
    pin = data.get("PIN")
    if not pin:
        return 0

    name = data.get("Name") or data.get("name")
    user_doc = get_or_create_user_by_pin(pin, name)

    # Ensure this device is in the user's device list
    device_in_list = any(row.attendance_device == sn for row in user_doc.get("devices", []))
    if not device_in_list:
        user_doc.append("devices", {"attendance_device": sn, "brand": "ZKTeco", "enroll_data_source": 0})

    # Link employee if not yet linked
    if not user_doc.employee:
        emp = get_erp_employee_id(pin)
        if emp:
            user_doc.employee = emp

    user_doc.save(ignore_permissions=True)
    frappe.db.commit()

    # Persist card and password into enrollment JSON
    card = data.get("Card") or data.get("CardNo") or "0"
    passwd = data.get("Passwd") or data.get("Password") or ""
    update_zkteco_enrollment(user_doc, sn, card=card, passwd=passwd)

    maybe_log(sn, "Enrollment", "IN", f"USER PIN={pin} Name={name}", user_pin=pin)
    return 1


def _handle_operlog_fp(sn: str, line: str) -> int:
    """Handle an FP (fingerprint) line from OPERLOG — older firmware format.

    FP PIN=X FID=Y Size=N Valid=V TMP=base64
    Biometric type 1 = Fingerprint, no = FID (0-9 for ten fingers).
    """
    m = re.search(r"FP\s+PIN=(\S+)\s+FID=(\d+)\s+Size=(\d+)\s+Valid=(\d+)\s+TMP=(\S+)", line)
    if not m:
        return 0
    pin, fid, size, valid, tmp = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
    user_doc = get_or_create_user_by_pin(pin)
    update_zkteco_enrollment(user_doc, sn, biometric={
        "type": 1, "no": fid, "index": 0,
        "size": size, "valid": valid, "duress": 0,
        "majorver": 0, "minorver": 0, "tmp": tmp,
    })
    maybe_log(sn, "Enrollment", "IN", f"FP PIN={pin} FID={fid}", user_pin=pin)
    return 1


def _handle_operlog_face(sn: str, line: str) -> int:
    """Handle a FACE line from OPERLOG — older firmware format.

    FACE PIN=X FID=Y Size=N Valid=V TMP=base64
    Biometric type 9 = Visible light face.
    """
    m = re.search(r"FACE\s+PIN=(\S+)\s+FID=(\d+)\s+Size=(\d+)\s+Valid=(\d+)\s+TMP=(\S+)", line, re.IGNORECASE)
    if not m:
        return 0
    pin, fid, size, valid, tmp = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
    user_doc = get_or_create_user_by_pin(pin)
    update_zkteco_enrollment(user_doc, sn, biometric={
        "type": 9, "no": fid, "index": 0,
        "size": size, "valid": valid, "duress": 0,
        "majorver": 0, "minorver": 0, "tmp": tmp,
    })
    maybe_log(sn, "Enrollment", "IN", f"FACE PIN={pin} FID={fid}", user_pin=pin)
    return 1


def _handle_operlog_biodata(sn: str, line: str) -> int:
    """Handle a biodata line from OPERLOG — newer unified firmware format.

    biodata pin=X no=Y index=I valid=V duress=D type=T majorver=M minorver=m size=S TMP=base64
    Covers all biometric types: 1=FP, 2=NIR face, 8=Palm vein, 9=Visible face, etc.
    """
    m = re.search(
        r"biodata\s+pin=(\S+)\s+no=(\d+)\s+index=(\d+)\s+valid=(\d+)\s+duress=(\d+)"
        r"\s+type=(\d+)(?:\s+majorver=(\d+))?(?:\s+minorver=(\d+))?(?:\s+size=(\d+))?\s+TMP=(\S+)",
        line, re.IGNORECASE,
    )
    if not m:
        return 0
    pin = m.group(1)
    user_doc = get_or_create_user_by_pin(pin)
    update_zkteco_enrollment(user_doc, sn, biometric={
        "type": int(m.group(6)),
        "no": int(m.group(2)),
        "index": int(m.group(3)),
        "size": int(m.group(9) or 0),
        "valid": int(m.group(4)),
        "duress": int(m.group(5)),
        "majorver": int(m.group(7) or 0),
        "minorver": int(m.group(8) or 0),
        "tmp": m.group(10),
    })
    bio_type = m.group(6)
    maybe_log(sn, "Enrollment", "IN", f"biodata PIN={pin} type={bio_type} no={m.group(2)}", user_pin=pin)
    return 1


_VERIFY_MAP = {
    1: "Fingerprint",
    3: "Fingerprint",   # some firmware uses 3
    4: "Face",
    6: "Password",
    15: "Card",
    20: "Face",         # FaceID on newer devices
}


def _verify_code_to_method(code_str: str) -> str | None:
    """Map ZKTeco VERIFY field to a human-readable biometric method."""
    try:
        code = int(code_str)
    except (TypeError, ValueError):
        return None
    return _VERIFY_MAP.get(code, "Other" if code else None)


def _parse_kv(line: str) -> dict:
    """Parse ZKTeco space-separated KEY=VALUE line (e.g. 'USER PIN=001 Name=John ...')."""
    return dict(re.findall(r"(\w+)=(\S+)", line))


def _zkteco_encode_time(dt: datetime) -> int:
    """Encode a datetime to ZKTeco's custom seconds format (Appendix 5).

    Formula: tt = ((year-2000)*12*31 + (mon-1)*31 + day-1) * 86400
                  + (hour*60 + min)*60 + sec
    Input must be UTC (the spec says DateTime is GMT).
    """
    y, mo, d, h, mi, s = dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second
    return ((y - 2000) * 12 * 31 + (mo - 1) * 31 + d - 1) * 86400 + (h * 60 + mi) * 60 + s


def _get_frappe_tz_offset() -> str:
    """Return the Frappe system timezone as ±HHMM (e.g. '+0100').

    Used for ServerTZ in the rtdata response.
    Falls back to '+0000' on any error.
    """
    try:
        from zoneinfo import ZoneInfo
        tz_name = frappe.utils.get_system_timezone()
        now_local = datetime.now(ZoneInfo(tz_name))
        return now_local.strftime("%z")  # e.g. "+0100"
    except Exception:
        return "+0000"


def _get_frappe_tz_hours() -> str:
    """Return the Frappe system timezone UTC offset for ZKTeco's TimeZone= parameter.

    Returns an integer string for whole-hour zones (e.g. "1" for UTC+1, "8" for UTC+8),
    or a decimal string for fractional zones (e.g. "5.5" for India UTC+5:30,
    "5.75" for Nepal UTC+5:45).  The spec only shows integer examples but ZKTeco's
    own software sends decimals for India — older firmware that can't parse a float
    will truncate to the integer part, which is no worse than sending the integer.

    DST-aware: uses datetime.now(ZoneInfo(...)) so the value updates automatically
    at DST transition boundaries (next device handshake picks up the new offset).
    Falls back to "0" on any error.
    """
    try:
        from zoneinfo import ZoneInfo
        tz_name = frappe.utils.get_system_timezone()
        now_local = datetime.now(ZoneInfo(tz_name))
        offset_hours = now_local.utcoffset().total_seconds() / 3600
        # Emit integer string if whole hour, decimal string otherwise
        if offset_hours == int(offset_hours):
            return str(int(offset_hours))
        return str(offset_hours)
    except Exception:
        return "0"


def _parse_kv_tsv(line: str) -> dict:
    """Parse ZKTeco tab-separated key=value line (e.g. 'time=2024-01-01 09:00:00\tpin=1\t...')."""
    result = {}
    for part in line.split("\t"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def _ensure_user_synced_from_query(pin: str, sn: str | None, kv: dict) -> None:
    """Create or update an Attendance Device User from a DATA QUERY user line.

    - If already linked to this device → skip (nothing to do).
    - Otherwise: append device link, save, then queue Get Enroll Data.

    NOTE: We do NOT call _ensure_device_user_synced() here because that function
    returns early if the device is already in the user's device list — which would
    be the case immediately after we appended it, causing Get Enroll Data to be skipped.
    """
    name = kv.get("name") or kv.get("Name")
    user_doc = get_or_create_user_by_pin(pin, name)

    # Already linked — nothing to do
    if any(row.attendance_device == sn for row in user_doc.get("devices", [])):
        return

    brand = frappe.db.get_value("Attendance Device", sn, "brand") if sn else None
    if brand:
        user_doc.append("devices", {"attendance_device": sn, "brand": brand, "enroll_data_source": 0})
    user_doc.save(ignore_permissions=True)
    frappe.db.commit()

    # Queue Get Enroll Data directly (bypass the device-link check in _ensure_device_user_synced)
    if sn:
        already_pending = frappe.db.exists("Attendance Device Command", {
            "attendance_device": sn,
            "attendance_device_user": user_doc.name,
            "command_type": "Get Enroll Data",
            "status": "Pending",
        })
        if not already_pending:
            cmd = frappe.new_doc("Attendance Device Command")
            cmd.attendance_device = sn
            cmd.attendance_device_user = user_doc.name
            cmd.command_type = "Get Enroll Data"
            cmd.status = "Pending"
            cmd.insert(ignore_permissions=True)
            frappe.db.commit()


def _localize_device_timestamp(naive_ts: datetime, sn: str | None) -> datetime:
    """Convert a naive device-local timestamp to a naive UTC-equivalent datetime.

    ZKTeco ATTLOG/rtlog timestamps are in device local time (whatever TimeZone= the
    device has been configured with).  We store the per-device timezone in
    Attendance Device.device_timezone.  If unset, the timestamp is treated as already
    in the site timezone (current legacy behaviour — no change).

    Returns a naive datetime suitable for passing directly to frappe.new_doc("Employee Checkin").
    Frappe stores checkin times as-is (naive, in the site timezone column).  So we convert
    device-local → UTC → site-local before stripping tzinfo.
    """
    device_tz_name: str | None = None
    if sn:
        device_tz_name = frappe.db.get_value("Attendance Device", sn, "device_timezone") or None

    if not device_tz_name:
        return naive_ts  # legacy: treat as site local, no conversion needed

    try:
        from zoneinfo import ZoneInfo
        # Attach device timezone, then convert to site timezone
        site_tz_name = frappe.utils.get_system_timezone()
        device_aware = naive_ts.replace(tzinfo=ZoneInfo(device_tz_name))
        site_aware = device_aware.astimezone(ZoneInfo(site_tz_name))
        return site_aware.replace(tzinfo=None)  # strip tzinfo — Frappe stores naive datetimes
    except Exception:
        return naive_ts  # on any error, fall back to as-is


def _store_registry_capabilities(sn: str, body: str) -> None:
    """Parse ZKTeco registry POST body and store capability fields on the device record.

    Most firmware sends URL-encoded form data on a single line:
      SN=xxx&FirmVer=14.00&DeviceName=MB360&MAC=00:17:61:xx:xx:xx&MaxUserCount=3000&...
    Some older firmware sends newline-separated key=value pairs.
    We handle both formats.
    """
    from urllib.parse import parse_qs

    kv: dict = {}
    body = body.strip()

    if "&" in body:
        # URL-encoded form data (most firmware versions)
        for k, vals in parse_qs(body, keep_blank_values=True).items():
            kv[k.strip()] = vals[0] if vals else ""
    else:
        # Newline-separated key=value (older firmware)
        for line in body.splitlines():
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()

    updates: dict = {}
    if kv.get("FirmVer"):
        updates["firmware_version"] = kv["FirmVer"]
    if kv.get("DeviceName"):
        # DeviceName from registry may be more precise than user-entered device_name;
        # only populate firmware_version/MAC/capabilities — don't overwrite user's name.
        pass
    if kv.get("MAC"):
        updates["mac_address"] = kv["MAC"]
    if kv.get("MaxUserCount"):
        try:
            updates["max_users"] = int(kv["MaxUserCount"])
        except ValueError:
            pass
    # MultiBioDataSupport e.g. "FP,FACE,CARD" or "0101" bitmask — store as-is.
    bio = kv.get("MultiBioDataSupport") or kv.get("BioSupport") or kv.get("SupportedBio")
    if bio:
        updates["supported_biometrics"] = bio

    if updates:
        frappe.db.set_value("Attendance Device", sn, updates)
        frappe.db.commit()


def _get_device_tz_hours(sn: str | None) -> str:
    """Return timezone offset hours for a specific device.

    Checks device_timezone field first; falls back to site timezone.
    Returns a string suitable for ZKTeco's TimeZone= parameter.
    """
    if sn:
        device_tz = frappe.db.get_value("Attendance Device", sn, "device_timezone")
        if device_tz:
            try:
                from zoneinfo import ZoneInfo
                now_local = datetime.now(ZoneInfo(device_tz))
                offset_hours = now_local.utcoffset().total_seconds() / 3600
                if offset_hours == int(offset_hours):
                    return str(int(offset_hours))
                return str(offset_hours)
            except Exception:
                pass  # fall through to site timezone
    return _get_frappe_tz_hours()
