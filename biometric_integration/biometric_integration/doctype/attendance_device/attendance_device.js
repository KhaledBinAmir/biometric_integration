// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Device", {
	refresh(frm) {
		if (frm.is_new()) return;

		// --- Status indicator ---
		if (frm.doc.last_contact) {
			const age_min = Math.round(
				(Date.now() - new Date(frm.doc.last_contact + " UTC").getTime()) / 60000
			);
			const color = age_min < 5 ? "green" : age_min < 60 ? "yellow" : "red";
			frm.dashboard.add_indicator(`Last contact: ${age_min} min ago`, color);
		} else {
			frm.dashboard.add_indicator("Never contacted this server", "grey");
		}

		if (frm.doc.has_pending_command) {
			frm.dashboard.add_indicator("Has Pending Commands", "orange");
		}

		// --- Action buttons ---
		frm.add_custom_button(__("View Commands"), () => {
			frappe.set_route("List", "Attendance Device Command", {
				attendance_device: frm.doc.name,
			});
		}, __("Commands"));

		frm.add_custom_button(__("Enroll All Users"), () => {
			frappe.confirm(
				__("Queue Enroll User commands for all users with existing enrollment data for this device's brand?"),
				() => {
					frappe.call({
						method: "biometric_integration.api.enqueue_all_enrollments",
						args: { device_id: frm.doc.name },
						callback(r) {
							if (r.message) frappe.msgprint(r.message);
						},
					});
				}
			);
		}, __("Commands"));

		frm.add_custom_button(__("View Device Logs"), () => {
			frappe.set_route("List", "Attendance Device Log", {
				attendance_device: frm.doc.name,
			});
		});
	},

	brand(frm) {
		const hints = {
			ZKTeco: "Enter the Serial Number (SN) shown in the device menu under System Info",
			EBKN: "Enter the Device ID shown in the EBKN device settings",
		};
		frm.set_df_property("serial", "description", hints[frm.doc.brand] || "");
	},
});
