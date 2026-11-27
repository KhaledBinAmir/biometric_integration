import frappe


def execute():
    """Separate legacy device names from linked Employee names.

    Before the device_user_name field existed, get_or_create_user_by_pin()
    stored the name reported by the device in employee_name. If that record is
    still unlinked, the value is the device name, not an Employee's name, so move
    it. Then make sure every record has a display_name for the title/link fields.
    """
    rows = frappe.get_all(
        "Attendance Device User",
        fields=[
            "name",
            "user_id",
            "employee",
            "employee_name",
            "device_user_name",
            "display_name",
        ],
    )
    for row in rows:
        updates = {}
        if not row.employee and row.employee_name and not row.device_user_name:
            updates["device_user_name"] = row.employee_name
            updates["employee_name"] = None
        if not row.display_name:
            updates["display_name"] = (
                row.device_user_name
                or updates.get("device_user_name")
                or row.employee_name
                or row.user_id
                or ""
            ).strip()
        if updates:
            frappe.db.set_value(
                "Attendance Device User", row.name, updates, update_modified=False
            )
    frappe.db.commit()
