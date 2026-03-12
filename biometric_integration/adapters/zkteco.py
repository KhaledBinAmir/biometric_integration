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
        # ping, exchange, querydata, edata, file — acknowledge only
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

        body = (
            f"GET OPTION FROM: {sn}\n"
            f"ATTLOGStamp={last_sync_id}\n"
            "OPERLOGStamp=9999\n"
            "ATTPHOTOStamp=None\n"
            "ErrorDelay=30\n"
            "Delay=10\n"
            "TransTimes=00:00;14:05\n"
            "TransInterval=1\n"
            "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\n"
            + (f"TimeZone={_get_frappe_tz_hours()}\n" if settings.push_timezone_to_device else "")
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
        body = (
            f"ATTLOGStamp={last_sync_id}\n"
            "OPERLOGStamp=9999\n"
            "ATTPHOTOStamp=None\n"
            "ErrorDelay=30\n"
            "Delay=10\n"
            "TransTimes=00:00;14:05\n"
            "TransInterval=1\n"
            "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\n"
            + (f"TimeZone={_get_frappe_tz_hours()}\n" if settings.push_timezone_to_device else "")
            + "Realtime=1\n"
            "Encrypt=None\n"
        )
        return self.text(body)

    # ------------------------------------------------------------------
    # Server time  (GET /iclock/rtdata?type=time)
    # ------------------------------------------------------------------

    def _handle_rtdata(self, args) -> Response:
        """Device requests server time for clock sync.

        DateTime is Greenwich Mean Time (UTC) encoded with ZKTeco's custom formula (Appendix 5):
          tt = ((year-2000)*12*31 + (mon-1)*31 + day-1) * 86400 + (hour*60+min)*60 + sec

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
                log_id = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0

                ts = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
                create_employee_checkin(
                    device_pin=pin,
                    timestamp=ts,
                    device_id=sn,
                )
                maybe_log(sn, "Attendance", "IN", f"PIN={pin} time={time_str}", user_pin=pin, raw_data=line)
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
            ts = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
            create_employee_checkin(device_pin=pin, timestamp=ts, device_id=sn)
            maybe_log(sn, "Attendance", "IN", f"RT PIN={pin} time={time_str}", user_pin=pin, raw_data=body)
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
                    cmd_doc.closed_on = datetime.now()
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
        tz_name = frappe.utils.get_time_zone()
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
        tz_name = frappe.utils.get_time_zone()
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
