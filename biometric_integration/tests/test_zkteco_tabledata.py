# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""ZKTeco `cdata?table=tabledata` uploads and their acknowledgement.

The device expects `<tablename>=<count>` back; any other reply makes it re-send
the same batch indefinitely.

    bench --site test.pidyen.com run-tests --module biometric_integration.tests.test_zkteco_tabledata
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from biometric_integration.adapters.zkteco import ZKTecoAdapter

SERIAL = "ZZTABLE-A"


def biodata_line(pin, finger):
    return (
        f"biodata pin={pin}\tno={finger}\tindex=0\tvalid=1\tduress=0\ttype=1"
        f"\tmajorver=13\tminorver=0\tformat=0\ttmp=QUJDREVG"
    )


class TestTabledataUpload(IntegrationTestCase):
    def setUp(self):
        frappe.db.savepoint("tabledata")
        # Device request handlers commit; keep everything in the savepoint.
        self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
        self._no_commit.start()
        frappe.get_doc(
            {"doctype": "Attendance Device", "serial": SERIAL, "device_name": SERIAL, "brand": "ZKTeco"}
        ).insert(ignore_permissions=True)

    def tearDown(self):
        self._no_commit.stop()
        frappe.db.rollback(save_point="tabledata")

    def upload(self, query, body=""):
        environ = EnvironBuilder(
            method="POST",
            path="/iclock/cdata",
            query_string=f"SN={SERIAL}&table=tabledata{query}",
            data=body.encode(),
        ).get_environ()
        return ZKTecoAdapter(Request(environ)).dispatch().get_data(as_text=True)

    def stored_fingers(self, pin):
        url = frappe.db.get_value("Attendance Device User", {"user_id": pin}, "zkteco_enroll_data")
        if not url:
            return []
        content = frappe.get_doc("File", {"file_url": url}).get_content()
        return [b["no"] for b in json.loads(content)["biometrics"]]

    def test_biodata_batch_is_acknowledged_with_its_own_table_name(self):
        body = "\n".join([biodata_line("990301", 6), biodata_line("990301", 7)])
        self.assertEqual(self.upload("&tablename=biodata&count=2", body), "biodata=2")
        self.assertEqual(sorted(self.stored_fingers("990301")), [6, 7])

    def test_biodata_without_count_echoes_the_parsed_count(self):
        self.assertEqual(self.upload("&tablename=biodata", biodata_line("990302", 1)), "biodata=1")

    def test_other_tables_are_echoed_by_name(self):
        self.assertEqual(self.upload("&tablename=userpic&count=3", "pin=1\tsize=0\tcontent="), "userpic=3")

    def test_upload_without_table_name(self):
        self.assertEqual(self.upload(""), "OK")
