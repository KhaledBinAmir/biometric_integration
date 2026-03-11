# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

"""Nginx server block template generator."""

from __future__ import annotations


def get_server_block(site: str, port: int, gunicorn_port: int = 8000) -> str:
    """Generate the nginx server block for the biometric HTTP listener.

    Key differences from v1:
    - No X-Original-Request-URI header (page_renderer reads request.path directly)
    - underscores_in_headers on: required for EBKN's request_code, dev_id, blk_no headers
    - No header transformation: Werkzeug on Frappe side reads headers directly
    """
    from frappe.utils import get_url
    try:
        site_url = get_url()
        hostname = site_url.split("//")[-1].split("/")[0].split(":")[0]
    except Exception:
        hostname = site

    return f"""
# -- BIOMETRIC_LISTENER_START_{site}_{port} --
server {{
    listen {port};
    server_name _;

    # Required for EBKN: allows request_code, dev_id, blk_no, trans_id, cmd_return_code headers
    underscores_in_headers on;

    location / {{
        proxy_pass http://127.0.0.1:{gunicorn_port};
        proxy_set_header Host {hostname};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto http;
        proxy_pass_request_headers on;
        proxy_read_timeout 60;
        proxy_connect_timeout 10;
    }}
}}
# -- BIOMETRIC_LISTENER_END_{site}_{port} --
"""
