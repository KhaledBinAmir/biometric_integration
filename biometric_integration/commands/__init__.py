import click
import json
import frappe
from frappe.commands import pass_context

from biometric_integration.proxy.configurator import (
    enable_listener_logic,
    disable_listener_logic,
    get_status_logic,
    LISTENER_PORT_KEY,
    _get_site_config,
)


@click.group("biometric-listener")
def listener():
    """Manage the Nginx HTTP listener for biometric devices."""
    pass


@listener.command("enable")
@click.option("--port", type=int, default=None, help="Port to listen on (default: 8998)")
@pass_context
def enable(context, port):
    """Enable the Nginx HTTP listener for biometric devices on a given port."""
    try:
        site = context.sites[0]
    except IndexError:
        click.secho("Specify a site: bench --site SITENAME biometric-listener enable", fg="red")
        return

    try:
        frappe.connect(site=site)
        if not port:
            port = _get_site_config(LISTENER_PORT_KEY) or 8998
        success, message = enable_listener_logic(site, int(port))
        click.secho(message, fg="green" if success else "red")
    except Exception as exc:
        click.secho(f"Error on site {site}: {exc}", fg="red")
    finally:
        if frappe.local.db:
            frappe.destroy()


@listener.command("disable")
@pass_context
def disable(context):
    """Disable the Nginx HTTP listener."""
    try:
        site = context.sites[0]
    except IndexError:
        click.secho("Specify a site: bench --site SITENAME biometric-listener disable", fg="red")
        return

    try:
        frappe.connect(site=site)
        success, message = disable_listener_logic(site)
        click.secho(message, fg="green" if success else "red")
    except Exception as exc:
        click.secho(f"Error on site {site}: {exc}", fg="red")
    finally:
        if frappe.local.db:
            frappe.destroy()


@listener.command("status")
@pass_context
def status(context):
    """Show current listener status."""
    sites = context.sites or frappe.utils.get_sites()
    results = {}
    for s in sites:
        try:
            frappe.connect(site=s)
            results[s] = get_status_logic(s)
        except Exception as exc:
            results[s] = {"error": str(exc)}
        finally:
            if frappe.local.db:
                frappe.destroy()
    click.echo(json.dumps(results, indent=2))


commands = [listener]
