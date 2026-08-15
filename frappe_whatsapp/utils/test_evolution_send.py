# Copyright (c) 2026, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe_whatsapp.testing import IntegrationTestCase


class TestEvolutionSendRouting(IntegrationTestCase):
	"""Sending via WhatsApp Message routes to the Evolution API client."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._ensure_test_account()

	@classmethod
	def _ensure_test_account(cls):
		if not frappe.db.exists("WhatsApp Account", "Test Evo Send Account"):
			account = frappe.get_doc({
				"doctype": "WhatsApp Account",
				"account_name": "Test Evo Send Account",
				"status": "Active",
				"api_type": "Evolution API",
				"base_url": "https://evo.example.com",
				"instance_name": "test-send-instance",
				"api_key": "test-api-key",
				"default_country_code": "254",
				"webhook_secret": "test-webhook-secret",
				"is_default_incoming": 1,
				"is_default_outgoing": 1,
			})
			account.insert(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

	def setUp(self):
		frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
		frappe.db.set_value("WhatsApp Account", "Test Evo Send Account", {
			"is_default_outgoing": 1,
			"is_default_incoming": 1,
		})

	def tearDown(self):
		for name in frappe.get_all("WhatsApp Message", filters={"to": ["like", "9199%"]}, pluck="name"):
			frappe.delete_doc("WhatsApp Message", name, force=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

	@patch("frappe_whatsapp.utils.evolution_client.EvolutionAPIClient.send_text")
	def test_outgoing_text_message_sends_via_evolution(self, mock_send_text):
		mock_send_text.return_value = {"key": {"id": "EVO-SEND-1"}}

		doc = frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": "0725548065",
			"message": "Hello from Evolution",
			"message_type": "Manual",
			"content_type": "text",
			"whatsapp_account": "Test Evo Send Account",
		})
		doc.insert(ignore_permissions=True)

		self.assertEqual(doc.status, "Success")
		self.assertEqual(doc.message_id, "EVO-SEND-1")

		self.assertEqual(mock_send_text.call_args.kwargs["to"], "0725548065")
		self.assertEqual(mock_send_text.call_args.kwargs["message"], "Hello from Evolution")

	@patch("frappe_whatsapp.utils.evolution_client.EvolutionAPIClient.send_media")
	def test_outgoing_image_message_sends_via_evolution(self, mock_send_media):
		mock_send_media.return_value = {"key": {"id": "EVO-SEND-2"}}

		doc = frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": "+919900112299",
			"message": "Image caption",
			"message_type": "Manual",
			"content_type": "image",
			"attach": "https://example.com/image.jpg",
			"whatsapp_account": "Test Evo Send Account",
		})
		doc.insert(ignore_permissions=True)

		self.assertEqual(doc.status, "Success")
		self.assertEqual(doc.message_id, "EVO-SEND-2")
		kwargs = mock_send_media.call_args.kwargs
		self.assertEqual(kwargs["mediatype"], "image")
		self.assertEqual(kwargs["media"], "https://example.com/image.jpg")
		self.assertEqual(kwargs["caption"], "Image caption")

	@patch("frappe_whatsapp.utils.evolution_client.EvolutionAPIClient.send_text")
	def test_template_falls_back_to_plain_text(self, mock_send_text):
		"""Template sends fall back to plain text for Evolution API accounts."""
		mock_send_text.return_value = {"key": {"id": "EVO-SEND-3"}}

		if not frappe.db.exists("WhatsApp Templates", "test_evo_tmpl-en"):
			tmpl = frappe.get_doc({
				"doctype": "WhatsApp Templates",
				"template_name": "test_evo_tmpl",
				"actual_name": "test_evo_tmpl",
				"template": "Hello {{1}}, your order {{2}} is ready.",
				"sample_values": "name,order_no",
				"field_names": "name,order_no",
				"category": "TRANSACTIONAL",
				"language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
				"language_code": "en",
				"whatsapp_account": "Test Evo Send Account",
				"status": "APPROVED",
				"id": "test_evo_template_id",
			})
			tmpl.flags.ignore_validate = True
			tmpl.db_insert()
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

		doc = frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": "919900112300",
			"message_type": "Template",
			"content_type": "text",
			"template": "test_evo_tmpl-en",
			"body_param": json.dumps({"name": "Alice", "order_no": "SO-001"}),
			"whatsapp_account": "Test Evo Send Account",
		})
		doc.insert(ignore_permissions=True)

		self.assertEqual(doc.status, "Success")
		self.assertEqual(doc.message_id, "EVO-SEND-3")
		# Rendered plain text, not a Meta template payload
		self.assertEqual(
			mock_send_text.call_args.kwargs["message"],
			"Hello Alice, your order SO-001 is ready.",
		)
		self.assertEqual(doc.template_parameters, '["Alice", "SO-001"]')

	@patch("frappe_whatsapp.utils.evolution_client.EvolutionAPIClient.send_text")
	def test_meta_account_still_uses_meta_path(self, mock_send_text):
		"""Accounts without api_type=Evolution API must NOT use the Evolution client."""
		if not frappe.db.exists("WhatsApp Account", "Test Evo Meta Account"):
			account = frappe.get_doc({
				"doctype": "WhatsApp Account",
				"account_name": "Test Evo Meta Account",
				"status": "Active",
				"api_type": "Meta Cloud API",
				"url": "https://graph.facebook.com",
				"version": "v17.0",
				"phone_id": "meta_evo_test_phone",
				"business_id": "meta_evo_test_biz",
				"app_id": "meta_evo_test_app",
				"webhook_verify_token": "meta_evo_test_token",
			})
			account.insert(ignore_permissions=True)
			from frappe.utils.password import set_encrypted_password
			set_encrypted_password("WhatsApp Account", account.name, "meta_token", "token")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

		with patch(
			"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request"
		) as mock_post:
			mock_post.return_value = {"messages": [{"id": "wamid.meta_evo_path"}]}
			doc = frappe.get_doc({
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": "919900112301",
				"message": "Meta path",
				"message_type": "Manual",
				"content_type": "text",
				"whatsapp_account": "Test Evo Meta Account",
			})
			doc.insert(ignore_permissions=True)

		self.assertEqual(doc.status, "Success")
		self.assertEqual(doc.message_id, "wamid.meta_evo_path")
		mock_send_text.assert_not_called()
