// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on('Attendance Device', {
	refresh(frm) {
		_init_timezone_field(frm);
		if (frm.is_new()) return;

		// --- Status indicator ---
		if (frm.doc.last_contact) {
			const user_dt = frappe.datetime.convert_to_user_tz(frm.doc.last_contact);
			const age_min = Math.round(
				(Date.now() - new Date(user_dt).getTime()) / 60000
			);
			const color = age_min < 5 ? 'green' : age_min < 60 ? 'yellow' : 'red';
			frm.dashboard.add_indicator(`Last contact: ${frappe.datetime.prettyDate(user_dt)}`, color);
		} else {
			frm.dashboard.add_indicator('Never contacted this server', 'grey');
		}

		if (frm.doc.has_pending_command) {
			frm.dashboard.add_indicator('Has Pending Commands', 'orange');
		}

		// --- Action buttons ---
		frm.add_custom_button(__('View Commands'), () => {
			frappe.set_route('List', 'Attendance Device Command', {
				attendance_device: frm.doc.name,
			});
		}, __('Commands'));

		frm.add_custom_button(__('Sync User List'), () => {
			const d = new frappe.ui.Dialog({
				title: __('Sync User List \u2014 {0}', [frm.doc.device_name || frm.doc.name]),
				fields: [{ fieldname: 'status_html', fieldtype: 'HTML' }],
			});
			d.get_field('status_html').$wrapper.html(
				`<div class="text-center py-3">
					<div class="spinner-border text-primary mb-2" role="status" style="width:2rem;height:2rem;"></div>
					<div class="text-muted">${__('Sending command to device\u2026')}</div>
				</div>`
			);
			d.show();
			frappe.call({
				method: 'biometric_integration.api.create_device_command',
				args: { device_id: frm.doc.name, command_type: 'Sync User List' },
				callback(r) {
					if (!r.message) {
						d.get_field('status_html').$wrapper.html(
							`<div class="text-center py-3 text-danger"><b>${__('Failed to queue command')}</b></div>`
						);
						return;
					}
					d.get_field('status_html').$wrapper.html(
						`<div class="text-center py-3">
							<div class="text-primary mb-1" style="font-size:2rem;">&#8635;</div>
							<b>${__('Sync User List command sent')}</b>
							<div class="text-muted small mt-2">${__('The device will upload its enrolled user IDs. Unknown users will be queued for biometric data fetch automatically.')}</div>
							<div class="text-muted small mt-1">${__('Check Device Logs for results.')}</div>
						</div>`
					);
					d.set_primary_action(__('View Device Logs'), () => {
						d.hide();
						frappe.set_route('List', 'Attendance Device Log', {
							attendance_device: frm.doc.name,
						});
					});
				},
			});
		}, __('Commands'));

		frm.add_custom_button(__('Restart Device'), () => {
			const d = new frappe.ui.Dialog({
				title: __('Restart Device \u2014 {0}', [frm.doc.device_name || frm.doc.name]),
				fields: [{ fieldname: 'status_html', fieldtype: 'HTML' }],
			});
			d.get_field('status_html').$wrapper.html(
				`<div class="text-center py-3">
					<div class="spinner-border text-warning mb-2" role="status" style="width:2rem;height:2rem;"></div>
					<div class="text-muted">${__('Sending restart command\u2026')}</div>
				</div>`
			);
			d.show();
			frappe.call({
				method: 'biometric_integration.api.create_device_command',
				args: { device_id: frm.doc.name, command_type: 'Restart Device' },
				callback(r) {
					if (!r.message) {
						d.get_field('status_html').$wrapper.html(
							`<div class="text-center py-3 text-danger"><b>${__('Failed to queue command')}</b></div>`
						);
						return;
					}
					d.get_field('status_html').$wrapper.html(
						`<div class="text-center py-3">
							<div class="text-success mb-1" style="font-size:2.5rem;">&#10003;</div>
							<b>${__('Restart command sent')}</b>
							<div class="text-muted small mt-1">${__('The device will restart in a few seconds.')}</div>
						</div>`
					);
					setTimeout(() => d.hide(), 2500);
				},
			});
		}, __('Commands'));

		frm.add_custom_button(__('Unlock Door'), () => {
			const d = new frappe.ui.Dialog({
				title: __('Unlock Door \u2014 {0}', [frm.doc.device_name || frm.doc.name]),
				fields: [{ fieldname: 'status_html', fieldtype: 'HTML' }],
			});
			d.get_field('status_html').$wrapper.html(
				`<div class="text-center py-3">
					<div class="spinner-border text-primary mb-2" role="status" style="width:2rem;height:2rem;"></div>
					<div class="text-muted">${__('Sending command to device\u2026')}</div>
				</div>`
			);
			d.show();
			frappe.call({
				method: 'biometric_integration.api.create_device_command',
				args: { device_id: frm.doc.name, command_type: 'Unlock Door' },
				callback(r) {
					if (!r.message) {
						d.get_field('status_html').$wrapper.html(
							`<div class="text-center py-3 text-danger"><b>${__('Failed to queue command')}</b></div>`
						);
						return;
					}
					_poll_door_unlock(d, r.message, 0);
				},
			});
		}, __('Commands'));

		frm.add_custom_button(__('View Device Logs'), () => {
			frappe.set_route('List', 'Attendance Device Log', {
				attendance_device: frm.doc.name,
			});
		});
	},

	brand(frm) {
		const hints = {
			ZKTeco: 'Enter the Serial Number (SN) shown in the device menu under System Info',
			EBKN: 'Enter the Device ID shown in the EBKN device settings',
		};
		frm.set_df_property('serial', 'description', hints[frm.doc.brand] || '');
		_apply_tz_field_state(frm);
	},
});

// ---------------------------------------------------------------------------
// Timezone field helpers
// ---------------------------------------------------------------------------

/**
 * Load IANA timezone list and apply read-only / default logic for device_timezone.
 * Called on every refresh so the state stays in sync with Attendance Integration Settings.
 */
function _init_timezone_field(frm) {
	const _apply = () => _apply_tz_field_state(frm);

	const _set_tz_options = (timezones) => {
		// For Autocomplete fieldtype, set_data() updates the awesomplete suggestion list
		const field = frm.fields_dict.device_timezone;
		if (field && field.set_data) {
			field.set_data(timezones);
		}
	};

	if (frappe.all_timezones) {
		_set_tz_options(frappe.all_timezones);
		_apply();
		return;
	}
	// Fetch timezone list (same method used by User doctype)
	frappe.call({
		method: 'frappe.core.doctype.user.user.get_timezones',
		callback(r) {
			if (r.message && r.message.timezones) {
				frappe.all_timezones = r.message.timezones;
				_set_tz_options(frappe.all_timezones);
			}
			_apply();
		},
	});
}

/**
 * Set device_timezone read-only state + description based on:
 *   - Brand (EBKN = always read-only, always UTC)
 *   - Attendance Integration Settings.push_timezone_to_device
 *     ON  -> editable, default = site timezone
 *     OFF -> read-only (shows whatever value the device last reported)
 */
function _apply_tz_field_state(frm) {
	if (frm.doc.brand === 'EBKN') {
		frm.set_df_property('device_timezone', 'read_only', 1);
		frm.set_df_property('device_timezone', 'description',
			__('EBKN devices always send timestamps in UTC. This field is read-only and informational.'));
		return;
	}

	// ZKTeco: behaviour depends on the global push setting
	frappe.call({
		method: 'biometric_integration.api.get_device_form_settings',
		callback(r) {
			const push = r.message && r.message.push_timezone_to_device;
			if (push) {
				frm.set_df_property('device_timezone', 'read_only', 0);
				frm.set_df_property('device_timezone', 'description',
					__("Timezone pushed to this device on every handshake. ATTLOG timestamps are interpreted in this timezone. Leave blank to use the site timezone."));
				// Auto-populate with site timezone when field is empty
				if (!frm.doc.device_timezone) {
					const site_tz = frappe.sys_defaults && frappe.sys_defaults.time_zone;
					if (site_tz) {
						frm.set_value('device_timezone', site_tz);
					}
				}
			} else {
				frm.set_df_property('device_timezone', 'read_only', 1);
				frm.set_df_property('device_timezone', 'description',
					__("Read-only. Enable 'Push Timezone to Device' in Attendance Integration Settings to configure this field."));
			}
		},
	});
}

// ---------------------------------------------------------------------------
// Door unlock dialog + polling
// ---------------------------------------------------------------------------

/**
 * Poll a door-unlock command every 2 seconds, up to 30 seconds.
 * Updates the dialog with the result and closes it automatically on success.
 */
function _poll_door_unlock(dialog, cmd_name, attempts) {
	const MAX_ATTEMPTS = 15; // 15 x 2s = 30s
	if (attempts >= MAX_ATTEMPTS) {
		dialog.get_field('status_html').$wrapper.html(
			`<div class="text-center py-3">
				<div class="text-warning mb-1" style="font-size:2rem;">&#9888;</div>
				<b>${__('No response from device within 30 seconds')}</b>
				<div class="text-muted small mt-1">${__('Check the device is online and has no pending commands.')}</div>
			</div>`
		);
		return;
	}
	setTimeout(() => {
		frappe.call({
			method: 'biometric_integration.api.get_command_status',
			args: { cmd_name },
			callback(r) {
				if (!r.message) return;
				const { status } = r.message;
				if (status === 'Success') {
					dialog.get_field('status_html').$wrapper.html(
						`<div class="text-center py-3">
							<div class="text-success mb-1" style="font-size:2.5rem;">&#10003;</div>
							<b>${__('Door unlocked successfully')}</b>
						</div>`
					);
					setTimeout(() => dialog.hide(), 1500);
				} else if (status === 'Failed') {
					dialog.get_field('status_html').$wrapper.html(
						`<div class="text-center py-3">
							<div class="text-danger mb-1" style="font-size:2.5rem;">&#10007;</div>
							<b>${__('Door unlock failed')}</b>
							<div class="text-muted small mt-1">${__('Check Attendance Device Log for details.')}</div>
						</div>`
					);
				} else {
					_poll_door_unlock(dialog, cmd_name, attempts + 1);
				}
			},
		});
	}, 2000);
}
