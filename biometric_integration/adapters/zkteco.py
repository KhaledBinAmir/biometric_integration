# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
ZKTeco ADMS (Attendance Device Management Service) adapter.

Protocol flow:
  GET  /iclock/cdata?SN=<serial>           → handshake / configuration
  POST /iclock/cdata?SN=<serial>&table=ATTLOG   → attendance logs
  POST /iclock/cdata?SN=<serial>&table=OPERLOG  → user / fingerprint data
  GET  /iclock/getrequest?SN=<serial>      → device polls for pending commands
  GET  /iclock/devicecmd?ID=<id>&Return=<code>  → device reports command result
  GET  /iclock/ping|registry|edata         → health checks → "OK"
"""

from __future__ import annotations

import re
from datetime import datetime

import frappe
from werkzeug.wrappers import Response

from biometric_integration.adapters.base import AbstractDeviceAdapter
from biometric_integration.services.checkin import create_employee_checkin
from biometric_integration.services.command_processor import process_device_command
from biometric_integration.biometric_integration.doctype.attendance_device_user.attendance_device_user import (
    get_or_create_user_by_pin,
    save_enrollment_data,
)
from biometric_integration.biometric_integration.doctype.attendance_device_log.attendance_device_log import maybe_log


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
        # ping, registry, edata — just acknowledge
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

        # Update device record on first contact
        if frappe.db.exists("Attendance Device", sn):
            frappe.db.set_value(
                "Attendance Device", sn,
                {"is_push_configured": 1, "last_contact": datetime.now()},
                update_modified=False,
            )

        last_sync_id = frappe.db.get_value("Attendance Device", sn, "last_synced_id") or 0
        maybe_log(sn, "Handshake", "IN", f"Handshake SN={sn}")

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
            "TimeZone=6\n"
            "Realtime=1\n"
            "Encrypt=None\n"
        )
        return self.text(body)

    # ------------------------------------------------------------------
    # Data upload
    # ------------------------------------------------------------------

    def _upload(self, args) -> Response:
        sn = args.get("SN") or args.get("sn")
        table = args.get("table", "")
        body_str = self.raw_body.decode("utf-8", errors="ignore")

        if table == "ATTLOG":
            return self._process_attlog(sn, body_str)
        if table == "OPERLOG":
            if "USER" in body_str:
                return self._process_user_data(sn, body_str)
            if "FP" in body_str:
                return self._process_fingerprint_data(sn, body_str)
        return self.text("OK")

    def _process_attlog(self, sn: str, body: str) -> Response:
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
            frappe.db.set_value(
                "Attendance Device", sn, "last_synced_id", latest_id, update_modified=False
            )
            frappe.db.commit()

        return self.text(f"OK: {processed}")

    def _process_user_data(self, sn: str, body: str) -> Response:
        processed = 0
        for line in body.strip().split("\n"):
            if not line.startswith("USER"):
                continue
            data = _parse_kv(line)
            pin = data.get("PIN")
            if pin:
                get_or_create_user_by_pin(pin, data.get("Name"))
                processed += 1
        return self.text(f"OK: {processed}")

    def _process_fingerprint_data(self, sn: str, body: str) -> Response:
        pattern = re.findall(
            r"FP PIN=(\S+)\s+FID=(\d+)\s+Size=(\d+)\s+Valid=(\d+)\s+TMP=(.*)", body
        )
        processed = 0
        for pin, fid, size, valid, template in pattern:
            try:
                user_doc = get_or_create_user_by_pin(pin)
                if user_doc:
                    save_enrollment_data(user_doc, "ZKTeco", sn, template.encode("utf-8"))
                    processed += 1
            except Exception as exc:
                frappe.log_error(title="ZKTeco FP Data Error", message=f"PIN={pin}: {exc}")
        return self.text(f"OK: {processed}")

    # ------------------------------------------------------------------
    # Command polling
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
    # Command result
    # ------------------------------------------------------------------

    def _handle_devicecmd(self, args) -> Response:
        body_str = self.raw_body.decode("utf-8", errors="ignore")
        for line in body_str.strip().split("\n"):
            # device sends: ID=<num>&Return=<code>&CMD=...
            from urllib.parse import parse_qs
            params = parse_qs(line)
            cmd_id = (params.get("ID") or [None])[0]
            return_code = (params.get("Return") or [None])[0]

            if not cmd_id:
                # Some devices embed params differently — try args
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

def _parse_kv(line: str) -> dict:
    """Parse ZKTeco key=value line format."""
    return dict(re.findall(r"(\w+)=(\S+)", line))
