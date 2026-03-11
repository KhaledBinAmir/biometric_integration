# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""
Detect whether the current server supports UI-based reverse proxy configuration.

Checks (in order):
1. Is this Frappe Cloud?             → recommendation: cloud_instructions
2. Is nginx installed?
3. Is bench nginx.conf writable?
4. Can nginx be reloaded via sudo?   → recommendation: ui_configure or ui_configure_no_reload
5. Otherwise                         → recommendation: show_generated_config
"""

from __future__ import annotations

import os
import shutil
import subprocess

import frappe
from frappe.utils import get_bench_path


def check_proxy_compatibility() -> dict:
    result = {
        "is_frappe_cloud": False,
        "nginx_installed": False,
        "nginx_conf_writable": False,
        "sudo_reload_available": False,
        "compatible": False,
        "recommendation": "show_generated_config",
        "nginx_conf_path": None,
    }

    # 1. Frappe Cloud detection
    result["is_frappe_cloud"] = bool(
        frappe.conf.get("frappe_cloud")
        or os.path.exists("/etc/frappe-cloud")
        or os.environ.get("FRAPPE_CLOUD")
    )
    if result["is_frappe_cloud"]:
        result["recommendation"] = "cloud_instructions"
        return result

    # 2. Is nginx installed?
    result["nginx_installed"] = bool(shutil.which("nginx"))

    # 3. Is bench nginx.conf writable?
    nginx_conf = os.path.join(get_bench_path(), "config", "nginx.conf")
    result["nginx_conf_path"] = nginx_conf
    result["nginx_conf_writable"] = (
        os.path.exists(nginx_conf) and os.access(nginx_conf, os.W_OK)
    )

    # 4. Can we reload nginx without a password prompt?
    if result["nginx_installed"]:
        try:
            check = subprocess.run(
                ["sudo", "-n", "service", "nginx", "configtest"],
                capture_output=True,
                timeout=3,
            )
            result["sudo_reload_available"] = check.returncode == 0
        except Exception:
            result["sudo_reload_available"] = False

    # Determine recommendation
    if result["nginx_installed"] and result["nginx_conf_writable"]:
        result["compatible"] = True
        result["recommendation"] = "ui_configure"
    else:
        result["compatible"] = False
        result["recommendation"] = "show_generated_config"

    return result
