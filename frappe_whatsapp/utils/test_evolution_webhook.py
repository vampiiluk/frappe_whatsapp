# Copyright (c) 2026, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe_whatsapp.testing import IntegrationTestCase

from frappe_whatsapp.utils import evolution_webhook


class TestEvolutionWebhook(IntegrationTestCase):
	"""Evolution API webhook: token validation, incoming messages, status updates."""

	ACCOUNT = "Test Evo Webhook Account"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("WhatsApp Account", cls.ACCOUNT):
			account = frappe.get_doc({
				"doctype": "WhatsApp Account",
				"account_name": cls.ACCOUNT,
				"status": "Active",
				"api_type": "Evolution API",
				"base_url": "https://evo.example.com",
				"instance_name": "test-webhook-instance",
				"api_key": "test-api-key",
				"default_country_code": "254",
				"webhook_secret": "test-webhook-secret",
			})
			account.insert(ignore_permissions=True)
			from frappe.utils.password import set_encrypted_password
			set_encrypted_password("WhatsApp Account", account.name, "test-api-key", "api_key")
			set_encrypted_password("WhatsApp Account", account.name, "test-webhook-secret", "webhook_secret")
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

	def tearDown(self):
		for name in frappe.get_all("WhatsApp Message", filters={"whatsapp_account": self.ACCOUNT}, pluck="name"):
			frappe.delete_doc("WhatsApp Message", name, force=True)
		for name in frappe.get_all("WhatsApp Notification Log", filters={"template": "Evolution Webhook"}, pluck="name"):
			frappe.delete_doc("WhatsApp Notification Log", name, force=True)
		for name in frappe.get_all("WhatsApp Profiles", filters={"whatsapp_account": self.ACCOUNT}, pluck="name"):
			frappe.delete_doc("WhatsApp Profiles", name, force=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

	def _mock_request(self, raw_body=None):
		mock_request = MagicMock()
		mock_request.method = "POST"
		mock_request.args = {}
		mock_request.data = (raw_body or b"").encode("utf-8") if isinstance(raw_body, str) else (raw_body or b"")
		return mock_request

	def test_matches_account_by_token(self):
		frappe.form_dict.token = "test-webhook-secret"
		try:
			account = evolution_webhook._validate_webhook_request()
			self.assertEqual(account.name, self.ACCOUNT)
		finally:
			frappe.form_dict.clear()

	def test_unmatched_token_rejected(self):
		frappe.form_dict.token = "wrong-token"
		try:
			with self.assertRaises(frappe.PermissionError):
				evolution_webhook._validate_webhook_request()
		finally:
			frappe.form_dict.clear()

	def test_webhook_creates_incoming_text_message(self):
		payload = {
			"event": "MESSAGES_UPSERT",
			"data": {
				"key": {
					"remoteJid": "0725548065@s.whatsapp.net",
					"fromMe": False,
					"id": "EVO-WEBHOOK-TEXT-1",
				},
				"message": {"conversation": "Hello from Evolution"},
				"messageType": "conversation",
			},
		}
		frappe.local.form_dict = frappe._dict({})
		mock_request = self._mock_request(json.dumps(payload))
		mock_request.args = {"token": "test-webhook-secret"}

		with patch("frappe_whatsapp.utils.evolution_webhook.frappe.request", mock_request):
			result = evolution_webhook.webhook()

		self.assertTrue(result.get("ok"))
		msg = frappe.get_doc("WhatsApp Message", {"message_id": "EVO-WEBHOOK-TEXT-1"})
		self.assertEqual(msg.type, "Incoming")
		self.assertEqual(msg.message, "Hello from Evolution")
		self.assertEqual(msg.get("from"), "254725548065")
		self.assertEqual(msg.content_type, "text")
		self.assertEqual(msg.whatsapp_account, self.ACCOUNT)

	def test_webhook_creates_incoming_media_message_with_base64(self):
		import base64

		b64 = base64.b64encode(b"fake-image-bytes").decode("utf-8")
		payload = {
			"event": "MESSAGES_UPSERT",
			"data": {
				"key": {
					"remoteJid": "0725548066@s.whatsapp.net",
					"fromMe": False,
					"id": "EVO-WEBHOOK-IMG-1",
				},
				"message": {
					"imageMessage": {
						"mimetype": f"image/png;base64,{b64}",
						"caption": "Check this",
					}
				},
				"messageType": "imageMessage",
			},
		}
		frappe.local.form_dict = frappe._dict({})
		mock_request = self._mock_request(json.dumps(payload))
		mock_request.args = {"token": "test-webhook-secret"}

		with patch("frappe_whatsapp.utils.evolution_webhook.frappe.request", mock_request):
			evolution_webhook.webhook()

		msg = frappe.get_doc("WhatsApp Message", {"message_id": "EVO-WEBHOOK-IMG-1"})
		self.assertEqual(msg.content_type, "image")
		self.assertTrue(msg.attach)

	def test_webhook_accepts_all_incoming_even_without_existing_doc(self):
		"""Constraint preservation removed: new incoming messages are always created."""
		self.assertFalse(frappe.db.exists("WhatsApp Message", {"message_id": "EVO-WEBHOOK-NEW-1"}))

		payload = {
			"event": "MESSAGES_UPSERT",
			"data": {
				"key": {
					"remoteJid": "254725548067@s.whatsapp.net",
					"fromMe": False,
					"id": "EVO-WEBHOOK-NEW-1",
				},
				"message": {"conversation": "Fresh incoming"},
				"messageType": "conversation",
			},
		}
		frappe.local.form_dict = frappe._dict({})
		mock_request = self._mock_request(json.dumps(payload))
		mock_request.args = {"token": "test-webhook-secret"}

		with patch("frappe_whatsapp.utils.evolution_webhook.frappe.request", mock_request):
			evolution_webhook.webhook()

		self.assertTrue(frappe.db.exists("WhatsApp Message", {"message_id": "EVO-WEBHOOK-NEW-1"}))

	def test_webhook_updates_status_of_existing_message(self):
		msg = frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": "919900112299",
			"message": "Status update test",
			"message_id": "EVO-WEBHOOK-STATUS-1",
			"content_type": "text",
			"whatsapp_account": self.ACCOUNT,
		})
		msg.flags.ignore_validate = True
		msg.db_insert()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

		payload = {
			"event": "MESSAGES_UPDATE",
			"data": {
				"key": {
					"remoteJid": "919900112299@s.whatsapp.net",
					"fromMe": True,
					"id": "EVO-WEBHOOK-STATUS-1",
				},
				"status": 3,
				"messageType": "protocolMessage",
			},
		}
		frappe.local.form_dict = frappe._dict({})
		mock_request = self._mock_request(json.dumps(payload))
		mock_request.args = {"token": "test-webhook-secret"}

		with patch("frappe_whatsapp.utils.evolution_webhook.frappe.request", mock_request):
			evolution_webhook.webhook()

		msg.reload()
		self.assertEqual(msg.status, "delivered")

	def test_webhook_mirrors_outgoing_not_logged_in_frappe(self):
		payload = {
			"event": "SEND_MESSAGE",
			"data": {
				"key": {
					"remoteJid": "919900112302@s.whatsapp.net",
					"fromMe": True,
					"id": "EVO-WEBHOOK-OUT-1",
				},
				"message": {"conversation": "Sent from phone"},
				"messageType": "conversation",
			},
		}
		frappe.local.form_dict = frappe._dict({})
		mock_request = self._mock_request(json.dumps(payload))
		mock_request.args = {"token": "test-webhook-secret"}

		with patch("frappe_whatsapp.utils.evolution_webhook.frappe.request", mock_request):
			evolution_webhook.webhook()

		msg = frappe.get_doc("WhatsApp Message", {"message_id": "EVO-WEBHOOK-OUT-1"})
		self.assertEqual(msg.type, "Outgoing")
		self.assertEqual(msg.to, "919900112302")
