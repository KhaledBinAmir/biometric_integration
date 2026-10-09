// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Integration Settings", {
	refresh(frm) {
		_load_endpoint_urls(frm);
		_check_proxy_compatibility(frm);
		frm.add_custom_button(__("Sync Employees"), () => _sync_employees(frm));
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

// Preview first, then sync: show who would be added to which device and only
// queue the commands once the operator confirms.
function _sync_employees(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the settings first."));
		return;
	}
	frappe.call({
		method: "biometric_integration.api.sync_employees",
		args: { dry_run: 1 },
		freeze: true,
		callback(r) {
			const s = r.message;
			if (!s) return;
			if (s.mode === "Disabled") {
				frappe.msgprint(__("Employee Sync Mode is Disabled: nothing is assigned automatically."));
				return;
			}
			const html = _sync_summary_html(s);
			if (!s.people.length) {
				frappe.msgprint({ title: __("Employees are in sync"), message: html });
				return;
			}
			frappe.confirm(html + `<p>${__("Add them now?")}</p>`, () => {
				frappe.realtime.off("biometric_employee_sync_done");
				frappe.realtime.on("biometric_employee_sync_done", (done) => {
					frappe.realtime.off("biometric_employee_sync_done");
					frappe.msgprint({ title: __("Employees synced"), message: _sync_summary_html(done) });
				});
				frappe.call({
					method: "biometric_integration.api.sync_employees",
					args: { dry_run: 0 },
					callback() {
						frappe.show_alert({
							message: __("Sync started in the background. You will see the result here."),
							indicator: "blue",
						});
					},
				});
			});
		},
	});
}

function _sync_summary_html(s) {
	const esc = frappe.utils.escape_html;
	const names = s.device_names || {};
	const per_device = Object.entries(s.added || {})
		.map(([sn, n]) => `<li>${esc(names[sn] || sn)}: ${n}</li>`)
		.join("");
	let html = `<p>${__("{0} employees checked ({1}).", [s.employees, esc(s.mode)])}</p>`;
	if (s.people.length) {
		const verb = s.dry_run ? __("To add") : __("Added");
		html += `<p><b>${verb}: ${s.people.length}</b></p><ul>${per_device}</ul>`;
		html += `<p class="text-muted small">${s.people.map(esc).join(", ")}</p>`;
	}
	if (s.conflicts && s.conflicts.length) {
		html += `<p class="text-danger">${__("Not synced:")}</p><ul>${s.conflicts
			.map((c) => `<li>${esc(c)}</li>`)
			.join("")}</ul>`;
	}
	if (s.no_pin && s.no_pin.length) {
		html += `<p>${__("No Attendance Device ID, so not on any device:")} ${s.no_pin.map(esc).join(", ")}</p>`;
	}
	return html;
}

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

