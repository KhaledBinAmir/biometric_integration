// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Integration Settings", {
	refresh(frm) {
		_load_endpoint_urls(frm);
		_check_proxy_compatibility(frm);
	},

	proxy_enabled(frm) {
		if (frm.doc.proxy_enabled && !frm.doc.proxy_port) {
			frm.set_value("proxy_port", 8998);
		}
		if (!frm.doc.proxy_enabled) {
			_disable_proxy(frm);
		}
	},

	proxy_port(frm) {
		// Debounced: only apply if proxy is enabled and port has changed
		if (frm.doc.proxy_enabled && frm.doc.proxy_port) {
			clearTimeout(frm._proxy_port_timer);
			frm._proxy_port_timer = setTimeout(() => _enable_proxy(frm), 1000);
		}
	},

	after_save(frm) {
		if (frm.doc.proxy_enabled) {
			_enable_proxy(frm);
		} else {
			_disable_proxy(frm);
		}
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
				frm.set_df_property("generated_nginx_config", "hidden", 0);
				frm.set_df_property("proxy_enabled", "hidden", 1);
				frm.set_df_property("proxy_port", "hidden", 1);
				// Set the global flag so depends_on expressions work
				frappe.proxy_compatible = false;
				_load_generated_config(frm);

			} else if (d.recommendation === "ui_configure") {
				html = `
<div class="alert alert-success mb-0">
  <strong>Self-hosted server.</strong> You can configure the HTTP listener directly from this form.
</div>`;
				frm.set_df_property("proxy_enabled", "hidden", 0);
				frm.set_df_property("proxy_port", "hidden", 0);
				frm.set_df_property("generated_nginx_config", "hidden", 1);
				frappe.proxy_compatible = true;
				_load_proxy_status(frm);

			} else {
				html = `
<div class="alert alert-info mb-0">
  Nginx is not available or not writable on this server. Copy the <strong>Generated Nginx Config</strong>
  below and apply it manually.
</div>`;
				frm.set_df_property("generated_nginx_config", "hidden", 0);
				frm.set_df_property("proxy_enabled", "hidden", 1);
				frm.set_df_property("proxy_port", "hidden", 1);
				frappe.proxy_compatible = false;
				_load_generated_config(frm);
			}

			wrapper.html(html);
		},
	});
}

function _load_proxy_status(frm) {
	frappe.call({
		method: "biometric_integration.api.get_proxy_status",
		callback(r) {
			if (!r.message) return;
			const status = r.message;
			if (status.enabled) {
				frm.set_value("proxy_enabled", 1);
				frm.set_value("proxy_port", status.port);
			}
		},
	});
}

function _enable_proxy(frm) {
	if (!frm.doc.proxy_port) return;
	frappe.call({
		method: "biometric_integration.api.enable_proxy",
		args: { port: frm.doc.proxy_port },
		callback(r) {
			if (r.message && !r.message.success) {
				frappe.msgprint({
					title: __("Proxy Error"),
					message: r.message.message,
					indicator: "red",
				});
			}
		},
	});
}

function _disable_proxy(frm) {
	frappe.call({
		method: "biometric_integration.api.disable_proxy",
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
