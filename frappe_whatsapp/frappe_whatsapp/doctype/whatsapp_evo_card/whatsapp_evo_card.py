# Copyright (c) 2026, Shridhar Patil and contributors
# For license information, please see license.txt

from frappe.model.document import Document

class WhatsAppEvoCard(Document):
	def validate(self):
		self.buttons_summary = ", ".join(
			f"[{b.button_type}] {b.display_text}" for b in self.get("buttons") or []
		)
