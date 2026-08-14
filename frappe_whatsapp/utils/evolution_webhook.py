"""Evolution API webhook.

Receives callbacks from a self-hosted Evolution API server (event,
data.key.remoteJid format) and translates them into ``WhatsApp Message``
records, mirroring the Meta Cloud API webhook in ``frappe_whatsapp.utils.webhook``.

Unlike the Evo reference app, ALL incoming messages are accepted (the
"only update existing messages" constraint is intentionally removed) and
accounts coexist with Meta Cloud API accounts — the webhook is matched to
a ``WhatsApp Account`` with ``api_type == "Evolution API"`` via the secret
token appended to the webhook URL.
"""
import base64
import json
import time

import frappe
from frappe import _

from frappe_whatsapp.utils.evolution_client import (
	get_password_value,
	normalize_phone,
)

EVENT_STATUS_MAP = {
	0: "error",
	1: "pending",
	2: "sent",
	3: "delivered",
	4: "read",
	5: "played",
}

EVENT_STATUS_STRING_MAP = {
	"PENDING": "pending",
	"SERVER_ACK": "sent",
	"SENT": "sent",
	"DELIVERY_ACK": "delivered",
	"DELIVERED": "delivered",
	"READ": "read",
	"PLAYED": "played",
	"ERROR": "error",
	"FAILED": "failed",
}


def _as_json(value) -> str:
	if isinstance(value, str):
		return value
	return frappe.as_json(value)


def _extract_text(data: dict) -> str | None:
	message = data.get("message") if isinstance(data.get("message"), dict) else {}
	if message.get("conversation"):
		return message.get("conversation")

	for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
		value = message.get(key)
		if isinstance(value, dict):
			return value.get("text") or value.get("caption") or value.get("fileName")

	# Interactive responses (button / list / carousel / template taps).
	# Mirror the Meta webhook: store the button/row id (not the display
	# text) so chatbot routing via button_payload stays stable regardless
	# of label, emoji or language. Fall back to display text when the id
	# is not present in the payload.
	list_response = message.get("listResponseMessage")
	if isinstance(list_response, dict):
		title = list_response.get("title") or ""
		description = list_response.get("description") or ""
		return " - ".join(part for part in (title, description) if part) or None

	button_response = message.get("buttonsResponseMessage")
	if isinstance(button_response, dict):
		return (
			button_response.get("selectedButtonId")
			or button_response.get("selectedDisplayText")
			or None
		)

	template_response = message.get("templateButtonReplyMessage")
	if isinstance(template_response, dict):
		return (
			template_response.get("selectedId")
			or template_response.get("selectedDisplayText")
			or None
		)

	for key in ("interactiveResponseMessage", "interactiveMessage"):
		interactive = message.get(key)
		if isinstance(interactive, dict):
			native_flow = interactive.get("nativeFlowResponseMessage")
			if isinstance(native_flow, dict):
				params_json = native_flow.get("paramsJson")
				if isinstance(params_json, str):
					try:
						params = json.loads(params_json)
						text = (
							params.get("display_text")
							or params.get("displayText")
							or params.get("title")
							or params.get("text")
							or params.get("id")
						)
						if text:
							return str(text)
					except (ValueError, TypeError):
						pass
				elif isinstance(params_json, dict):
					text = (
						params_json.get("display_text")
						or params_json.get("displayText")
						or params_json.get("title")
						or params_json.get("text")
						or params_json.get("id")
					)
					if text:
						return str(text)
			body = interactive.get("body")
			if isinstance(body, dict) and body.get("text"):
				return body.get("text")

	return data.get("text") or data.get("messageText")


def _extract_reply_to(data: dict) -> str | None:
	message = data.get("message") if isinstance(data.get("message"), dict) else {}
	for value in message.values():
		if isinstance(value, dict):
			context = value.get("contextInfo")
			if isinstance(context, dict) and context.get("stanzaId"):
				return context.get("stanzaId")
	return None


def _extract_message_payload(payload: dict) -> dict:
	data = payload.get("data")
	if isinstance(data, list) and len(data) > 0:
		data = data[0]
	elif not isinstance(data, dict):
		data = payload

	key = data.get("key") if isinstance(data.get("key"), dict) else {}
	remote_jid = key.get("remoteJid") or data.get("remoteJid") or data.get("from")
	from_me = bool(key.get("fromMe") or data.get("fromMe"))
	message_id = key.get("id") or data.get("keyId") or data.get("id") or data.get("messageId")

	return {
		"data": data,
		"remote_jid": remote_jid,
		"from_me": from_me,
		"message_id": message_id,
		"text": _extract_text(data),
	}


def _find_account_by_webhook_token(token: str | None):
	if not token:
		return None

	account_names = frappe.get_all(
		"WhatsApp Account",
		filters={"api_type": "Evolution API"},
		pluck="name",
	)
	for name in account_names:
		account = frappe.get_doc("WhatsApp Account", name)
		secret = get_password_value(account, "webhook_secret")
		if secret and secret == token:
			return account
	return None


def _validate_webhook_request():
	token = None
	if hasattr(frappe, "request") and frappe.request:
		token = frappe.request.args.get("token")

	if not token:
		token = (
			frappe.form_dict.get("token")
			or frappe.get_request_header("X-Webhook-Secret")
			or frappe.get_request_header("X-Evolution-Webhook-Secret")
		)

	account = _find_account_by_webhook_token(token)
	if not account:
		frappe.throw(_("Invalid webhook token"), frappe.PermissionError)
	return account


def _map_content_type(raw_type: str) -> str:
	raw_type = raw_type or "text"
	lower = raw_type.lower()
	if "image" in lower:
		return "image"
	if "video" in lower:
		return "video"
	if "document" in lower:
		return "document"
	if "audio" in lower:
		return "audio"
	if "reaction" in lower:
		return "reaction"
	if lower in ("buttonsresponsemessage", "listresponsemessage", "templatebuttonreplymessage"):
		return "button"
	return "text"


def _extract_extension(mimetype: str) -> str:
	ext = (mimetype or "").split("/")[-1].split(";")[0]
	return ext if ext and len(ext) <= 10 else "bin"


def _attach_media(message_doc, data: dict, mapped_type: str, account=None):
	"""Attach media from the Evolution webhook payload.

	Download the real media bytes through the Evolution API and store them
	as a Frappe ``File`` so the message shows an actual attachment instead
	of a raw WhatsApp CDN (``mmg.whatsapp.net``) link. Falls back to
	storing the URL when the download is unavailable.
	"""
	message = data.get("message") if isinstance(data.get("message"), dict) else {}
	message_data = message.get(f"{mapped_type}Message") or {}
	mimetype = message_data.get("mimetype") or ""
	file_name = (
		message_data.get("fileName")
		or f"{frappe.generate_hash(length=10)}.{_extract_extension(mimetype)}"
	)

	if ";base64," in mimetype:
		try:
			b64 = mimetype.split(";base64,", 1)[1]
			content = base64.b64decode(b64)
			file_doc = frappe.get_doc({
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": "WhatsApp Message",
				"attached_to_name": message_doc.name,
				"attached_to_field": "attach",
				"content": content,
			}).save(ignore_permissions=True)
			message_doc.attach = file_doc.file_url
			message_doc.save(ignore_permissions=True)
			return
		except Exception:
			frappe.log_error(title="Failed to save Evolution webhook media")

	key = data.get("key") if isinstance(data.get("key"), dict) else {}
	media_message = message.get(f"{mapped_type}Message")

	if account and key and isinstance(media_message, dict) and media_message.get("url"):
		try:
			from frappe_whatsapp.utils.evolution_client import (
				EvolutionAPIClient,
				is_base64,
				is_evolution_account,
			)

			if is_evolution_account(account):
				client = EvolutionAPIClient(account)
				download = client.get_base64_from_media_message(key, message)
				media_b64 = (download or {}).get("base64") or ""
				media_name = (download or {}).get("fileName") or file_name
				media_mime = (download or {}).get("mimetype") or mimetype
				if is_base64(media_b64):
					content = base64.b64decode(media_b64, validate=True)
					file_doc = frappe.get_doc({
						"doctype": "File",
						"file_name": frappe.utils.safe_decode(
							frappe.utils.split_emails(media_name)[0]
						)[:100],
						"mimetype": media_mime,
						"is_private": 0,
						"attached_to_doctype": "WhatsApp Message",
						"attached_to_name": message_doc.name,
						"attached_to_field": "attach",
						"content": content,
					}).save(ignore_permissions=True)
					message_doc.attach = file_doc.file_url
					message_doc.save(ignore_permissions=True)
					return
		except Exception:
			frappe.log_error(title="Failed to download Evolution webhook media")

	media_url = message_data.get("url") or data.get("url")
	if media_url:
		try:
			short_name = frappe.utils.safe_decode(frappe.utils.split_emails(file_name)[0])
			file_doc = frappe.get_doc({
				"doctype": "File",
				"file_name": short_name[:100],
				"file_url": media_url,
				"is_private": 0,
				"attached_to_doctype": "WhatsApp Message",
				"attached_to_name": message_doc.name,
				"attached_to_field": "attach",
			}).save(ignore_permissions=True)
			message_doc.attach = file_doc.file_url
			message_doc.save(ignore_permissions=True)
		except Exception:
			frappe.log_error(title="Failed to link Evolution webhook media")


def _insert_whatsapp_message(account, extracted: dict, mapped_type: str, remote_number: str | None, direction: str):
	"""Create a WhatsApp Message record for an incoming/outgoing webhook message.

	All incoming messages are accepted (no constraint preservation).
	Outgoing messages not previously logged by Frappe are mirrored too,
	so the conversation log stays complete.
	"""
	data = extracted["data"]
	text = extracted["text"] or ""
	message_id = extracted["message_id"]

	if not message_id and not text:
		return {"ok": True, "message": "Ignored: no message payload"}

	doc_args = {
		"doctype": "WhatsApp Message",
		"type": direction,
		"message": text,
		"message_id": message_id,
		"content_type": mapped_type,
		"whatsapp_account": account.name,
	}
	if direction == "Incoming":
		doc_args["from"] = remote_number
	else:
		doc_args["to"] = remote_number

	if mapped_type == "reaction":
		reaction = data.get("reactionMessage") or {}
		doc_args["message"] = reaction.get("text") or text
		target = (reaction.get("key") or {}).get("id")
		if target:
			doc_args["reply_to_message_id"] = target
			doc_args["is_reply"] = 1
	else:
		reply_to = _extract_reply_to(data)
		if reply_to:
			doc_args["reply_to_message_id"] = reply_to
			doc_args["is_reply"] = 1

	doc = frappe.get_doc(doc_args)
	# Never re-send a message that originated outside Frappe.
	doc.flags.ignore_send = True
	doc.insert(ignore_permissions=True)

	if mapped_type in ("image", "video", "document", "audio"):
		_attach_media(doc, data, mapped_type, account)

	return {"ok": True, "message_log": doc.name}


def _find_existing_message(message_id: str, direction: str):
	"""Locate an existing WhatsApp Message record for an event's message id.

	Outgoing messages are sent from the Frappe form *inside* the form's
	``before_insert``; the doc is committed only when that request finishes.
	Evolution fires ``send.message`` concurrently, so a webhook request can
	take a REPEATABLE READ snapshot before the form doc is committed and
	miss it — creating a duplicate mirror. Retry with a fresh snapshot for
	a short window (only for outgoing events) before deciding a message is
	genuinely unknown.

	Multi-component template sends store all their message ids comma-joined
	in one record (e.g. ``"ID1, ID2, ID3"``), so an exact match on the
	single event id fails and would create duplicate mirrors. Match against
	the joined list too.

	Observed in production: the sending request can keep its transaction
	open for ~7-11s after the insert (the whole send — including the
	Evolution HTTP round-trips inside ``before_insert`` — runs inside that
	transaction), so the webhook retry window must comfortably exceed that
	or mirrors still slip through. 20 x 0.7s = 14s covers it.
	"""
	existing = frappe.db.exists("WhatsApp Message", {"message_id": message_id})
	if not existing:
		existing = frappe.db.exists("WhatsApp Message", {"message_id": ["like", f"%{message_id}%"]})
	if existing or direction != "Outgoing":
		return existing

	for _ in range(20):
		time.sleep(0.7)
		# End the current read snapshot so the next EXISTS sees new commits.
		frappe.db.commit()
		existing = frappe.db.exists("WhatsApp Message", {"message_id": message_id})
		if not existing:
			existing = frappe.db.exists("WhatsApp Message", {"message_id": ["like", f"%{message_id}%"]})
		if existing:
			break
	return existing


def _auto_reconnect(account, payload):
	"""Best-effort auto-reconnect when Evolution reports the instance closed.

	``CONNECTION_UPDATE`` events carry the new instance state; when it is no
	longer "open" we call ``GET /instance/connect`` which restores the stored
	WhatsApp session. Rate-limited per instance so a flapping connection does
	not produce a reconnect loop.
	"""
	from datetime import datetime

	from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient

	if account.get("api_type") != "Evolution API":
		return

	data = payload.get("data") or []
	row = data[0] if isinstance(data, list) and data else {}
	if not isinstance(row, dict):
		row = {}
	state = (row.get("state") or row.get("connectionStatus") or "").lower()
	if state in ("", "open", "connected"):
		return

	account_name = account.get("name")
	if not account_name:
		return

	cache_key = f"evolution_auto_reconnect:{account_name}"
	last = frappe.cache.get_value(cache_key)
	if last and (datetime.now() - last).total_seconds() < 300:
		return

	frappe.cache.set_value(cache_key, datetime.now(), expires_in_sec=300)
	try:
		full_account = frappe.get_doc("WhatsApp Account", account_name)
		client = EvolutionAPIClient(full_account)
		client.get_qr_code()  # reconnects from stored session (or returns a QR)
		frappe.logger().info(
			f"Evolution API auto-reconnect attempted for instance {account_name} (state={state})"
		)
	except Exception:
		frappe.log_error(
			title="Evolution API auto-reconnect failed",
			message=frappe.get_traceback(),
		)


@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	"""Evolution API webhook endpoint.

	The body is dispatched to this function as keyword arguments by the
	frappe API layer, so ``**kwargs`` is required to absorb them (the real
	payload is read from ``frappe.local.form_dict`` below).
	"""
	account = _validate_webhook_request()

	if account.get("status") != "Active":
		return {"ok": True, "message": "Ignored: WhatsApp Account is inactive"}

	payload = frappe.local.form_dict
	raw_data = None
	if frappe.request and isinstance(frappe.request.data, (bytes, str)) and frappe.request.data:
		raw_data = frappe.request.data
	if raw_data:
		try:
			payload = json.loads(raw_data)
		except ValueError:
			payload = dict(frappe.local.form_dict)

	if isinstance(payload, list):
		event_header = (
			frappe.request.headers.get("event")
			or frappe.request.headers.get("Event")
			or frappe.form_dict.get("event")
		)
		if not event_header and len(payload) > 0 and "update" in payload[0]:
			event_header = "MESSAGES_UPDATE"
		payload = {"event": event_header, "data": payload}

	event = payload.get("event")
	extracted = _extract_message_payload(payload)
	data = extracted["data"]

	if event in ("CONNECTION_UPDATE", "connection.update"):
		_auto_reconnect(account, payload)
		return {"ok": True, "message": "Ignored: connection event"}

	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Evolution Webhook",
		"meta_data": _as_json(payload),
	}).insert(ignore_permissions=True)

	remote_number = (
		normalize_phone(extracted["remote_jid"] or "", account.default_country_code)
		if extracted["remote_jid"]
		else None
	)
	direction = "Outgoing" if extracted["from_me"] else "Incoming"

	existing_name = None
	if extracted["message_id"]:
		existing_name = _find_existing_message(
			extracted["message_id"], direction
		)
	if existing_name:
		doc = frappe.get_doc("WhatsApp Message", existing_name)

		if event in ("MESSAGES_UPDATE", "messages.update"):
			update_status = data.get("status") or (data.get("update") or {}).get("status")
			if isinstance(update_status, int):
				update_status = EVENT_STATUS_MAP.get(update_status, update_status)
			elif isinstance(update_status, str):
				update_status = EVENT_STATUS_STRING_MAP.get(update_status.upper(), update_status.lower())
			doc.status = update_status or doc.status or "updated"
		elif direction == "Outgoing":
			doc.status = doc.status or "sent"

		doc.save(ignore_permissions=True)
		return {"ok": True, "message_log": doc.name}

	# Status-only events (messages.update / DELIVERY_ACK / READ) carry no
	# message body and address the chat by WhatsApp LID (e.g.
	# "91281040638117@lid") - Evolution reports the linked-ID form of the
	# real number. They can never create a conversation, only update an
	# existing one. Inserting here produced phantom records with an empty
	# message and the LID as the phone number.
	if event in ("MESSAGES_UPDATE", "messages.update"):
		return {"ok": True, "message": "Ignored: status update for unknown message"}

	return _insert_whatsapp_message(
		account, extracted, _map_content_type(data.get("messageType")), remote_number, direction
	)


@frappe.whitelist()
def mark_whatsapp_messages_read(reference_doctype: str, reference_name: str) -> dict:
	"""Send read receipts for unread incoming WhatsApp Messages of a reference.

	Used by the CRM frontend when a conversation is opened so the client
	receives the blue read tick without anyone opening the Web form. Only
	Evolution accounts are marked (Meta flow is unchanged) and only messages
	that have not already been marked read are touched.
	"""
	import frappe_whatsapp.utils.evolution_client as evo

	if not reference_doctype or not reference_name:
		return {"ok": False, "reason": "missing reference"}

	unread = frappe.db.get_all(
		"WhatsApp Message",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"type": "Incoming",
			"status": ["not in", ("read", "marked as read")],
		},
		fields=["name", "whatsapp_account", "message_id", "from"],
	)
	marked = 0
	for row in unread:
		if not row.get("message_id"):
			continue
		try:
			account = frappe.get_doc("WhatsApp Account", row["whatsapp_account"])
			if not evo.is_evolution_account(account):
				continue
			client = evo.EvolutionAPIClient(account)
			client.mark_message_as_read(
				remote_jid=row.get("from") or "",
				message_id=row["message_id"],
			)
			frappe.db.set_value(
				"WhatsApp Message", row["name"], "status", "read"
			)
			marked += 1
		except Exception:
			frappe.log_error(
				title="Failed to mark WhatsApp message read (CRM)",
				message=frappe.get_traceback(),
			)
	frappe.db.commit()
	return {"ok": True, "marked": marked}
