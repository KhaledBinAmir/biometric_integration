app_name = "biometric_integration"
app_title = "Biometric Integration"
app_publisher = "KhaledBinAmir"
app_description = "Real-time attendance integration for ZKTeco and EBKN biometric devices"
app_email = "khaledbinamir@gmail.com"
app_license = "mit"

# --- Routing: intercept /iclock/* and /ebkn before Frappe's template renderer ---
# This makes the app work on Frappe Cloud AND self-hosted without any Nginx dependency.
page_renderer = [
    "biometric_integration.renderers.ZKTecoRenderer",
    "biometric_integration.renderers.EBKNRenderer",
]

# --- Lifecycle ---
after_uninstall = "biometric_integration.utils.installation.after_uninstall"

# --- Scheduled Tasks ---
scheduler_events = {
    "daily": [
        "biometric_integration.services.command_processor.force_close_stale_commands",
    ],
}

# --- Log Retention ---
default_log_clearing_doctypes = {
    "Attendance Device Log": 30,
}

# --- Document Events ---
doc_events = {
    "Employee": {
        "on_update": "biometric_integration.services.user_sync.on_employee_update",
    },
}

# --- CLI (kept for advanced / self-hosted users) ---
console_scripts = ["biometric-listener=biometric_integration.commands.cli:main"]
