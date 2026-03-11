// Copyright (c) 2026, Khaled Bin Amir
// SPDX-License-Identifier: MIT

frappe.ui.form.on("Attendance Device Command", {
	refresh(frm) {
		const color_map = {
			Pending: "orange",
			Success: "green",
			Failed: "red",
			Closed: "grey",
		};
		frm.dashboard.add_indicator(frm.doc.status, color_map[frm.doc.status] || "blue");

		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry"), () => {
				frappe.call({
					method: "frappe.client.set_value",
					args: {
						doctype: "Attendance Device Command",
						name: frm.doc.name,
						fieldname: { status: "Pending", no_of_attempts: 0 },
					},
					callback() {
						frm.reload_doc();
					},
				});
			}).addClass("btn-warning");
		}
	},
});
