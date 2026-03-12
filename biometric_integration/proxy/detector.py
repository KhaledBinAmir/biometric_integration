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

from biometric_integration.proxy.configurator import _detect_nginx_conf_dir


def check_proxy_compatibility() -> dict:
    result = {
        "is_frappe_cloud": False,
        "nginx_installed": False,
        "conf_dir_found": False,
        "sudo_reload_available": False,
        "compatible": False,
        "recommendation": "show_generated_config",
        "nginx_conf_dir": None,
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

    # 3. Can we find nginx conf.d directory?
    conf_dir = _detect_nginx_conf_dir()
    result["nginx_conf_dir"] = conf_dir
    result["conf_dir_found"] = conf_dir is not None

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
    if result["nginx_installed"] and result["conf_dir_found"]:
        result["compatible"] = True
        result["recommendation"] = "ui_configure"
    else:
        result["compatible"] = False
        result["recommendation"] = "show_generated_config"

    return result
