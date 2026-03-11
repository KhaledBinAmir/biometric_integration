# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Frappe page_renderer classes that intercept ZKTeco (/iclock/*) and EBKN (/ebkn)
HTTP requests before any template lookup.

This approach works on Frappe Cloud AND self-hosted without any Nginx dependency
for basic operation. self.path is already stripped of the leading '/' by BaseRenderer.
"""

from __future__ import annotations

import frappe
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response


class ZKTecoRenderer(BaseRenderer):
    """Intercepts all /iclock/* paths for ZKTeco ADMS protocol."""

    def can_render(self):
        # self.path is stripped of leading '/', so '/iclock/cdata' → 'iclock/cdata'
        return self.path.startswith("iclock")

    def render(self):
        from biometric_integration.adapters.zkteco import ZKTecoAdapter
        try:
            frappe.set_user("Administrator")
            return ZKTecoAdapter(frappe.local.request).dispatch()
        except Exception:
            frappe.log_error(title="ZKTeco Renderer Error", message=frappe.get_traceback())
            return Response("Internal Server Error", status=500, mimetype="text/plain")
        finally:
            frappe.db.commit()


class EBKNRenderer(BaseRenderer):
    """Intercepts the /ebkn path for EBKN FkWeb protocol."""

    def can_render(self):
        return self.path == "ebkn"

    def render(self):
        from biometric_integration.adapters.ebkn import EBKNAdapter
        try:
            frappe.set_user("Administrator")
            return EBKNAdapter(frappe.local.request).dispatch()
        except Exception:
            frappe.log_error(title="EBKN Renderer Error", message=frappe.get_traceback())
            return Response("Internal Server Error", status=500)
        finally:
            frappe.db.commit()
