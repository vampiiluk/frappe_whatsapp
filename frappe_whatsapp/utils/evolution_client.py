"""Evolution API client for frappe_whatsapp.

Ported from the Frappe-WhatsApp-Evo app (https://github.com/Cecypo/Frappe-WhatsApp-Evo)
and adapted to use the ``WhatsApp Account`` doctype as the connection record
instead of ``Evo Line``. This keeps Meta Cloud API and Evolution API accounts
side by side (multi-provider coexistence).
"""
import re

import frappe
import requests
from frappe import _


def is_evolution_account(account) -> bool:
	"""Whether the given WhatsApp Account doc uses the Evolution API."""
	return bool(account) and account.get("api_type") == "Evolution API"


def get_client(account):
	"""Return an API client for the given WhatsApp Account.

	Returns an :class:`EvolutionAPIClient` for Evolution API accounts and
	``None`` for Meta Cloud API accounts (the Meta path uses direct HTTP
	calls and is left unchanged).
	"""
	if is_evolution_account(account):
		return EvolutionAPIClient(account)
	return None


def normalize_phone(number: str, default_country_code: str | None = None) -> str:
	if not number:
		frappe.throw(_("Phone number is required"))

	raw = str(number).strip()
	if "@" in raw:
		raw = raw.split("@", 1)[0]

	digits = re.sub(r"\D", "", raw)
	if not digits:
		frappe.throw(_("Phone number must contain digits"))

	if raw.startswith("+"):
		return digits

	if digits.startswith("00"):
		return digits[2:]

	default_country_code = re.sub(r"\D", "", default_country_code or "")
	if default_country_code and digits.startswith("0"):
		return f"{default_country_code}{digits[1:]}"

	return digits


def get_password_value(doc, fieldname: str) -> str | None:
	try:
		return doc.get_password(fieldname)
	except Exception:
		return doc.get(fieldname)


def get_message_id(response: dict | None) -> str | None:
	if not isinstance(response, dict):
		return None

	key = response.get("key")
	if isinstance(key, dict) and key.get("id"):
		return key.get("id")

	return response.get("messageId") or response.get("id")


def redact_secrets(value):
	if isinstance(value, dict):
		return {
			key: "*****" if key.lower() in {"apikey", "api_key", "authorization", "token"} else redact_secrets(item)
			for key, item in value.items()
		}
	if isinstance(value, list):
		return [redact_secrets(item) for item in value]
	return value


def strip_data_uri(value: str | None) -> str | None:
	"""Strip a ``data:<mime>;base64,`` prefix from media content.

	Evolution API accepts raw base64 in ``media``/``audio`` fields but
	rejects RFC 2397 ``data:`` URIs, so normalize them before sending.
	"""
	if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
		return value.split(";base64,", 1)[1]
	return value


def is_base64(value: str | None) -> bool:
	"""Whether a media value is already raw base64 (non-empty, decodable).

	Values that look like URLs, file paths or data URIs are rejected; a
	raw base64 string may legitimately start with ``/`` (e.g. JPEG data).
	"""
	if not isinstance(value, str) or not value:
		return False
	if value.startswith("data:") or "://" in value or value.startswith("./"):
		return False
	if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
		return False
	import base64 as _base64

	try:
		decoded = _base64.b64decode(value.replace("\n", "").replace("\r", ""), validate=True)
	except Exception:
		return False
	return bool(decoded)


def media_to_base64(media: str | None) -> str | None:
	"""Resolve an attach value to raw base64 for the Evolution API.

	The Evolution API container cannot reach ``http://localhost`` and cannot
	authenticate to read Frappe private files, so URL-based media sends fail
	with ``Owned media must be a url or base64``. Reading the file content
	from the Frappe File doctype and sending raw base64 is the reliable path.
	"""
	if not media:
		return None
	if is_base64(media):
		return media

	import base64 as _base64
	import os

	file_url = strip_data_uri(media)
	if file_url and file_url.startswith("http"):
		# e.g. http://localhost/private/files/foo.pdf -> /private/files/foo.pdf
		from urllib.parse import urlsplit

		file_url = urlsplit(file_url).path

	file_path = None
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
		if isinstance(content, bytes) and content:
			return _base64.b64encode(content).decode("ascii")
	except Exception:
		pass

	if not file_url.startswith("/"):
		file_url = f"/{file_url}"

	local = file_url.split("/private/files/", 1)
	if len(local) == 2:
		file_path = frappe.get_site_path("private", "files", local[1])
	else:
		file_path = frappe.get_site_path("public", "files", file_url.split("?")[0].split("/")[-1])

	if file_path and os.path.isfile(file_path):
		with open(file_path, "rb") as fh:
			return _base64.b64encode(fh.read()).decode("ascii")

	return None


class EvolutionAPIClient:
	def __init__(self, account):
		self.settings = account
		self.base_url = (account.base_url or "").strip().rstrip("/")
		self.instance_identifier = (account.instance_name or "").strip()
		self.instance_name = self.instance_identifier
		self.api_key = get_password_value(account, "api_key")
		self.timeout = int(account.timeout or 30)

		if not self.base_url:
			frappe.throw(_("Evolution API Base URL is required"))
		if not self.instance_identifier:
			frappe.throw(_("Evolution API Instance Name is required"))
		if not self.api_key:
			frappe.throw(_("Evolution API Key is required"))

	def request(self, method: str, path: str, payload: dict | None = None) -> dict:
		path = path.lstrip("/")
		url = f"{self.base_url}/{path}"
		headers = {
			"Content-Type": "application/json",
			"apikey": self.api_key,
		}

		try:
			response = requests.request(method, url, json=payload, headers=headers, timeout=self.timeout)
		except requests.RequestException as exc:
			frappe.log_error(message=str(exc), title="Evolution API Request Error")
			frappe.throw(_("Evolution API request failed: {0}").format(str(exc)))

		response_text = response.text or ""
		try:
			data = response.json() if response_text else {}
		except ValueError:
			data = {"response_text": response_text}

		if response.status_code >= 400:
			frappe.log_error(
				message=frappe.as_json(
					{
						"url": url,
						"status_code": response.status_code,
						"response": data,
					}
				),
				title="Evolution API HTTP Error",
			)
			detail = data.get("message") or data.get("error") or data.get("response_text") or response_text
			message = _("Evolution API returned HTTP {0} for /{1}").format(response.status_code, path)
			if detail:
				message = f"{message}<br><pre>{frappe.utils.escape_html(str(detail)[:1000])}</pre>"
			frappe.throw(message)

		return data

	def request_or_error(self, method: str, path: str, payload: dict | None = None) -> tuple[dict | None, str | None]:
		try:
			return self.request(method, path, payload), None
		except Exception as exc:
			return None, str(exc)

	def fetch_instances(self) -> dict:
		return self.request("GET", "/instance/fetchInstances")

	def get_current_webhook(self) -> dict:
		return self.request("GET", f"/webhook/find/{self.get_route_instance_name()}")

	def get_route_instance_name(self) -> str:
		identifier = self.instance_identifier
		instances = self.fetch_instances()
		if not isinstance(instances, list):
			return identifier

		for row in instances:
			instance = row.get("instance") if isinstance(row, dict) and isinstance(row.get("instance"), dict) else row
			if not isinstance(instance, dict):
				continue

			names = [instance.get("name"), instance.get("instanceName")]
			identifiers = [
				instance.get("name"),
				instance.get("instanceName"),
				instance.get("id"),
				instance.get("instanceId"),
				instance.get("token"),
			]

			if identifier in [value for value in identifiers if value]:
				return next((value for value in names if value), identifier)

		return identifier

	def get_connection_state(self) -> dict:
		return self.request("GET", f"/instance/connectionState/{self.get_route_instance_name()}")

	def get_qr_code(self) -> dict:
		return self.request("GET", f"/instance/connect/{self.get_route_instance_name()}")

	def send_text(self, to: str, message: str, delay: int | None = None, link_preview: bool = True, quoted: dict | None = None) -> dict:
		if not message:
			frappe.throw(_("Message is required"))

		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"text": message,
			"linkPreview": bool(link_preview),
		}
		if delay is not None:
			payload["delay"] = int(delay)
		if quoted:
			payload["quoted"] = quoted

		return self.request("POST", f"/message/sendText/{self.get_route_instance_name()}", payload)

	def send_media(
		self,
		to: str,
		media: str,
		mediatype: str,
		mimetype: str,
		filename: str,
		caption: str | None = None,
		delay: int | None = None,
		quoted: dict | None = None,
	) -> dict:
		if not media:
			frappe.throw(_("Media URL or base64 content is required"))
		if mediatype not in {"image", "video", "document"}:
			frappe.throw(_("Media Type must be image, video, or document"))

		media = strip_data_uri(media)
		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"mediatype": mediatype,
			"mimetype": mimetype,
			"caption": caption or "",
			"media": media,
			"fileName": filename,
		}
		if delay is not None:
			payload["delay"] = int(delay)
		if quoted:
			payload["quoted"] = quoted

		return self.request("POST", f"/message/sendMedia/{self.get_route_instance_name()}", payload)

	def send_audio(self, to: str, audio: str, delay: int | None = None) -> dict:
		"""Send an audio message. ``audio`` must be base64-encoded audio data."""
		if not audio:
			frappe.throw(_("Audio content is required"))

		audio = strip_data_uri(audio)
		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"audio": audio,
		}
		if delay is not None:
			payload["delay"] = int(delay)

		return self.request("POST", f"/message/sendWhatsAppAudio/{self.get_route_instance_name()}", payload)

	def get_base64_from_media_message(self, key: dict, message: dict) -> dict:
		"""Download a WhatsApp media message as base64.

		``key`` and ``message`` mirror the `key`/`message` fields of the
		Evolution webhook payload (or of a ``chat/findMessages`` record).
		Returns ``{mediaType, fileName, mimetype, base64, ...}``.
		"""
		if not isinstance(key, dict) or not isinstance(message, dict):
			frappe.throw(_("media key and message are required"))

		payload = {"message": {"key": key, "message": message}}
		return self.request(
			"POST",
			f"/chat/getBase64FromMediaMessage/{self.get_route_instance_name()}",
			payload,
		)

	def find_messages(self, remote_jid: str, limit: int = 40) -> dict:
		"""Return the most recent messages for a chat, matching Evolution's
		``chat/findMessages`` result shape (``messages.records``)."""
		return self.request(
			"POST",
			f"/chat/findMessages/{self.get_route_instance_name()}",
			{"where": {"remoteJid": remote_jid}, "page": 1, "offset": limit},
		)

	def mark_message_as_read(self, remote_jid: str, message_id: str) -> dict:
		remote_jid = self._to_full_jid(remote_jid)
		payload = {
			"readMessages": [
				{"remoteJid": remote_jid, "fromMe": False, "id": message_id}
			]
		}
		return self.request("POST", f"/chat/markMessageAsRead/{self.get_route_instance_name()}", payload)

	def send_buttons(self, to: str, buttons: list[dict], title: str | None = None, description: str | None = None, footer: str | None = None, delay: int | None = None, quoted: dict | None = None) -> dict:
		"""Send a buttons (quick-reply) message via Evolution API."""
		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"buttons": buttons,
		}
		if title:
			payload["title"] = title
		if description:
			payload["description"] = description
		if footer:
			payload["footer"] = footer
		if delay is not None:
			payload["delay"] = int(delay)
		if quoted:
			payload["quoted"] = quoted
		return self.request("POST", f"/message/sendButtons/{self.get_route_instance_name()}", payload)

	def send_list(self, to: str, sections: list[dict], button_text: str | None = None, title: str | None = None, description: str | None = None, footer: str | None = None, delay: int | None = None, quoted: dict | None = None) -> dict:
		"""Send a list message via Evolution API."""
		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"sections": sections,
		}
		if button_text:
			payload["buttonText"] = button_text
		if title:
			payload["title"] = title
		if description:
			payload["description"] = description
		if footer is not None:
			payload["footerText"] = footer
		if delay is not None:
			payload["delay"] = int(delay)
		if quoted:
			payload["quoted"] = quoted
		return self.request("POST", f"/message/sendList/{self.get_route_instance_name()}", payload)

	def send_carousel(self, to: str, cards: list[dict], body: str | None = None, title: str | None = None, description: str | None = None, footer: str | None = None, delay: int | None = None, quoted: dict | None = None) -> dict:
		"""Send a carousel message via Evolution API."""
		payload = {
			"number": normalize_phone(to, self.settings.default_country_code),
			"cards": cards,
		}
		if body:
			payload["body"] = body
		if title:
			payload["title"] = title
		if description:
			payload["description"] = description
		if footer:
			payload["footer"] = footer
		if delay is not None:
			payload["delay"] = int(delay)
		if quoted:
			payload["quoted"] = quoted
		return self.request("POST", f"/message/sendCarousel/{self.get_route_instance_name()}", payload)

	def _to_full_jid(self, remote_jid: str) -> str:
		"""Return a routable WhatsApp JID for a bare phone number.

		Baileys sends receipts by putting the given ``remoteJid`` directly in
		the receipt ``to`` attribute; a bare number without ``@s.whatsapp.net``
		cannot be routed, so the read receipt silently disappears. Messages are
		stored at the ``@lid`` address when available, otherwise the plain
		phone JID is used.
		"""
		jid = str(remote_jid or "")
		if "@" not in jid:
			# Try to map the phone number to its stored LID/PN address.
			res = self.find_messages(remote_jid=jid)
			if res:
				key = (res.get("messages") or {}).get("records") or []
				for record in key:
					remote = ((record or {}).get("key") or {}).get("remoteJid")
					if remote:
						return remote
			return f"{jid}@s.whatsapp.net"
		return jid

	def set_webhook(self, url: str, events: list[str] | None = None, webhook_base64: bool = True) -> dict:
		route = f"/webhook/set/{self.get_route_instance_name()}"
		events = events or ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "SEND_MESSAGE", "CONNECTION_UPDATE"]

		# 1. Check if already configured correctly
		try:
			current = self.get_current_webhook()
			if isinstance(current, dict) and current.get("url") == url and current.get("enabled"):
				current_events = current.get("events", [])
				if all(e in current_events for e in events):
					return current
		except Exception:
			pass

		# 2. Try various payloads
		payloads = self.get_webhook_payloads(url, events, webhook_base64)
		errors = []
		for payload in payloads:
			try:
				result = self.request("POST", route, payload)
				if result is not None:
					return result
			except Exception as exc:
				errors.append(str(exc))

		# 3. Final check (maybe a "failed" request actually worked)
		try:
			current = self.get_current_webhook()
			if isinstance(current, dict) and current.get("url") == url:
				return current
		except Exception:
			pass

		frappe.throw(_("Evolution API rejected all supported webhook payloads:<br>{0}").format("<br>".join(errors)))

	def get_webhook_payloads(
		self, url: str, events: list[str] | None = None, webhook_base64: bool = True
	) -> list[dict]:
		events = events or ["MESSAGES_UPSERT", "MESSAGES_UPDATE", "SEND_MESSAGE", "CONNECTION_UPDATE"]
		return [
			{
				"enabled": True,
				"url": url,
				"webhookByEvents": True,
				"webhookBase64": False,
				"events": events,
			},
			{
				"enabled": True,
				"url": url,
				"webhookByEvents": False,
				"webhookBase64": bool(webhook_base64),
				"events": events,
			},
			{
				"enabled": True,
				"url": url,
				"webhookByEvents": False,
				"webhookBase64": bool(webhook_base64),
				"events": events,
			},
			{
				"enabled": True,
				"url": url,
				"webhook_by_events": False,
				"webhook_base64": bool(webhook_base64),
				"events": events,
			},
			{
				"enabled": True,
				"url": url,
				"webhookByEvents": True,
				"webhookBase64": False,
				"events": events,
			},
			{
				"url": url,
				"enabled": True,
				"events": events,
			},
			{
				"url": url,
				"events": events,
			},
			{
				"webhook": {
					"enabled": True,
					"url": url,
					"webhookByEvents": False,
					"webhookBase64": bool(webhook_base64),
					"events": events,
				}
			},
		]
