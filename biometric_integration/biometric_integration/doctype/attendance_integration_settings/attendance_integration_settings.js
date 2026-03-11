// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Integration Settings", {
	refresh(frm) {
		_load_endpoint_urls(frm);
		_check_proxy_compatibility(frm);
	},

	proxy_enabled(frm) {
		frm.toggle_display("proxy_port", frm.doc.proxy_enabled);
		if (frm.doc.proxy_enabled && !frm.doc.proxy_port) {
			frm.set_value("proxy_port", 8998);
		}
	},

	after_save(frm) {
		// Reload endpoint URLs so HTTP listener addresses appear/disappear
		_load_endpoint_urls(frm);
	},
});

function _load_endpoint_urls(frm) {
	frappe.call({
		method: "biometric_integration.api.get_endpoint_urls",
		callback(r) {
			if (!r.message) return;
			frm.set_value("zkteco_server_address", r.message.zkteco);
			frm.set_value("ebkn_server_address", r.message.ebkn);
		},
	});
}

function _check_proxy_compatibility(frm) {
	const wrapper = frm.get_field("proxy_compatibility_status").$wrapper;
	wrapper.html('<div class="text-muted small">Checking server compatibility...</div>');

	frappe.call({
		method: "biometric_integration.api.check_proxy_compatibility",
		callback(r) {
			if (!r.message) return;
			const d = r.message;
			let html = "";

			if (d.is_frappe_cloud) {
				html = `
<div class="alert alert-warning mb-0">
  <strong>Frappe Cloud detected.</strong><br>
  Biometric devices use plain HTTP and cannot connect directly to your Frappe Cloud HTTPS server.
  You need an on-premises Nginx reverse proxy on your local network that accepts HTTP from devices
  and forwards to this server.<br><br>
  Copy the <strong>Generated Nginx Config</strong> below and paste it into your local Nginx server.
</div>`;
				frm.toggle_display("generated_nginx_config", true);
				frm.toggle_display(["proxy_enabled", "proxy_port"], false);
				_load_generated_config(frm);

			} else if (d.recommendation === "ui_configure") {
				html = `
<div class="alert alert-success mb-0">
  <strong>Self-hosted server.</strong> You can configure the HTTP listener directly from this form.
</div>`;
				frm.toggle_display("proxy_enabled", true);
				frm.toggle_display("proxy_port", !!frm.doc.proxy_enabled);
				frm.toggle_display("generated_nginx_config", false);

			} else {
				html = `
<div class="alert alert-info mb-0">
  Nginx is not available or not writable on this server. Copy the <strong>Generated Nginx Config</strong>
  below and apply it manually.
</div>`;
				frm.toggle_display("generated_nginx_config", true);
				frm.toggle_display(["proxy_enabled", "proxy_port"], false);
				_load_generated_config(frm);
			}

			wrapper.html(html);
		},
	});
}

function _load_generated_config(frm) {
	frappe.call({
		method: "biometric_integration.api.get_generated_nginx_config",
		args: { port: frm.doc.proxy_port || 8998 },
		callback(r) {
			if (r.message) {
				frm.set_value("generated_nginx_config", r.message);
			}
		},
	});
}
