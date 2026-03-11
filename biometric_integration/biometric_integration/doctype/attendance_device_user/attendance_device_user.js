// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Device User", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Sync to All Devices"), () => {
			frappe.confirm(
				__("Queue Enroll User commands for all this user's assigned devices (where enrollment data exists)?"),
				() => {
					frappe.call({
						method: "biometric_integration.api.enqueue_user_enrollments",
						args: { user_id: frm.doc.name },
						callback(r) {
							if (r.message) frappe.msgprint(r.message);
						},
					});
				}
			);
		}, __("Commands"));

		frm.add_custom_button(__("Remove from All Devices"), () => {
			frappe.confirm(
				__("Queue Delete User commands for all this user's assigned devices?"),
				() => {
					frappe.call({
						method: "biometric_integration.api.enqueue_user_deletions",
						args: { user_id: frm.doc.name },
						callback(r) {
							if (r.message) frappe.msgprint(r.message);
						},
					});
				}
			);
		}, __("Commands"));

		frm.add_custom_button(__("View Commands"), () => {
			frappe.set_route("List", "Attendance Device Command", {
				attendance_device_user: frm.doc.name,
			});
		});
	},

	allow_in_all_devices(frm) {
		frm.toggle_display("devices", !frm.doc.allow_in_all_devices);
		frm.toggle_display("section_devices", !frm.doc.allow_in_all_devices);
	},
});
