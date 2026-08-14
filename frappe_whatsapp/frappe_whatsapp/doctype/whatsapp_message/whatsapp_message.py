# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
import re
import frappe
from frappe import _, throw
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request

from frappe_whatsapp.utils import get_whatsapp_account, format_number

class WhatsAppMessage(Document):
    def validate(self):
        self.set_whatsapp_account()

    def on_update(self):
        self.update_profile_name()

    def update_profile_name(self):
        number = self.get("from")
        if not number:
            return
        from_number = format_number(number)

        if (
            self.has_value_changed("profile_name")
            and self.profile_name
            and from_number
            and frappe.db.exists("WhatsApp Profiles", {"number": from_number})
        ):
            profile_id = frappe.get_value("WhatsApp Profiles", {"number": from_number}, "name")
            frappe.db.set_value("WhatsApp Profiles", profile_id, "profile_name", self.profile_name)

    def create_whatsapp_profile(self):
        number = format_number(self.get("from") or self.to)
        if not frappe.db.exists("WhatsApp Profiles", {"number": number}):
            frappe.get_doc({
                "doctype": "WhatsApp Profiles",
                "profile_name": self.profile_name,
                "number": number,
                "whatsapp_account": self.whatsapp_account
            }).insert(ignore_permissions=True)

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            account_type = 'outgoing' if self.type == 'Outgoing' else 'incoming'
            default_whatsapp_account = get_whatsapp_account(account_type=account_type)
            if not default_whatsapp_account:
                throw(_("Please set a default outgoing WhatsApp Account or Select available WhatsApp Account"))
            else:
                self.whatsapp_account = default_whatsapp_account.name

    """Send whats app messages."""
    def before_insert(self):
        """Send message."""
        self.set_whatsapp_account()
        if not self.flags.get("ignore_send"):
            # Route to template path when a template is selected,
            # since message_type is read_only and cannot be set from the UI.
            if self.template:
                self.message_type = "Template"
            self.send_outgoing()
        self.create_whatsapp_profile()

    def send_outgoing(self):
        """Dispatch an Outgoing message.

        Called from `before_insert` for first-time sends and from bulk
        retry for re-sending Failed messages. No-op for non-Outgoing docs.
        Routes to the Evolution API client when the account is an Evolution
        API account, otherwise sends to Meta (existing behaviour).
        On non-template sends, raises and sets status to Failed on error;
        on template sends, `send_template` -> `notify` raises on error.
        """
        if self.type != "Outgoing":
            return

        whatsapp_account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        from frappe_whatsapp.utils.evolution_client import is_evolution_account

        is_evo = is_evolution_account(whatsapp_account)

        # Match the template type with the provider account. Meta templates are
        # only sent through Meta API accounts and Evolution interactive
        # templates only through Evolution API accounts. A mismatch is
        # silently skipped (no send request is made).
        if self.message_type == "Template" and self.template:
            template_doc = frappe.get_doc("WhatsApp Templates", self.template)
            template_type = template_doc.get("template_type") or "Meta"
            is_evo_template = template_type != "Meta"

            if is_evo != is_evo_template:
                self.status = "Success"
                return

        if is_evo:
            self.send_outgoing_evolution(whatsapp_account)
            return

        if self.message_type != "Template":
            if self.attach and not self.attach.startswith("http"):
                link = frappe.utils.get_url() + "/" + self.attach
            else:
                link = self.attach

            data = {
                "messaging_product": "whatsapp",
                "to": format_number(self.to),
                "type": self.content_type,
            }
            if self.is_reply and self.reply_to_message_id:
                data["context"] = {"message_id": self.reply_to_message_id}
            if self.content_type in ["document", "image", "video"]:
                data[self.content_type.lower()] = {
                    "link": link,
                    "caption": self.message,
                }
            elif self.content_type == "reaction":
                data["reaction"] = {
                    "message_id": self.reply_to_message_id,
                    "emoji": self.message,
                }
            elif self.content_type == "text":
                data["text"] = {"preview_url": True, "body": self.message}

            elif self.content_type == "audio":
                data["audio"] = {"link": link}

            elif self.content_type == "interactive":
                # Interactive message (buttons or list)
                data["type"] = "interactive"
                buttons_data = json.loads(self.buttons) if isinstance(self.buttons, str) else self.buttons

                if isinstance(buttons_data, list) and len(buttons_data) > 3:
                    # Use list message for more than 3 options (max 10)
                    data["interactive"] = {
                        "type": "list",
                        "body": {"text": self.message},
                        "action": {
                            "button": "Select Option",
                            "sections": [{
                                "title": "Options",
                                "rows": [
                                    {"id": btn["id"], "title": btn["title"], "description": btn.get("description", "")}
                                    for btn in buttons_data[:10]
                                ]
                            }]
                        }
                    }
                else:
                    # Use button message for 3 or fewer options
                    data["interactive"] = {
                        "type": "button",
                        "body": {"text": self.message},
                        "action": {
                            "buttons": [
                                {
                                    "type": "reply",
                                    "reply": {"id": btn["id"], "title": btn["title"]}
                                }
                                for btn in buttons_data[:3]
                            ]
                        }
                    }

            elif self.content_type == "flow":
                # WhatsApp Flow message
                if not self.flow:
                    frappe.throw(_("WhatsApp Flow is required for flow content type"))

                flow_doc = frappe.get_doc("WhatsApp Flow", self.flow)

                if not flow_doc.flow_id:
                    frappe.throw(_("Flow must be created on WhatsApp before sending"))

                # Determine flow mode - draft flows can be tested with mode: "draft"
                flow_mode = None
                if flow_doc.status != "Published":
                    flow_mode = "draft"
                    frappe.msgprint(_("Sending flow in draft mode (for testing only)"), indicator="orange")

                # Get first screen if not specified
                flow_screen = self.flow_screen
                if not flow_screen and flow_doc.screens:
                    flow_screen = flow_doc.screens[0].screen_id

                data["type"] = "interactive"
                data["interactive"] = {
                    "type": "flow",
                    "body": {"text": self.message or "Please fill out the form"},
                    "action": {
                        "name": "flow",
                        "parameters": {
                            "flow_message_version": "3",
                            "flow_id": flow_doc.flow_id,
                            "flow_cta": self.flow_cta or flow_doc.flow_cta or "Open",
                            "flow_action": "navigate",
                            "flow_action_payload": {
                                "screen": flow_screen
                            }
                        }
                    }
                }

                # Add draft mode for testing unpublished flows
                if flow_mode:
                    data["interactive"]["action"]["parameters"]["mode"] = flow_mode

                # Add flow token - generate one if not provided (required by WhatsApp)
                flow_token = self.flow_token or frappe.generate_hash(length=16)
                data["interactive"]["action"]["parameters"]["flow_token"] = flow_token

            try:
                self.notify(data)
                self.status = "Success"
            except Exception as e:
                self.status = "Failed"
                frappe.throw(f"Failed to send message {str(e)}")
        elif not self.message_id:
            self.send_template()

    def send_template(self):
        """Send template."""
        template = frappe.get_doc("WhatsApp Templates", self.template)
        data = {
            "messaging_product": "whatsapp",
            "to": format_number(self.to),
            "type": "template",
            "template": {
                "name": template.actual_name or template.template_name,
                "language": {"code": template.language_code},
                "components": [],
            },
        }

        parameters = []
        template_parameters = []
        if template.sample_values:
            field_names = template.field_names.split(",") if template.field_names else template.sample_values.split(",")

            if self.body_param is not None:
                params = list(json.loads(self.body_param).values())
                for param in params:
                    parameters.append({"type": "text", "text": param})
                    template_parameters.append(param)
            elif self.flags.custom_ref_doc:
                custom_values = self.flags.custom_ref_doc
                for field_name in field_names:
                    value = custom_values.get(field_name.strip())
                    parameters.append({"type": "text", "text": value})
                    template_parameters.append(value)                    

            else:
                ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
                for field_name in field_names:
                    value = ref_doc.get_formatted(field_name.strip())
                    parameters.append({"type": "text", "text": value})
                    template_parameters.append(value)

            self.template_parameters = json.dumps(template_parameters)

        # Always add the body component, even if parameters list is empty
        data["template"]["components"].append({
            "type": "body",
            "parameters": parameters,
        })

        if template.header_type:
            if self.attach:
                if self.attach.startswith("http"):
                    url = f'{self.attach}'
                else:
                    url = f'{frappe.utils.get_url()}{self.attach}'
                if template.header_type == 'IMAGE':
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "image",
                            "image": {
                                "link": url
                            }
                        }]
                    })

                elif template.header_type == 'DOCUMENT':
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "document",
                            "document": {
                                "link": url,
                                "filename": "document.pdf"  # should be configurable
                            }
                        }]
                    })

            elif template.sample:
                if template.header_type == 'IMAGE':
                    if template.sample.startswith("http"):
                        url = f'{template.sample}'
                    else:
                        url = f'{frappe.utils.get_url()}{template.sample}'
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "image",
                            "image": {
                                "link": url
                            }
                        }]
                    })

        # We check this before standard buttons because MPM is an interactive action
        has_mpm = False
        if self.product_catalog_json:
            try:
                catalog_data = json.loads(self.product_catalog_json)
                data['template']['components'].append({
                    "type": "button",
                    "sub_type": "mpm",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "action",
                            "action": catalog_data
                        }
                    ]
                })
                has_mpm = True
            except Exception as e:
                frappe.log_error(f"Failed to parse Product Catalog JSON: {str(e)}", "WhatsApp MPM Error")

        if template.buttons:
            # Only buttons with *runtime* parameters go into components.
            # Static Call Phone and static Visit Website buttons are applied
            # by Meta from the approved template — sending them here yields
            # "sub_type must be one of {...}" errors since Meta no longer
            # accepts `phone_number`. See issue #188.
            button_parameters = []
            for idx, btn in enumerate(template.buttons):
                # Shift index if MPM was added at index 0
                current_idx = str(idx + 1) if has_mpm else str(idx)

                if btn.button_type == "Quick Reply":
                    button_parameters.append({
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": current_idx,
                        "parameters": [{"type": "payload", "payload": btn.button_label}]
                    })
                elif btn.button_type == "Visit Website" and btn.url_type == "Dynamic":
                    ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
                    url = ref_doc.get_formatted(btn.website_url)
                    button_parameters.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": current_idx,
                        "parameters": [{"type": "text", "text": url}]
                    })

            if button_parameters:
                data['template']['components'].extend(button_parameters)

        self.notify(data)

    def send_outgoing_evolution(self, whatsapp_account):
        """Send an Outgoing message through the Evolution API.

        Only Evolution interactive templates (buttons / list / carousel) are
        sent here. Meta templates are routed to the Meta API or silently
        skipped by `send_outgoing` when the provider does not match.
        """
        from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient, get_message_id

        client = EvolutionAPIClient(whatsapp_account)
        try:
            if self.message_type == "Template":
                self.message_id = self.send_template_evolution(client)
            else:
                self.message_id = self.notify_evolution(client)
            self.status = "Success"
        except Exception as e:
            self.status = "Failed"
            frappe.throw(f"Failed to send message {str(e)}")

    def send_template_evolution(self, client):
        """Send a WhatsApp template via Evolution API.

        Supports Meta-style templates (rendered as plain text) and
        Revolution interactive templates (buttons / list / carousel)
        that are sent through the dedicated Evolution endpoints — one
        message per enabled component.
        """
        template = frappe.get_doc("WhatsApp Templates", self.template)
        template_type = template.get("template_type") or "Meta"

        if template_type == "Revolution":
            return self._send_evolution_components(client, template)

        parameters = self.get_template_parameters(template)
        message = self.render_template_text(template, parameters)
        self.message = message
        self.template_parameters = json.dumps(parameters, default=str)

        media = self.resolve_attach_url()
        header_type = template.get("header_type") or ""
        if header_type == "IMAGE" and media:
            from frappe_whatsapp.utils.evolution_client import media_to_base64

            response = client.send_media(
                to=self.to, media=media_to_base64(media), mediatype="image",
                mimetype="", filename="image", caption=message,
            )
        elif header_type == "DOCUMENT" and media:
            from frappe_whatsapp.utils.evolution_client import media_to_base64

            response = client.send_media(
                to=self.to, media=media_to_base64(media), mediatype="document",
                mimetype="", filename="document", caption=message,
            )
        else:
            response = client.send_text(to=self.to, message=message)

        return get_message_id(response)

    def _send_evolution_components(self, client, template):
        """Send one Evolution message per enabled component (buttons / list / carousel).

        Components with no actual content (e.g. a buttons component with an
        empty buttons list) are skipped — sending them yields HTTP 400 from
        the Evolution API.
        """
        payload = self._get_template_payload(template)
        message_ids = []

        buttons = payload.get("buttons")
        if template.get("evo_enable_buttons") and isinstance(buttons, dict) and buttons.get("buttons"):
            message_ids.append(self._send_evolution_buttons(client, template, buttons))

        list_component = payload.get("list")
        if template.get("evo_enable_list") and isinstance(list_component, dict) and list_component.get("sections"):
            message_ids.append(self._send_evolution_list(client, template, list_component))

        carousel = payload.get("carousel")
        if template.get("evo_enable_carousel") and isinstance(carousel, dict) and carousel.get("cards"):
            message_ids.append(self._send_evolution_carousel(client, template, carousel))

        if not message_ids:
            frappe.throw(_("No enabled template component with content to send — enable Buttons, List or Carousel and rebuild the payload"))
        return ", ".join(str(message_id) for message_id in message_ids)

    def _get_template_payload(self, template):
        """Return the Evolution template payload as a dict."""
        payload = template.get("template_payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        return payload or {}

    def _fill_evolution_placeholders(self, template, value):
        """Recursively replace {{1}}, {{2}}... placeholders with parameter values."""
        parameters = self.get_template_parameters(template)
        if not parameters:
            return value

        def fill(text):
            for idx, param in enumerate(parameters, start=1):
                text = text.replace("{{%s}}" % idx, str(param or ""))
            return text

        if isinstance(value, str):
            return fill(value)
        if isinstance(value, list):
            return [self._fill_evolution_placeholders(template, item) for item in value]
        if isinstance(value, dict):
            return {
                key: self._fill_evolution_placeholders(template, item)
                for key, item in value.items()
            }
        return value

    def _render_evolution_payload_text(self, payload):
        """Render a filled Evolution payload as readable text for the record."""
        lines = []

        def add(key, value):
            value = str(value or "").strip()
            if value:
                lines.append(value)

        add("title", payload.get("title"))
        add("description", payload.get("description"))
        add("body", payload.get("body"))
        add("footerText", payload.get("footerText") or payload.get("footer"))
        add("buttonText", payload.get("buttonText"))

        for button in payload.get("buttons") or []:
            text = button.get("displayText") or ""
            if button.get("type") and text:
                text = "[{}] {}".format(button.get("type"), text)
            add("button", text)

        for section in payload.get("sections") or []:
            section_lines = []
            section_title = str(section.get("title") or "").strip()
            if section_title:
                section_lines.append(section_title)
            for row in section.get("rows") or []:
                row_text = str(row.get("title") or "").strip()
                if row.get("description"):
                    row_text = "{}: {}".format(row_text, row.get("description"))
                if row_text:
                    section_lines.append("  " + row_text)
            if section.get("buttonText"):
                section_lines.append("  [select] {}".format(section.get("buttonText")))
            if section_lines:
                lines.append("\n".join(section_lines))

        for card in payload.get("cards") or []:
            card_parts = []
            for key in ("title", "body", "footer"):
                if card.get(key):
                    card_parts.append(str(card[key]))
            for button in card.get("buttons") or []:
                text = button.get("displayText") or ""
                if button.get("type") and text:
                    text = "[{}] {}".format(button.get("type"), text)
                if text:
                    card_parts.append(text)
            if card_parts:
                lines.append(" — ".join(card_parts))

        return "\n\n".join(lines)

    def _send_evolution_buttons(self, client, template, component=None):
        """Send an Evolution buttons (quick-reply) template."""
        from frappe_whatsapp.utils.evolution_client import get_message_id

        if component is None:
            payload = self._get_template_payload(template)
            component = payload.get("buttons") or payload
        if not component:
            frappe.throw(_("Template payload is required for the Buttons component"))
        payload = self._fill_evolution_placeholders(template, component)
        self.message = self._render_evolution_payload_text(payload)

        buttons = payload.get("buttons") or []
        if not buttons:
            frappe.throw(_("The Buttons component has no buttons — add buttons in the template and rebuild the payload"))
        title = payload.get("title")
        description = payload.get("description")
        footer = payload.get("footer")

        response = client.send_buttons(
            to=self.to,
            buttons=buttons,
            title=title,
            description=description,
            footer=footer,
        )
        return get_message_id(response)

    def _send_evolution_list(self, client, template, component=None):
        """Send an Evolution list template."""
        from frappe_whatsapp.utils.evolution_client import get_message_id

        if component is None:
            payload = self._get_template_payload(template)
            component = payload.get("list") or payload
        if not component:
            frappe.throw(_("Template payload is required for the List component"))
        payload = self._fill_evolution_placeholders(template, component)
        self.message = self._render_evolution_payload_text(payload)

        sections = payload.get("sections") or []
        if not sections:
            frappe.throw(_("The List component has no sections — add sections in the template and rebuild the payload"))
        button_text = payload.get("buttonText")
        title = payload.get("title")
        description = payload.get("description")
        footer = payload.get("footerText") or payload.get("footer")

        response = client.send_list(
            to=self.to,
            sections=sections,
            button_text=button_text,
            title=title,
            description=description,
            footer=footer,
        )
        return get_message_id(response)

    def _send_evolution_carousel(self, client, template, component=None):
        """Send an Evolution carousel template."""
        from frappe_whatsapp.utils.evolution_client import get_message_id

        if component is None:
            payload = self._get_template_payload(template)
            component = payload.get("carousel") or payload
        if not component:
            frappe.throw(_("Template payload is required for the Carousel component"))
        payload = self._fill_evolution_placeholders(template, component)
        self.message = self._render_evolution_payload_text(payload)

        cards = payload.get("cards") or []
        if not cards:
            frappe.throw(_("The Carousel component has no cards — add cards in the template and rebuild the payload"))
        body = payload.get("body") or payload.get("description")
        title = payload.get("title")
        description = payload.get("description")
        footer = payload.get("footerText") or payload.get("footer")

        response = client.send_carousel(
            to=self.to,
            cards=cards,
            body=body,
            title=title,
            description=description,
            footer=footer,
        )
        return get_message_id(response)

    def get_template_parameters(self, template):
        """Resolve template parameter values from body_param, custom ref doc or the reference document.

        Each entry in ``field_names`` is resolved against the reference
        document. Supported notations:

        - ``field`` — plain field on the reference document
        - ``field.subfield`` — follow Link fields across documents
        - ``childtable[0].field`` — field on the n-th row of a child table
        - ``childtable[field=value].field`` — field on the row whose field
          equals value (first match)
        - ``Doctype[field=value, field2=value2].field`` — look up a record
          of another doctype (``{field}`` substitutes the reference
          document's own value)
        - ``sum(childtable[].field)`` — sum the field across all rows
        - ``count(childtable[])`` — number of rows in the child table
        - ``=literal`` — a fixed value used as-is (no document lookup)
        """
        parameters = []
        if not template.field_names and not template.sample_values:
            return parameters

        field_names = (
            self._split_field_names(template.field_names)
            if template.field_names
            else self._split_field_names(template.sample_values)
        )

        if self.body_param is not None:
            parameters = list(json.loads(self.body_param).values())
        elif self.flags.custom_ref_doc:
            custom_values = self.flags.custom_ref_doc
            parameters = [self._resolve_field_path(custom_values, field_name.strip()) for field_name in field_names]
        else:
            ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
            parameters = [self._resolve_field_path(ref_doc, field_name.strip()) for field_name in field_names]

        # Fall back to sample values for parameters that could not be
        # resolved from the reference document (e.g. the template's
        # field_names do not exist on the reference doctype).
        samples = self._split_field_names(template.sample_values) if template.sample_values else []
        for idx, param in enumerate(parameters):
            if param is None or (isinstance(param, str) and not param.strip()):
                if idx < len(samples):
                    parameters[idx] = samples[idx]

        return [", ".join(str(v) for v in param) if isinstance(param, list) else param for param in parameters]

    @staticmethod
    def _split_field_names(field_names):
        """Split on newlines when present, otherwise on commas not inside [...]."""
        value = (field_names or "")
        if "\n" in value:
            return [part.strip() for part in value.split("\n") if part.strip()]
        parts = []
        current = []
        depth = 0
        for ch in value:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append("".join(current))
        return [part.strip() for part in parts if part.strip()]

    def _resolve_field_path(self, doc, path, depth=0):
        """Resolve a dotted field path against a document (or dict).

        Supports child-table indexing (``items[0].rate``), row filters
        (``items[item_code=ABC].rate``), doctype lookups
        (``Item Price[item_code={item_code}, price_list=Standard Buying].price_list_rate``),
        aggregate wrappers (``sum(items[].rate)``, ``count(items[])``) and
        Link traversal (``item.rate``). ``{field}`` inside a filter value
        substitutes the field's value from the reference document.
        ``=value`` returns a literal. Returns None when the path cannot
        be resolved.
        """
        if depth > 5:
            return None
        path = (path or "").strip()
        if path.startswith("="):
            return path[1:]
        if not path:
            return None

        aggregate = re.match(r"^(sum|count)\((.*)\)$", path)
        if aggregate:
            agg, inner = aggregate.group(1), aggregate.group(2)
            values = self._resolve_field_path(doc, inner, depth=depth + 1)
            if not isinstance(values, list):
                return None
            if agg == "count":
                return len(values)
            total = 0.0
            for value in values:
                try:
                    if isinstance(value, str):
                        value = re.sub(r"[^0-9.\-]", "", value)
                    total += float(value)
                except (TypeError, ValueError):
                    pass
            return frappe.utils.fmt_money(total) if values else "0"

        first, _, rest = path.partition(".")
        selector = None
        if "[" in first and first.endswith("]"):
            first, selection = first.split("[", 1)
            selection = selection.rstrip("]").strip()
            if not selection:
                selector = {"type": "all"}
            elif selection.isdigit():
                selector = {"type": "index", "index": int(selection)}
            else:
                filters = {}
                for pair in selection.split(","):
                    fieldname, _, fieldvalue = pair.partition("=")
                    fieldvalue = fieldvalue.strip().strip("'\"")
                    match = re.match(r"^\{(.+)\}$", fieldvalue)
                    if match:
                        resolved = doc.get(match.group(1)) if isinstance(doc, frappe.model.document.Document) else (doc or {}).get(match.group(1))
                        fieldvalue = resolved if resolved is not None else ""
                    filters[fieldname.strip()] = fieldvalue
                selector = {"type": "filter", "filters": filters}

        is_doc = isinstance(doc, frappe.model.document.Document)
        value = doc.get(first) if is_doc else (doc or {}).get(first)

        if selector:
            if value is None and selector["type"] in ("filter", "all"):
                if frappe.db.exists("DocType", first):
                    rows = frappe.get_all(
                        first,
                        filters=selector["filters"] if selector["type"] == "filter" else {},
                        limit=100 if selector["type"] == "filter" else None,
                        order_by="modified desc",
                    )
                    if selector["type"] == "all":
                        if not rows:
                            return None
                        docs = [frappe.get_doc(first, row["name"]) for row in rows]
                        if not rest:
                            return docs
                        resolved = [self._resolve_field_path(d, rest, depth=depth + 1) for d in docs]
                        return [item for item in resolved if item is not None]
                    if not rows:
                        return None
                    if not rest:
                        return frappe.get_doc(first, rows[0]["name"])
                    docs = [frappe.get_doc(first, row["name"]) for row in rows]
                    resolved = [self._resolve_field_path(d, rest, depth=depth + 1) for d in docs]
                    resolved = [item for item in resolved if item is not None]
                    if not resolved:
                        return None
                    return ", ".join(str(item) for item in resolved)
                return None
            if not isinstance(value, (list, tuple)):
                return None
            if selector["type"] == "index":
                try:
                    value = value[selector["index"]]
                except IndexError:
                    return None
            elif selector["type"] == "filter":
                matches = [
                    row
                    for row in value
                    if all(
                        str((row or {}).get(fieldname) or "") == str(fieldvalue)
                        for fieldname, fieldvalue in selector["filters"].items()
                    )
                ]
                if not matches:
                    return None
                if not rest:
                    return matches[0]
                resolved = [self._resolve_field_path(row, rest, depth=depth + 1) for row in matches]
                resolved = [item for item in resolved if item is not None]
                if not resolved:
                    return None
                return ", ".join(str(item) for item in resolved)
            else:
                if not rest:
                    return list(value)
                resolved = [self._resolve_field_path(row, rest, depth=depth + 1) for row in value]
                return [item for item in resolved if item is not None]
            if not rest:
                return value

        if rest:
            if value is None:
                return None
            if isinstance(value, str):
                field = doc.meta.get_field(first) if is_doc else None
                if field and field.fieldtype == "Link":
                    try:
                        value = frappe.get_doc(field.options, value)
                    except Exception:
                        return None
                else:
                    return None
            return self._resolve_field_path(value, rest, depth=depth + 1)

        return self._format_parameter_value(doc, first, value)

    def _format_parameter_value(self, doc, fieldname, value):
        """Format a leaf value, using the document's formatter when possible."""
        if value is None or isinstance(value, frappe.model.document.Document):
            return value
        if isinstance(doc, frappe.model.document.Document):
            try:
                return doc.get_formatted(fieldname)
            except Exception:
                return value
        return value

    def render_template_text(self, template, parameters):
        """Substitute {{n}} placeholders in the template text with parameter values."""
        def fill(text):
            for idx, param in enumerate(parameters, start=1):
                text = text.replace("{{%s}}" % idx, str(param or ""))
            return text

        parts = []
        if template.get("header_type") == "TEXT" and template.get("header"):
            parts.append(fill(template.header))
        parts.append(fill(template.get("template") or ""))
        if template.get("footer"):
            parts.append(template.footer)
        return "\n".join(part for part in parts if part)

    def notify_evolution(self, client):
        """Send content through the Evolution API and record the message id."""
        from frappe_whatsapp.utils.evolution_client import get_message_id

        content_type = self.get("content_type") or "text"
        quoted = None
        if self.get("is_reply") and self.get("reply_to_message_id"):
            quoted = {"key": {"id": self.reply_to_message_id}}

        if content_type == "text":
            response = client.send_text(to=self.to, message=self.message or "", quoted=quoted)
        elif content_type in ("image", "video", "document"):
            from frappe_whatsapp.utils.evolution_client import media_to_base64

            response = client.send_media(
                to=self.to,
                media=media_to_base64(self.resolve_attach_url()),
                mediatype=content_type,
                mimetype="",
                filename=self.attach_file_name(content_type),
                caption=self._media_caption(),
                quoted=quoted,
            )
        elif content_type == "audio":
            response = client.send_audio(to=self.to, audio=self.attach_as_base64())
        elif content_type == "interactive":
            buttons_data = json.loads(self.buttons) if isinstance(self.buttons, str) else self.buttons
            if isinstance(buttons_data, list) and buttons_data:
                if len(buttons_data) > 3:
                    # Use list message for more than 3 options (max 10)
                    rows = []
                    for btn in buttons_data[:10]:
                        row = {"rowId": btn["id"], "title": btn["title"]}
                        if btn.get("description"):
                            row["description"] = btn["description"]
                        rows.append(row)

                    response = client.send_list(
                        to=self.to,
                        sections=[{"title": "Options", "rows": rows}],
                        button_text="Select an option",
                        title=self.message or "",
                        footer="",
                        quoted=quoted,
                    )
                else:
                    # Use quick-reply buttons for 3 or fewer options
                    response = client.send_buttons(
                        to=self.to,
                        buttons=[
                            {
                                "type": "reply",
                                "displayText": btn["title"],
                                "id": btn["id"],
                            }
                            for btn in buttons_data
                        ],
                        title=self.message or "",
                        quoted=quoted,
                    )
            else:
                response = client.send_text(to=self.to, message=self.interactive_as_text())
        elif content_type in ("reaction", "flow"):
            frappe.throw(
                _("Content type {0} is not supported by the Evolution API provider").format(
                    frappe.bold(content_type)
                )
            )
        else:
            response = client.send_text(to=self.to, message=self.message or "")

        return get_message_id(response)

    def interactive_as_text(self):
        """Render an interactive (buttons/list) message as plain text."""
        text = self.message or ""
        buttons_data = self.buttons
        if isinstance(buttons_data, str):
            buttons_data = json.loads(buttons_data)
        if isinstance(buttons_data, list) and buttons_data:
            options = "\n".join(
                f"{btn.get('id')}: {btn.get('title')}"
                for btn in buttons_data
                if isinstance(btn, dict) and btn.get("title")
            )
            if options:
                text = f"{text}\n\n{options}" if text else options
        if not text:
            frappe.throw(_("Interactive messages need a message body"))
        return text

    def resolve_attach_url(self):
        """Resolve the attach field to an absolute URL Evolution API can fetch."""
        if not self.attach:
            return None
        if self.attach.startswith("http"):
            return self.attach
        return f"{frappe.utils.get_url()}/{self.attach.lstrip('/')}"

    def attach_file_name(self, content_type):
        """Best-effort file name for a media attachment."""
        url = self.resolve_attach_url() or ""
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if name and "." in name:
            return name
        return f"media.{content_type}"

    def _media_caption(self):
        """Caption for a media send.

        Some consumers record the attach path as the message when no text
        was given (e.g. CRM's ``message or attach``). Never send a file
        path/URL as the WhatsApp caption — fall back to an empty caption so
        the recipient sees the media without a bogus "message".
        """
        caption = self.message or ""
        attach = self.attach or ""
        candidate_paths = {
            attach,
            self.resolve_attach_url() or "",
        }
        stripped = caption.strip()
        if stripped in candidate_paths:
            return ""
        if stripped.startswith(("/files/", "/private/files/", "/private/")):
            return ""
        return caption

    def attach_as_base64(self):
        """Download the attach file and return it as base64 (for audio messages)."""
        import base64
        import requests as _requests

        url = self.resolve_attach_url()
        if not url:
            frappe.throw(_("Attachment is required to send audio messages"))
        response = _requests.get(url, timeout=30)
        if response.status_code != 200:
            frappe.throw(_("Failed to download attachment for audio message"))
        return base64.b64encode(response.content).decode("utf-8")

    def notify(self, data):
        """Notify."""
        whatsapp_account = frappe.get_doc(
            "WhatsApp Account",
            self.whatsapp_account,
        )
        token = whatsapp_account.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        try:
            response = make_post_request(
                f"{whatsapp_account.url}/{whatsapp_account.version}/{whatsapp_account.phone_id}/messages",
                headers=headers,
                data=json.dumps(data),
            )
            self.message_id = response["messages"][0]["id"]

        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("Error", res.get("message"))
            frappe.get_doc(
                {
                    "doctype": "WhatsApp Notification Log",
                    "template": "Text Message",
                    "meta_data": frappe.flags.integration_request.json(),
                }
            ).insert(ignore_permissions=True)

            frappe.throw(msg=error_message, title=res.get("error_user_title", "Error"))

    def format_number(self, number):
        """Format number."""
        if number.startswith("+"):
            number = number[1 : len(number)]

        return number

    @frappe.whitelist()
    def send_read_receipt(self):
        whatsapp_account = frappe.get_doc(
            "WhatsApp Account",
            self.whatsapp_account,
        )

        from frappe_whatsapp.utils.evolution_client import EvolutionAPIClient, is_evolution_account

        if is_evolution_account(whatsapp_account):
            try:
                client = EvolutionAPIClient(whatsapp_account)
                client.mark_message_as_read(
                    remote_jid=self.get("from") or self.to,
                    message_id=self.message_id,
                )
                self.status = "read"
                self.save()
                return True
            except Exception as e:
                frappe.log_error("WhatsApp API Error", f"{str(e)}")
                return False

        data = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": self.message_id
        }

        token = whatsapp_account.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        try:
            response = make_post_request(
                f"{whatsapp_account.url}/{whatsapp_account.version}/{whatsapp_account.phone_id}/messages",
                headers=headers,
                data=json.dumps(data),
            )

            if response.get("success"):
                self.status = "read"
                self.save()
                return response.get("success")

        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("Error", res.get("message"))
            frappe.log_error("WhatsApp API Error", f"{error_message}\n{res}")


def on_doctype_update():
    frappe.db.add_index("WhatsApp Message", ["reference_doctype", "reference_name"])


@frappe.whitelist()
def send_template(to, reference_doctype, reference_name, template):
    try:
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "to": to,
            "type": "Outgoing",
            "message_type": "Template",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "content_type": "text",
            "template": template
        })

        doc.save()
    except Exception as e:
        raise e
