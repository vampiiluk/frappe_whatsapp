# Copyright (c) 2026, Frappe Whatsapp contributors
# For license information, please see license.txt


import frappe


def after_install():
	from frappe.desk.doctype.desktop_icon.desktop_icon import create_desktop_icons_from_workspace
	from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import (
		create_workspace_sidebar_for_workspaces,
	)

	create_workspace_sidebar_for_workspaces()
	create_desktop_icons_from_workspace()
	setup_desktop_icon()
	frappe.db.commit()


def setup_desktop_icon():
	"""Link the Frappe Whatsapp Desktop Icon record to the frappe_whatsapp app so the
	desk renders the app icon (assets/frappe_whatsapp/icons/desktop_icons/...) instead
	of falling back to the label's initial letter."""
	try:
		if frappe.db.exists("Desktop Icon", "Frappe Whatsapp"):
			if frappe.db.get_value("Desktop Icon", "Frappe Whatsapp", "app") != "frappe_whatsapp":
				frappe.db.set_value(
					"Desktop Icon", "Frappe Whatsapp", "app", "frappe_whatsapp", update_modified=False
				)
		else:
			frappe.get_doc(
				{
					"doctype": "Desktop Icon",
					"label": "Frappe Whatsapp",
					"icon_type": "Link",
					"link_type": "Workspace Sidebar",
					"link_to": "Frappe Whatsapp",
					"icon": "message-square",
					"app": "frappe_whatsapp",
					"standard": 0,
					"hidden": 0,
					"restrict_removal": 0,
					"bg_color": "gray",
				}
			).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Desktop Icon Setup Error", message=frappe.get_traceback())
