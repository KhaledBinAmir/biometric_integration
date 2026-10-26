# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Refresh Device Info: ZKTeco INFO and the device's reply.

Needs this app installed on the test site:
    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_device_info
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from biometric_integration.adapters.zkteco import ZKTecoAdapter
from biometric_integration.services.command_processor import process_device_command

INFO_REPLY = (
    "ID={cmd}&Return=0&CMD=INFO\n"
    "~DeviceName=SenseFP M2\n"
    "FWVersion=ZAM70-NF28HA-Ver3.2.2\n"
    "FPVersion=13\n"
    "TimeZone=1\n"
    "MAC=00:17:61:aa:bb:cc"
)


class TestDeviceInfo(IntegrationTestCase):
    def setUp(self):
        frappe.db.savepoint("device_info")
        # Device request handlers commit; keep everything in the savepoint.
        self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
        self._no_commit.start()
        self.device("ZZINFO-A")
        self.device("ZZINFO-B")

    def tearDown(self):
        self._no_commit.stop()
        frappe.db.rollback(save_point="device_info")

    def device(self, serial):
        return frappe.get_doc(
            {"doctype": "Attendance Device", "serial": serial, "device_name": serial, "brand": "ZKTeco"}
        ).insert(ignore_permissions=True)

    def command(self, serial, command_type="Refresh Device Info"):
        return frappe.get_doc(
            {
                "doctype": "Attendance Device Command",
                "attendance_device": serial,
                "brand": "ZKTeco",
                "command_type": command_type,
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)

    def post_devicecmd(self, serial, body, query=""):
        environ = EnvironBuilder(
            method="POST", path="/iclock/devicecmd", query_string=f"SN={serial}{query}", data=body.encode()
        ).get_environ()
        return ZKTecoAdapter(Request(environ)).dispatch()

    def test_refresh_sends_info_and_waits_for_the_reply(self):
        cmd = self.command("ZZINFO-A")
        self.assertEqual(process_device_command("ZZINFO-A"), f"C:{cmd.name}:INFO")
        self.assertEqual(frappe.db.get_value("Attendance Device Command", cmd.name, "status"), "Sent")

    def test_info_reply_is_kept_whole_and_fills_the_capabilities(self):
        cmd = self.command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        self.post_devicecmd("ZZINFO-A", INFO_REPLY.format(cmd=cmd.name))

        cmd.reload()
        self.assertEqual(cmd.status, "Success")
        self.assertIn("TimeZone=1", cmd.device_response)
        self.assertIn("MAC=00:17:61:aa:bb:cc", cmd.device_response)
        device = frappe.get_doc("Attendance Device", "ZZINFO-A")
        self.assertEqual(device.fp_version, 13)
        self.assertEqual(device.firmware_version, "ZAM70-NF28HA-Ver3.2.2")
        self.assertEqual(device.device_model, "SenseFP M2")

    def test_info_reply_from_another_device_is_ignored(self):
        cmd = self.command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        self.post_devicecmd("ZZINFO-B", INFO_REPLY.format(cmd=cmd.name))

        cmd.reload()
        self.assertEqual(cmd.status, "Sent")
        self.assertFalse(cmd.device_response)
        self.assertFalse(frappe.db.get_value("Attendance Device", "ZZINFO-B", "fp_version"))

    def test_other_command_acks_are_unchanged(self):
        cmd = self.command("ZZINFO-A", command_type="Unlock Door")
        process_device_command("ZZINFO-A")
        self.post_devicecmd("ZZINFO-A", f"ID={cmd.name}&Return=-13&CMD=CONTROL DEVICE")

        cmd.reload()
        self.assertEqual(cmd.status, "Failed")
        self.assertEqual(cmd.device_response, f"ID={cmd.name}&Return=-13&CMD=CONTROL DEVICE")

    def test_batched_results_each_close_their_own_command(self):
        info = self.command("ZZINFO-A")
        unlock = self.command("ZZINFO-A", command_type="Unlock Door")
        process_device_command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        body = INFO_REPLY.format(cmd=info.name) + f"\nID={unlock.name}&Return=0&CMD=CONTROL DEVICE"
        self.post_devicecmd("ZZINFO-A", body)

        info.reload()
        unlock.reload()
        self.assertEqual(info.status, "Success")
        self.assertNotIn(unlock.name, info.device_response)
        self.assertEqual(unlock.status, "Success")
        self.assertEqual(frappe.db.get_value("Attendance Device", "ZZINFO-A", "fp_version"), 13)

    def test_info_after_another_ack_still_fills_the_capabilities(self):
        unlock = self.command("ZZINFO-A", command_type="Unlock Door")
        info = self.command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        body = f"ID={unlock.name}&Return=-13&CMD=CONTROL DEVICE\n" + INFO_REPLY.format(cmd=info.name)
        self.post_devicecmd("ZZINFO-A", body)

        self.assertEqual(frappe.db.get_value("Attendance Device Command", unlock.name, "status"), "Failed")
        self.assertIn(
            "TimeZone=1", frappe.db.get_value("Attendance Device Command", info.name, "device_response")
        )
        self.assertEqual(frappe.db.get_value("Attendance Device", "ZZINFO-A", "fp_version"), 13)

    def test_info_with_id_and_return_in_the_url(self):
        info = self.command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        items = INFO_REPLY.format(cmd=info.name).split("\n", 1)[1]
        self.post_devicecmd("ZZINFO-A", items, query=f"&ID={info.name}&Return=-1002")

        info.reload()
        self.assertEqual(info.status, "Failed")
        self.assertIn("FPVersion=13", info.device_response)

    def test_info_values_keep_their_commas(self):
        info = self.command("ZZINFO-A")
        process_device_command("ZZINFO-A")
        body = f"ID={info.name}&Return=0&CMD=INFO\n~DeviceName=SpeedFace V5L, TI\nFPVersion=10"
        self.post_devicecmd("ZZINFO-A", body)

        self.assertEqual(
            frappe.db.get_value("Attendance Device", "ZZINFO-A", "device_model"), "SpeedFace V5L, TI"
        )
