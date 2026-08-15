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
	frappe.db.commit()
