# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document


class WhatsAppAccount(Document):
	def validate(self):
		"""Auto-provision Evolution API webhook secret and URL."""
		if self.get("api_type") == "Evolution API":
			if not self.webhook_secret:
				self.webhook_secret = frappe.generate_hash(length=32)
			self.webhook_url = self.get_webhook_url()

	def on_update(self):
		"""Check there is only one default of each type."""
		self.there_must_be_only_one_default()

	def get_webhook_url(self):
		"""Build the Evolution API webhook URL including the secret token.

		The token is passed as a query string parameter and verified by
		frappe_whatsapp.utils.evolution_webhook.webhook before processing.

		The base host is taken from the site config key
		``evolution_api_webhook_url`` (if set) so the webhook points at a
		host that is reachable from inside the docker network, e.g.
		``http://frappe-frontend-1:8080``.
		"""
		if self.get("api_type") != "Evolution API":
			return None

		token = self.webhook_secret
		if self.name and token and self.is_dummy_password(token):
			token = self.get_password("webhook_secret")

		query = f"?token={token}" if token else ""
		path = f"/api/method/frappe_whatsapp.utils.evolution_webhook.webhook{query}"
		base = frappe.conf.get("evolution_api_webhook_url")
		if base:
			return f"{base.rstrip('/')}{path}"
		return frappe.utils.get_url(path)

	@frappe.whitelist()
	def test_connection(self):
		"""Test the Evolution API connection for this account.

		Returns a structured result so the form can render a clean status
		instead of the raw Evolution API JSON:
		``{state, connected, instance_name, raw}``.
		"""
		from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient

		if self.get("api_type") != "Evolution API":
			frappe.throw(_("Test Connection is only available for Evolution API accounts"))

		state = EvolutionAPIClient(self).get_connection_state()
		return self._extract_connection_state(state)

	def _extract_connection_state(self, state: dict) -> dict:
		instance = state.get("instance") if isinstance(state, dict) else {}
		if not isinstance(instance, dict):
			instance = {}
		conn = (instance.get("state") or "").lower()
		return {
			"state": conn,
			"connected": conn == "open",
			"instance_name": instance.get("instanceName") or self.get("instance_name"),
			"raw": state,
		}

	@frappe.whitelist()
	def get_qr_code(self):
		"""Fetch a QR code from Evolution API for this account.

		Calling ``GET /instance/connect`` also re-establishes the WebSocket
		connection when a stored session exists; it only returns a QR code
		when pairing (re-scan) is actually required.
		"""
		from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient

		if self.get("api_type") != "Evolution API":
			frappe.throw(_("Get QR Code is only available for Evolution API accounts"))

		result = EvolutionAPIClient(self).get_qr_code()
		resp = result if isinstance(result, dict) else {}
		qrcode = resp.get("qrcode") if isinstance(resp.get("qrcode"), dict) else resp
		return {
			"base64": (qrcode.get("base64") or ""),
			"pairingCode": (qrcode.get("pairingCode") or ""),
			"code": (qrcode.get("code") or ""),
			"raw": resp,
		}

	@frappe.whitelist()
	def reconnect_instance(self):
		"""Force-reconnect a disconnected instance.

		If the stored session is still valid the WebSocket re-opens on its
		own; otherwise a QR code is returned so the user can pair again.
		"""
		import time

		from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient

		if self.get("api_type") != "Evolution API":
			frappe.throw(_("Reconnect is only available for Evolution API accounts"))

		client = EvolutionAPIClient(self)
		current = self._extract_connection_state(client.get_connection_state())
		if current["connected"]:
			return current | {"qr": None}

		result = client.get_qr_code()  # reconnect attempt (or QR payload)

		time.sleep(3)
		after = self._extract_connection_state(client.get_connection_state())

		if after["connected"]:
			return after | {"qr": None}

		resp = result if isinstance(result, dict) else {}
		qrcode = resp.get("qrcode") if isinstance(resp.get("qrcode"), dict) else resp
		return after | {
			"qr": {
				"base64": (qrcode.get("base64") or ""),
				"pairingCode": (qrcode.get("pairingCode") or ""),
				"code": (qrcode.get("code") or ""),
			}
		}

	@frappe.whitelist()
	def configure_webhook(self):
		"""Configure the Evolution API webhook for this account."""
		from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient, redact_secrets

		if self.get("api_type") != "Evolution API":
			frappe.throw(_("Configure Webhook is only available for Evolution API accounts"))

		client = EvolutionAPIClient(self)
		url = self.webhook_url or self.get_webhook_url()
		result = client.set_webhook(url)
		self.reload()
		return {"webhook_url": url, "response": redact_secrets(result)}

	def there_must_be_only_one_default(self):
		"""If current WhatsApp Account is default, un-default all other accounts."""
		for field in ("is_default_incoming", "is_default_outgoing"):
			if not self.get(field):
				continue

			for whatsapp_account in frappe.get_all("WhatsApp Account", filters={field: 1}):
				if whatsapp_account.name == self.name:
					continue

				whatsapp_account = frappe.get_doc("WhatsApp Account", whatsapp_account.name)
				whatsapp_account.set(field, 0)
				whatsapp_account.save()

	@frappe.whitelist()
	def subscribe_app(self):
		"""Subscribe this app to webhooks for the WhatsApp Business Account.

		Required after phone number registration to receive incoming messages.
		Calls POST /{version}/{business_id}/subscribed_apps on the Graph API.
		"""
		for field in ("url", "version", "business_id"):
			if not self.get(field):
				frappe.throw(_("{0} is required to subscribe the app").format(
					frappe.bold(self.meta.get_label(field))
				))

		token = self.get_password("token")
		if not token:
			frappe.throw(_("Access token is required to subscribe the app"))

		endpoint = f"{self.url}/{self.version}/{self.business_id}/subscribed_apps"
		headers = {
			"authorization": f"Bearer {token}",
			"content-type": "application/json",
		}

		try:
			response = make_post_request(endpoint, headers=headers)
		except Exception as e:
			error_message = str(e)
			if frappe.flags.integration_request:
				err = frappe.flags.integration_request.json().get("error", {})
				if err:
					error_message = err.get("message") or err.get("Error") or error_message
			frappe.throw(_("Failed to subscribe app to webhooks: {0}").format(error_message))

		if not response.get("success"):
			frappe.throw(_("Subscription was not successful: {0}").format(frappe.as_json(response)))

		frappe.logger().info(
			f"WhatsApp app subscribed to webhooks for business_id={self.business_id}"
		)
		return response
