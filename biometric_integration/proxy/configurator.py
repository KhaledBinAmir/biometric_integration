# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Enable/disable the nginx HTTP listener for biometric devices."""

from __future__ import annotations

import json
import os
import re
import subprocess

import frappe
from frappe.utils import get_bench_path, get_site_path
from frappe.installer import update_site_config

from biometric_integration.proxy.template import get_server_block

LISTENER_PORT_KEY = "biometric_listener_port"


# ---------------------------------------------------------------------------
# Public functions (called from api.py whitelist methods and CLI)
# ---------------------------------------------------------------------------

def enable_listener_logic(site: str, port: int) -> tuple[bool, str]:
    """Inject a server block into bench nginx.conf and reload nginx."""
    nginx_conf = _nginx_conf_path()
    if not nginx_conf:
        return False, "bench nginx.conf not found"

    with open(nginx_conf, "r") as f:
        content = f.read()

    start_marker = f"# -- BIOMETRIC_LISTENER_START_{site}_{port} --"
    if start_marker in content:
        return True, f"Listener for {site} on port {port} already exists."

    new_block = get_server_block(site, port)
    _write_nginx_conf(nginx_conf, content.strip() + "\n\n" + new_block)
    _set_site_config(LISTENER_PORT_KEY, port)

    ok, msg = _reload_nginx()
    if ok:
        return True, f"HTTP listener enabled on port {port}."
    return False, msg


def disable_listener_logic(site: str) -> tuple[bool, str]:
    """Remove the biometric server block from nginx.conf and reload."""
    port = _get_site_config(LISTENER_PORT_KEY)
    if not port:
        return True, "Listener is not enabled."

    nginx_conf = _nginx_conf_path()
    if not nginx_conf:
        return False, "bench nginx.conf not found"

    with open(nginx_conf, "r") as f:
        content = f.read()

    start = f"# -- BIOMETRIC_LISTENER_START_{site}_{port} --"
    end = f"# -- BIOMETRIC_LISTENER_END_{site}_{port} --"
    pattern = re.compile(
        f"\\s*{re.escape(start)}.*?{re.escape(end)}", re.DOTALL
    )
    _write_nginx_conf(nginx_conf, pattern.sub("", content))
    _remove_site_config(LISTENER_PORT_KEY)

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

def _nginx_conf_path() -> str | None:
    path = os.path.join(get_bench_path(), "config", "nginx.conf")
    return path if os.path.exists(path) else None


def _write_nginx_conf(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)


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
