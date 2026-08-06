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

import re
from datetime import datetime, timezone

import frappe
from frappe.utils import cint
from werkzeug.wrappers import Response

from biometric_integration.adapters.base import AbstractDeviceAdapter
from biometric_integration.services.checkin import create_employee_checkin
from biometric_integration.services.command_processor import process_device_command
from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
    get_or_create_user_by_pin,
    update_zkteco_enrollment,
)
from biometric_integration.biometric_integration.doctype.attendance_device_log.attendance_device_log import maybe_log
from biometric_integration.utils.device_cache import (
    is_registered_device,
    touch_device,
    get_last_sync_id,
    set_last_sync_id,
    get_employee_by_pin,
)


class ZKTecoAdapter(AbstractDeviceAdapter):

    def dispatch(self) -> Response:
        path = self.path  # e.g. "/iclock/cdata"
        args = self.request.args

        # Any authenticated request proves the device is alive. Previously only the
        # handshake/registry paths recorded this, so a device that polled for
        # commands and uploaded punches (but did not re-handshake) looked offline
        # for days — misleading on the form indicator and when diagnosing. The
        # write is debounced to once per minute per device, so this is cheap.
        _sn = args.get("SN") or args.get("sn")
        if _sn and is_registered_device(_sn):
            touch_device(_sn)

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
            # MUST be non-200: a 200 here makes ZK firmware treat the options-fetch
            # as "done" and drop into command-poll-only mode, never re-handshaking
            # until a power-cycle — so a device added to ERPNext slightly after it
            # started polling would push nothing (attendance/enrollment) for days.
            # A non-200 makes the firmware keep retrying the handshake until the
            # device is registered.
            return self.text("ERROR: Device not registered.", 400)

        touch_device(sn)

        body = f"GET OPTION FROM: {sn}\n" + _build_config_options(sn)
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
            _store_device_capabilities(sn, body_str)
        maybe_log(sn or "unknown", "Handshake", "IN", f"Registry SN={sn}",
                  raw_data=self.raw_dump("RegistryCode=0"))
        return self.text("RegistryCode=0")

    # ------------------------------------------------------------------
    # Config download  (POST /iclock/push)
    # ------------------------------------------------------------------

    def _handle_push(self, args) -> Response:
        """Device downloads full config after registration. Return same params as handshake."""
        sn = args.get("SN") or args.get("sn")
        return self.text(_build_config_options(sn))

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

        # The device's own upload cursor. We echo it back on the next handshake so
        # it only re-sends NEW records instead of its whole history every time.
        stamp = args.get("Stamp") or args.get("stamp")

        if table == "ATTLOG":
            resp = self._process_attlog(sn, body_str)
            _save_stamp(sn, "last_attlog_stamp", stamp)  # only after a successful ingest
            return resp
        if table == "rtlog":
            return self._process_rtlog(sn, body_str)
        if table == "OPERLOG":
            resp = self._process_operlog(sn, body_str)
            _save_stamp(sn, "last_operlog_stamp", stamp)
            return resp
        if table == "options":
            # Device pushes its capabilities (firmware, MAC, max users, FP/face
            # algorithm versions, function switches). Capture them onto the device.
            _store_device_capabilities(sn, body_str)
            return self.text("OK")
        if table == "tabledata":
            # Unified template (biodata) upload via cdata. Some firmware answers a
            # biodata DATA QUERY here instead of via /iclock/querydata.
            tablename = args.get("tablename", "")
            if tablename.lower() == "biodata":
                n = _process_biodata_records(sn, body_str)
                return self.text(f"biophoto={n}")
        # rtstate and other tables — acknowledge
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
        sn0 = args.get("SN") or args.get("sn")
        if not is_registered_device(sn0):
            return self.text("ERROR: Device not registered.")

        table = args.get("tablename", "")
        cmd_id = args.get("cmdid")
        count_str = args.get("count", "0")

        # Ownership: only let a device close its own command (names are sequential).
        if cmd_id and frappe.db.get_value("Attendance Device Command", cmd_id, "attendance_device") != sn0:
            cmd_id = None

        # Some firmware returns a `DATA QUERY ATTLOG` result here instead of via a
        # plain /iclock/cdata?table=ATTLOG POST — route it through the same parser.
        if table.lower() == "attlog":
            sn = args.get("SN") or args.get("sn")
            body_str = self.raw_body.decode("utf-8", errors="ignore")
            return self._process_attlog(sn or "", body_str)

        # Unified biometric template query response (Get Enroll Data). Each line
        # carries type/majorver/format — what we need to re-enroll on other models.
        if table.lower() == "biodata":
            sn = args.get("SN") or args.get("sn")
            body_str = self.raw_body.decode("utf-8", errors="ignore")
            n = _process_biodata_records(sn or "", body_str)
            if cmd_id and n:
                try:
                    frappe.db.set_value("Attendance Device Command", cmd_id, {
                        "status": "Success",
                        "closed_on": frappe.utils.now_datetime(),
                        "device_response": f"Received {n} template(s)",
                    })
                    frappe.db.commit()
                except Exception:
                    pass
            return self.text(f"biophoto={n}")

        # Device capability probe response (Refresh Device Info → GET OPTIONS).
        # Firmware differs on how it answers: some POST options here via querydata
        # (type=options / tablename=options), others via /iclock/cdata?table=options
        # (handled in _upload). Parse both into the device's capability fields.
        if table.lower() == "options" or args.get("type") == "options":
            body_str = self.raw_body.decode("utf-8", errors="ignore")
            _store_device_capabilities(sn0, body_str)
            if cmd_id:
                try:
                    frappe.db.set_value("Attendance Device Command", cmd_id, {
                        "status": "Success",
                        "closed_on": frappe.utils.now_datetime(),
                        "device_response": f"Options reported: {body_str.strip()[:500]}",
                    })
                    frappe.db.commit()
                except Exception:
                    pass
            maybe_log(sn0 or "unknown", "Handshake", "IN",
                      f"Device options reported SN={sn0}", raw_data=body_str)
            return self.text("OK")

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
        # Gate on registration: command payloads (Enroll User) carry raw biometric
        # templates + PII, so an unregistered/forged serial must never be able to
        # poll them out of the queue.
        if not is_registered_device(sn):
            return self.text("ERROR: Device not registered.")
        command_str = process_device_command(sn)
        if command_str:
            maybe_log(sn, "Command", "OUT", f"Sending command to {sn}", raw_data=command_str)
        return self.text(command_str or "OK")

    # ------------------------------------------------------------------
    # Command result  (POST /iclock/devicecmd)
    # ------------------------------------------------------------------

    def _handle_devicecmd(self, args) -> Response:
        sn = args.get("SN") or args.get("sn")
        if not is_registered_device(sn):
            return self.text("ERROR: Device not registered.")
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
                    # Ownership check: a device may only report results for its own
                    # commands. Command names are sequential, so without this a forged
                    # request could mark or poison another device's command.
                    if cmd_doc.attendance_device != sn:
                        maybe_log(sn, "Error", "IN",
                                  f"devicecmd for command {cmd_id} not owned by {sn} — ignored",
                                  force=True)
                        continue
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
        emp = get_employee_by_pin(pin)
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


def _process_biodata_records(sn: str, body: str) -> int:
    """Parse a body containing one or more `biodata ...` template lines and merge
    each into its user's ZKTeco enrollment JSON. Returns the number stored.

    Used for both OPERLOG uploads and DATA QUERY biodata responses (querydata or
    cdata?table=tabledata).
    """
    stored = 0
    for line in body.strip().splitlines():
        line = line.strip()
        if line.lower().startswith("biodata"):
            try:
                stored += _handle_operlog_biodata(sn, line)
            except Exception as exc:
                frappe.log_error(title="ZKTeco biodata Parse Error",
                                 message=f"SN={sn} Line: {line!r}\n{exc}")
    return stored


def _parse_biodata_fields(line: str) -> dict:
    """Parse a `biodata k=v k=v ... tmp=base64` line into a lowercase-keyed dict.

    Tolerant of both space- and tab-separated fields and either `tmp=`/`TMP=`.
    The template value is the last field and is base64 (no embedded whitespace),
    so a plain whitespace split is safe.
    """
    body = re.sub(r"^\s*biodata\s+", "", line, flags=re.IGNORECASE)
    fields: dict = {}
    for tok in body.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            fields[k.strip().lower()] = v.strip()
    return fields


def _handle_operlog_biodata(sn: str, line: str) -> int:
    """Handle a biodata line — newer unified firmware format (all firmware/models).

    biodata pin=X no=Y index=I valid=V duress=D type=T majorver=M minorver=m format=F tmp=base64
    Covers all biometric types: 1=FP, 2=NIR face, 8=Palm vein, 9=Visible face, etc.
    `format` (0=ZK, 1=ISO, 2=ANSI) and `majorver` (algorithm version) are captured
    so the template can be pushed back to any device that shares the same version.
    """
    f = _parse_biodata_fields(line)
    tmp = f.get("tmp")
    pin = f.get("pin")
    if not tmp or not pin:
        return 0
    user_doc = get_or_create_user_by_pin(pin)
    update_zkteco_enrollment(user_doc, sn, biometric={
        "type": int(f.get("type", 1)),
        "no": int(f.get("no", 0)),
        "index": int(f.get("index", 0)),
        "size": int(f.get("size", 0)),
        "valid": int(f.get("valid", 1)),
        "duress": int(f.get("duress", 0)),
        "majorver": int(f.get("majorver", 0)),
        "minorver": int(f.get("minorver", 0)),
        "format": int(f.get("format", 0)),
        "tmp": tmp,
    })
    maybe_log(sn, "Enrollment", "IN",
              f"biodata PIN={pin} type={f.get('type')} no={f.get('no')} majorver={f.get('majorver')}",
              user_pin=pin)
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
    except Exception as exc:
        # A typo'd device_timezone would silently shift every punch by the
        # offset — surface it (once per device per day, punches are frequent).
        warn_key = f"biometric:tz_warned:{sn}"
        if not frappe.cache.get_value(warn_key):
            frappe.cache.set_value(warn_key, "1", expires_in_sec=86400)
            frappe.log_error(
                title="Device timezone conversion failed",
                message=f"device={sn} device_timezone={device_tz_name!r}: {exc} — storing timestamps unconverted",
            )
        return naive_ts  # fall back to as-is


# Stamp values that must never be stored as a cursor: "0" means "resend everything"
# and 9999 is a known suppress-all sentinel on this fleet.
_SENTINEL_STAMPS = {"0", "9999"}


def _save_stamp(sn: str | None, field: str, stamp: str | None) -> None:
    """Persist the device's upload cursor AFTER its batch was ingested.

    Called only once the records in that POST have been processed, so the cursor
    can never run ahead of what we actually hold — a crash mid-batch just leaves
    the old (lower) cursor, and the device re-sends. Never moves backwards, and a
    missing/blank stamp is left alone so we keep asking for everything (safe).
    """
    if not sn or stamp is None:
        return
    stamp = str(stamp).strip()
    if not stamp or stamp in _SENTINEL_STAMPS:
        # "0" means "send everything" and 9999 is a known suppress-all sentinel on
        # this fleet (see _build_config_options). Echoing either back would be wrong:
        # 0 restarts the full re-dump, 9999 can silence uploads entirely. Storing
        # neither leaves the cursor blank, which is the safe "ask for everything".
        return
    try:
        current = frappe.db.get_value("Attendance Device", sn, field)
        # Numeric cursors must only ever advance; non-numeric ones are replaced.
        if current and str(current).isdigit() and stamp.isdigit():
            if int(stamp) <= int(current):
                return
        frappe.db.set_value("Attendance Device", sn, field, stamp, update_modified=False)
        frappe.db.commit()
    except Exception:
        frappe.log_error(title="ZKTeco stamp persist failed",
                         message=f"device={sn} field={field} stamp={stamp!r}")


def _build_config_options(sn: str | None) -> str:
    """Build the ADMS 'GET OPTION' config block sent at handshake / push.

    Stamps are the device's upload cursors: it re-sends everything NEWER than the
    value we echo back. `0` means "send your entire history".

    We echo the cursor the device itself gave us on its last successful upload
    (last_attlog_stamp / last_operlog_stamp). Pinning these to 0 permanently made
    every handshake trigger a full re-dump of all punches + the whole operation
    log; on a device with many users/punches that saturates its flash and CPU, so
    fingerprint matching starves ("please try again"), and a reboot does not help
    because the loop restarts on reconnect.

    Fail-safe: when we have no cursor yet (or a deliberate resync is requested via
    force_full_resync, used by Sync Enrollments to replay locally-enrolled
    fingerprints) we send 0 = send everything. Duplicate check-ins are rejected
    idempotently, so a full resend is always safe — never lossy, only slower.
    """
    settings = frappe.get_cached_doc("Attendance Integration Settings")
    att_stamp = oper_stamp = "0"
    if sn:
        dev = frappe.db.get_value(
            "Attendance Device", sn,
            ["last_attlog_stamp", "last_operlog_stamp", "force_full_resync"], as_dict=True,
        ) or {}
        if cint(dev.get("force_full_resync")):
            # One-off deliberate full replay; clear the flag so the next handshake
            # goes back to incremental.
            frappe.db.set_value("Attendance Device", sn, "force_full_resync", 0,
                                update_modified=False)
            frappe.db.commit()
        else:
            att_stamp = str(dev.get("last_attlog_stamp") or "0")
            oper_stamp = str(dev.get("last_operlog_stamp") or "0")
    poll_delay = int(settings.device_poll_delay or 10)
    error_delay = int(settings.device_error_delay or 30)
    trans_times = settings.trans_times or "00:00;14:05"
    trans_interval = int(settings.trans_interval or 1)
    # Declare multi-bio support so newer (unified/hybrid) firmware will UPLOAD a
    # locally-enrolled fingerprint as a BIODATA template — the only way to CAPTURE
    # an on-device enrollment on fp_version >= 10 devices (classic FINGERTMP/CHECK
    # never surface it). Echo the device's own reported capability bitmap when we
    # have it; otherwise advertise fingerprint (type 1) so capture can bootstrap on
    # devices whose caps we haven't read yet. Classic firmware ignores this option,
    # so it's safe fleet-wide.
    bio = (frappe.db.get_value("Attendance Device", sn, "supported_biometrics") if sn else None)
    multibio = bio if (bio and ":" in bio) else "0:1:0:0:0:0:0:0:0:0"
    return (
        f"ATTLOGStamp={att_stamp}\n"
        f"OPERLOGStamp={oper_stamp}\n"
        "ATTPHOTOStamp=None\n"
        f"ErrorDelay={error_delay}\n"
        f"Delay={poll_delay}\n"
        f"TransTimes={trans_times}\n"
        f"TransInterval={trans_interval}\n"
        "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\n"
        f"MultiBioDataSupport={multibio}\n"
        + (f"TimeZone={_get_device_tz_hours(sn)}\n" if settings.push_timezone_to_device else "")
        + "Realtime=1\n"
        "Encrypt=None\n"
    )


def _parse_device_kv(body: str) -> dict:
    """Parse a device capability body into a key=value dict.

    Three wire formats observed in the wild (registry POST and table=options POST):
    1. Comma-separated (ADMS spec):
         DeviceType=acc,~DeviceName=MB360,FirmVer=14.00,MAC=00:17:61:xx:xx:xx,...
         FWVersion=...,FPVersion=10,FingerFunOn=1,FaceFunOn=1,~MaxUserCount=100,...
    2. URL-encoded (some firmware):  SN=xxx&FirmVer=14.00&MAC=...&MaxUserCount=3000
    3. Newline-separated (older firmware):  FirmVer=14.00\nMAC=...\n...

    Fields prefixed with ~ in the ADMS spec are optional; we strip the tilde.
    """
    from urllib.parse import parse_qs

    kv: dict = {}
    body = (body or "").strip()
    if not body:
        return kv

    if "&" in body and "\n" not in body:
        for k, vals in parse_qs(body, keep_blank_values=True).items():
            kv[k.strip().lstrip("~")] = vals[0] if vals else ""
    elif "," in body and "=" in body:
        # Comma-separated ADMS spec — also split on newlines (PDF-style wrapping)
        for part in re.split(r"[,\n]", body):
            part = part.strip().lstrip("~")
            if "=" in part:
                k, _, v = part.partition("=")
                kv[k.strip()] = v.strip()
    else:
        for line in body.splitlines():
            line = line.strip().lstrip("~")
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()
    return kv


def _store_device_capabilities(sn: str, body: str) -> None:
    """Parse a registry or table=options body and store capability fields on the device.

    Captures firmware, MAC, max users, the fingerprint algorithm version (fp_version —
    critical: templates only transfer between devices sharing it) and the supported
    biometric modalities (from an explicit list or the FingerFunOn/FaceFunOn switches).
    """
    if not sn:
        return
    kv = _parse_device_kv(body)
    updates: dict = {}

    fw = kv.get("FWVersion") or kv.get("FirmVer")
    if fw:
        updates["firmware_version"] = fw
    # Model name — DeviceName is the human model string (e.g. "MB360"); DeviceType
    # ("acc"/"att") is a generic class, so only fall back to it if nothing better.
    model = kv.get("DeviceName") or kv.get("MachineType")
    if model:
        updates["device_model"] = model
    if kv.get("MAC"):
        updates["mac_address"] = kv["MAC"]
    if kv.get("MaxUserCount"):
        try:
            updates["max_users"] = int(kv["MaxUserCount"])
        except ValueError:
            pass
    fpv = kv.get("FPVersion") or kv.get("ZKFPVersion")
    if fpv:
        try:
            updates["fp_version"] = int(fpv)
        except ValueError:
            pass

    # Supported biometrics: explicit list if present, else derive from function switches.
    bio = kv.get("MultiBioDataSupport") or kv.get("BioSupport") or kv.get("SupportedBio")
    if not bio:
        modalities = []
        if kv.get("FingerFunOn") == "1":
            modalities.append("Fingerprint")
        if kv.get("FaceFunOn") == "1":
            modalities.append("Face")
        if kv.get("FvFunOn") == "1":
            modalities.append("Finger Vein")
        if kv.get("PvFunOn") == "1":
            modalities.append("Palm Vein")
        if modalities:
            bio = ", ".join(modalities)
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
