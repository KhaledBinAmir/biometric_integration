# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Enable/disable the nginx HTTP listener for biometric devices.

Writes to a separate /etc/nginx/conf.d/biometric_listener.conf file so the
config survives `bench setup nginx` (which regenerates config/nginx.conf).
"""

from __future__ import annotations

import json
import os
import subprocess

import frappe
from frappe.utils import get_site_path
from frappe.installer import update_site_config

from biometric_integration.proxy.template import get_server_block

LISTENER_PORT_KEY = "biometric_listener_port"
NGINX_CONF_PATH = "/etc/nginx/conf.d/biometric_listener.conf"


# ---------------------------------------------------------------------------
# Public functions (called from api.py whitelist methods and CLI)
# ---------------------------------------------------------------------------

def enable_listener_logic(site: str, port: int) -> tuple[bool, str]:
    """Write a standalone nginx config file and reload nginx."""
    # Check if already enabled with same port
    existing_port = _get_site_config(LISTENER_PORT_KEY)
    if existing_port == port and os.path.exists(NGINX_CONF_PATH):
        return True, f"Listener for {site} on port {port} already exists."

    block = get_server_block(site, port)

    ok, msg = _write_nginx_conf(block)
    if not ok:
        return False, msg

    _set_site_config(LISTENER_PORT_KEY, port)

    ok, msg = _reload_nginx()
    if not ok:
        # Roll back: remove the config file we just wrote
        _remove_nginx_conf()
        _remove_site_config(LISTENER_PORT_KEY)
        return False, msg

    return True, f"HTTP listener enabled on port {port}."


def disable_listener_logic(site: str) -> tuple[bool, str]:
    """Remove the biometric nginx config file and reload."""
    port = _get_site_config(LISTENER_PORT_KEY)
    if not port and not os.path.exists(NGINX_CONF_PATH):
        return True, "Listener is not enabled."

    _remove_nginx_conf()
    _remove_site_config(LISTENER_PORT_KEY)

    # Also clean up legacy block from config/nginx.conf if present
    _cleanup_legacy_block(site, port)

    ok, msg = _reload_nginx()
    return (True, "HTTP listener disabled.") if ok else (False, msg)


def get_status_logic(site: str) -> dict:
    """Return current listener status."""
    port = _get_site_config(LISTENER_PORT_KEY)
    if port:
        return {"enabled": True, "port": port}
    return {"enabled": False}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_nginx_conf(content: str) -> tuple[bool, str]:
    """Write to /etc/nginx/conf.d/ using sudo tee."""
    try:
        result = subprocess.run(
            ["sudo", "tee", NGINX_CONF_PATH],
            input=content,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"Failed to write nginx config: {result.stderr}"
        # Validate config before reloading
        test = subprocess.run(
            ["sudo", "nginx", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if test.returncode != 0:
            # Invalid config — remove it
            _remove_nginx_conf()
            return False, f"nginx config test failed: {test.stderr}"
        return True, ""
    except Exception as e:
        return False, f"Failed to write nginx config: {e}"


def _remove_nginx_conf() -> None:
    """Remove the biometric nginx config file."""
    try:
        subprocess.run(
            ["sudo", "rm", "-f", NGINX_CONF_PATH],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _cleanup_legacy_block(site: str, port) -> None:
    """Remove the biometric block from config/nginx.conf (legacy v2.0 installs)."""
    import re
    from frappe.utils import get_bench_path

    nginx_conf = os.path.join(get_bench_path(), "config", "nginx.conf")
    if not os.path.exists(nginx_conf):
        return
    try:
        with open(nginx_conf, "r") as f:
            content = f.read()
        if "BIOMETRIC_LISTENER_START" not in content:
            return
        # Remove any biometric listener block
        pattern = re.compile(
            r"\s*# -- BIOMETRIC_LISTENER_START.*?# -- BIOMETRIC_LISTENER_END[^\n]*",
            re.DOTALL,
        )
        cleaned = pattern.sub("", content)
        if cleaned != content:
            with open(nginx_conf, "w") as f:
                f.write(cleaned)
    except Exception:
        pass  # non-critical cleanup


def _reload_nginx() -> tuple[bool, str]:
    try:
        subprocess.run(
            ["sudo", "service", "nginx", "reload"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return True, ""
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False, f"nginx reload failed: {getattr(e, 'stderr', str(e))}"


def _get_site_config(key: str):
    return frappe.conf.get(key)


def _set_site_config(key: str, value) -> None:
    update_site_config(key, value, site_config_path=get_site_path("site_config.json"))


def _remove_site_config(key: str) -> None:
    path = get_site_path("site_config.json")
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        conf = json.load(f)
    if conf.pop(key, None) is not None:
        with open(path, "w") as f:
            json.dump(conf, f, indent=4)
