# Copyright (c) 2026, Shridhar Patil and contributors
# For license information, please see license.txt

from frappe.model.document import Document

class WhatsAppEvoSection(Document):
	def validate(self):
		self.rows_summary = " | ".join(
			r.title + (f" — {r.description}" if r.description else "") for r in self.get("rows") or []
		)
