"""Create whatsapp template."""

# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
from decimal import Decimal
import frappe
from frappe import _
import magic
import requests
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request
from frappe.desk.form.utils import get_pdf_link

from frappe_whatsapp.utils import get_whatsapp_account

class WhatsAppTemplates(Document):  # nosemgrep: frappe-modifying-but-not-committing-other-method -- get_settings() sets self._token/_url/_version/_business_id/_app_id/_headers as in-memory scratch for the outbound Meta HTTP call; they are not DocType fields and must not be persisted
    """Create whatsapp template."""

    def validate(self):
        self.set_whatsapp_account()
        template_type = self.get("template_type") or "Meta"

        # Template body and Template Label are shared by both Meta and
        # Evolution templates (used by CRM to render the message).
        if not self.template:
            frappe.throw(_("Template is required"))

        if template_type != "Meta":
            if self.has_builder_data():
                self.template_payload = json.dumps(self._build_evolution_payload(), indent=2)

            if not self.template_payload:
                frappe.throw(_("Template Payload is required for Revolution interactive templates"))
            try:
                payload = json.loads(self.template_payload)
            except (ValueError, TypeError):
                frappe.throw(_("Template Payload must be valid JSON"))
            self._validate_evolution_payload(payload)
            return

        if not self.language:
            frappe.throw(_("Language is required for Meta templates"))
        if not self.language_code or self.has_value_changed("language"):
            lang_code = frappe.db.get_value("Language", self.language) or "en"
            self.language_code = lang_code.replace("-", "_")

        if self.header_type in ["IMAGE", "DOCUMENT"] and self.sample:
            self.get_session_id(self.sample)
            self.get_media_id(self.sample)

        if not self.is_new():
            self.update_template()

    def before_insert(self):
        # autoname (template_name-language_code) runs before validate(), so
        # ensure a language code exists for Revolution interactive templates.
        template_type = self.get("template_type") or "Meta"
        if template_type != "Meta" and not self.language_code:
            self.language_code = "en"

    def has_builder_data(self):
        """Whether any of the Evolution builder fields have content."""
        return bool(
            self.get("evo_buttons_title")
            or self.get("evo_buttons_body")
            or self.get("evo_buttons_footer")
            or self.get("evo_list_title")
            or self.get("evo_list_body")
            or self.get("evo_list_footer")
            or self.get("evo_carousel_title")
            or self.get("evo_carousel_body")
            or self.get("evo_carousel_footer")
            or self.get("evo_button_text")
            or self.get("evo_buttons")
            or self.get("evo_sections")
            or self.get("evo_cards")
        )

    def _build_evolution_payload(self):
        """Build the Evolution payload dict from the builder fields.

        One top-level key per enabled component (``buttons``, ``list``,
        ``carousel``); sending one message per enabled component.
        """
        payload = {}
        if self.get("evo_enable_buttons"):
            payload["buttons"] = self._build_evo_buttons_payload()
        if self.get("evo_enable_list"):
            payload["list"] = self._build_evo_list_payload()
        if self.get("evo_enable_carousel"):
            payload["carousel"] = self._build_evo_carousel_payload()
        return payload

    def _build_evo_buttons_payload(self):
        payload = {}
        if self.get("evo_buttons_title"):
            payload["title"] = self.evo_buttons_title
        if self.get("evo_buttons_body"):
            payload["description"] = self.evo_buttons_body
        if self.get("evo_buttons_footer"):
            payload["footer"] = self.evo_buttons_footer
        payload["buttons"] = [self._build_evo_button(btn) for btn in self.get("evo_buttons") or []]
        return payload

    def _build_evo_list_payload(self):
        payload = {}
        if self.get("evo_list_title"):
            payload["title"] = self.evo_list_title
        if self.get("evo_list_body"):
            payload["description"] = self.evo_list_body
        if self.get("evo_list_footer"):
            payload["footerText"] = self.evo_list_footer
        if self.get("evo_button_text"):
            payload["buttonText"] = self.evo_button_text
        payload["sections"] = [
            {
                "title": section.title,
                "rows": [
                    self._build_evo_row(row)
                    for row in self.get("evo_section_rows") or []
                    if (row.get("section") or "") == section.title
                ],
            }
            for section in self.get("evo_sections") or []
        ]
        return payload

    def _build_evo_carousel_payload(self):
        payload = {}
        if self.get("evo_carousel_title"):
            payload["title"] = self.evo_carousel_title
        if self.get("evo_carousel_body"):
            payload["body"] = self.evo_carousel_body
        if self.get("evo_carousel_footer"):
            payload["footerText"] = self.evo_carousel_footer
        cards = []
        for card in self.get("evo_cards") or []:
            c = {"body": card.body}
            if card.get("title"):
                c["title"] = card.title
            if card.get("footer"):
                c["footer"] = card.footer
            if card.get("image_url"):
                c["imageUrl"] = card.image_url
            if card.get("button_type"):
                c["buttons"] = [self._build_evo_button(card)]
            cards.append(c)
        payload["cards"] = cards
        return payload

    @staticmethod
    def _build_evo_button(btn):
        """Map a button (builder row or JSON dict) to the Evolution API button shape."""
        button = {
            "type": (btn.get("button_type") or btn.get("type") or "reply").lower(),
            "displayText": btn.get("display_text") or btn.get("displayText") or "",
        }
        for src, dst in (
            ("id", "id"),
            ("url", "url"),
            ("phone_number", "phoneNumber"),
            ("phoneNumber", "phoneNumber"),
            ("copy_code", "copyCode"),
            ("copyCode", "copyCode"),
        ):
            if btn.get(src):
                button[dst] = btn.get(src)
        return button

    @staticmethod
    def _build_evo_row(row):
        """Map a row (builder row or JSON dict) to the Evolution API row shape."""
        r = {
            "title": row.get("title") or "",
            "rowId": row.get("row_id") or row.get("rowId") or "",
        }
        if row.get("description"):
            r["description"] = row.get("description")
        return r

    def _validate_evolution_payload(self, payload):
        """Reject payloads that Evolution API's send* endpoints would reject."""
        if not isinstance(payload, dict):
            frappe.throw(_("Template Payload must be a JSON object"))

        if payload.get("buttons"):
            self._validate_buttons_component(payload["buttons"])
        if payload.get("list"):
            self._validate_list_component(payload["list"])
        if payload.get("carousel"):
            self._validate_carousel_component(payload["carousel"])

    def _validate_buttons_component(self, payload):
        buttons = payload.get("buttons")
        if not isinstance(buttons, list) or not buttons:
            frappe.throw(_("Buttons payload requires a non-empty \"buttons\" array"))
        types = [b.get("type") for b in buttons if isinstance(b, dict)]
        invalid = [t for t in types if t not in ("reply", "copy", "url", "call", "pix")]
        if invalid:
            frappe.throw(_("Invalid button type(s) in buttons payload: {0}. Valid types: reply, copy, url, call, pix").format(", ".join(map(str, invalid))))
        has_reply = "reply" in types
        has_cta = any(t in ("url", "call", "copy") for t in types)
        has_pix = "pix" in types
        if has_reply and (has_cta or has_pix):
            frappe.throw(_("Reply buttons cannot be mixed with CTA (url/call/copy) or PIX buttons in the same message"))
        if has_reply and len(buttons) > 3:
            frappe.throw(_("Maximum of 3 reply buttons allowed"))
        if has_pix and len(buttons) > 1:
            frappe.throw(_("Only one PIX button is allowed and it cannot be mixed with other button types"))
        if has_cta and not has_reply and len(buttons) > 2:
            frappe.throw(_("Maximum of 2 CTA (url/call/copy) buttons allowed"))

    def _validate_list_component(self, payload):
        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            frappe.throw(_("List payload requires a non-empty \"sections\" array"))
        titles = [s.get("title") for s in sections if isinstance(s, dict)]
        if len(titles) != len(set(titles)):
            frappe.throw(_("Section titles cannot be repeated in List payload"))
        for s in sections:
            if not isinstance(s.get("rows"), list) or not s.get("rows"):
                frappe.throw(_("Section \"{0}\" requires at least one row").format(s.get("title")))

    def _validate_carousel_component(self, payload):
        cards = payload.get("cards")
        if not isinstance(cards, list) or not cards or len(cards) > 10:
            frappe.throw(_("Carousel payload requires 1 to 10 cards"))
        for card in cards:
            if not isinstance(card, dict) or not card.get("body"):
                frappe.throw(_("Each carousel card requires a non-empty \"body\""))
            if not card.get("imageUrl"):
                frappe.throw(_("Each carousel card requires an Image URL — WhatsApp carousel cards must include media (image or video)"))
            card_label = card.get("title") or card.get("body")
            card_buttons = card.get("buttons")
            if not isinstance(card_buttons, list) or not card_buttons or len(card_buttons) > 3:
                frappe.throw(_("Card \"{0}\" has no buttons — set its Button Type and Display Text (or add buttons to the JSON)").format(card_label))
            for b in card_buttons:
                if not isinstance(b, dict) or b.get("type") not in ("reply", "copy", "url", "call"):
                    frappe.throw(_("Card \"{0}\": button types must be reply, copy, url or call (PIX is not supported in carousel cards)").format(card_label))

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            default_whatsapp_account = get_whatsapp_account()
            if not default_whatsapp_account:
                throw(_("Please set a default outgoing WhatsApp Account or Select available WhatsApp Account"))
            else:
                self.whatsapp_account = default_whatsapp_account.name

    def get_session_id(self, file):
        """Upload media."""
        self.get_settings()

        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_type = remote_file_data['file_type']
            file_length = remote_file_data['file_size']
        else:
            file_content = self._read_local_file(file)
            mime = magic.Magic(mime=True)
            file_type = mime.from_buffer(file_content)
            file_length = len(file_content)

        payload = {
            'file_length': file_length,
            'file_type': file_type,
            'messaging_product': 'whatsapp'
        }

        response = make_post_request(
            f"{self._url}/{self._version}/{self._app_id}/uploads",
            headers=self._headers,
            data=json.loads(json.dumps(payload))
        )
        self._session_id = response['id']

    def _prepare_remote_file(self, file_url):
        """Download and return remote file content from URL."""
        try:
            response = requests.get(file_url, timeout=30)
            response.raise_for_status()
            
            file_content = response.content
            file_size = len(file_content)
            
            # Get MIME type from Content-Type header or detect from content
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip()
            if content_type:
                file_type = content_type
            else:
                # Fallback to magic detection from content
                mime = magic.Magic(mime=True)
                file_type = mime.from_buffer(file_content)
            
            return {
                'file_content': file_content,
                'file_size': file_size,
                'file_type': file_type
            }
        except Exception as e:
            frappe.throw(f"Failed to download file from URL: {str(e)}")

    def get_media_id(self, file):
        self.get_settings()

        headers = {
                "authorization": f"OAuth {self._token}"
            }
        
        # Check if it's a remote file, load data accordingly
        if file.startswith(('http://', 'https://')):
            remote_file_data = self._prepare_remote_file(file)
            file_content = remote_file_data['file_content']
        else:
            file_content = self._read_local_file(file)

        payload = file_content
        response = make_post_request(
            f"{self._url}/{self._version}/{self._session_id}",
            headers=headers,
            data=payload
        )

        self._media_id = response['h']

    def _read_local_file(self, file_url):
        # Routed through File so path resolution stays inside Frappe's
        # vetted file handling — never feed a raw URL to open().
        return frappe.get_doc("File", {"file_url": file_url}).get_content()


    def after_insert(self):  # nosemgrep: frappe-modifying-but-not-committing -- self.actual_name/id/status are persisted via self.db_update() after the Meta round-trip; the static check can't trace through the API call
        # actual_name / id / status are persisted via self.db_update() below
        # after the Meta round-trip; the static check can't tell that call.
        if self.template_name:
            self.actual_name = self.template_name.lower().replace(" ", "_")  # nosemgrep: frappe-modifying-but-not-committing

        # Evolution interactive templates are sent directly via Evolution endpoints
        # and do not need to be registered on Meta. Skip Meta settings/API calls.
        template_type = self.get("template_type") or "Meta"
        if template_type != "Meta":
            self.id = ""
            # Usable immediately (no Meta approval needed); CRM only lists
            # templates with status APPROVED.
            self.status = "APPROVED"
            self.db_update()
            return

        self.get_settings()

        data = {
            "name": self.actual_name,
            "language": self.language_code,
            "category": self.category,
            "components": [],
        }

        body = {
            "type": "BODY",
            "text": self.template,
        }
        if self.sample_values:
            body.update({"example": {"body_text": [self.sample_values.split(",")]}})

        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())

        # add footer
        if self.footer:
            data["components"].append({"type": "FOOTER", "text": self.footer})

        # add buttons
        if self.buttons:
            button_block = {"type": "BUTTONS", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"
                elif btn.button_type == "Multi-Product Message":
                    b["type"] = "MPM"
                elif btn.button_type == "Catalog":
                    b["type"] = "CATALOG"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            response = make_post_request(
                f"{self._url}/{self._version}/{self._business_id}/message_templates",
                headers=self._headers,
                data=json.dumps(data),
            )
            self.id = response["id"]  # nosemgrep: frappe-modifying-but-not-committing
            self.status = response["status"]  # nosemgrep: frappe-modifying-but-not-committing
            self.db_update()
        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("error_user_msg", res.get("message"))
            frappe.throw(
                msg=error_message,
                title=res.get("error_user_title", "Error"),
            )
            self.id = response["id"]  # nosemgrep: frappe-modifying-but-not-committing
            self.status = response["status"]  # nosemgrep: frappe-modifying-but-not-committing
            self.db_update()
        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("error_user_msg", res.get("message"))
            frappe.throw(
                msg=error_message,
                title=res.get("error_user_title", "Error"),
            )

    def update_template(self):
        """Update template to meta."""
        template_type = self.get("template_type") or "Meta"
        if template_type != "Meta":
            return

        self.get_settings()
        data = {"components": []}

        body = {
            "type": "BODY",
            "text": self.template,
        }
        if self.sample_values:
            body.update({"example": {"body_text": [self.sample_values.split(",")]}})
        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())
        if self.footer:
            data["components"].append({"type": "FOOTER", "text": self.footer})
        if self.buttons:
            button_block = {"type": "BUTTONS", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"
                elif btn.button_type == "Multi-Product Message":
                    b["type"] = "MPM"
                    # MPM buttons often require additional fields like catalog_id
                elif btn.button_type == "Catalog":
                    b["type"] = "CATALOG"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            # post template to meta for update
            make_post_request(
                f"{self._url}/{self._version}/{self.id}",
                headers=self._headers,
                data=json.dumps(data),
            )
        except Exception as e:
            raise e
            # res = frappe.flags.integration_request.json()['error']
            # frappe.throw(
            #     msg=res.get('error_user_msg', res.get("message")),
            #     title=res.get("error_user_title", "Error"),
            # )

    def get_settings(self):
        """Get whatsapp settings."""
        # Underscore-prefixed attributes below are in-memory scratch for the
        # outbound HTTP call — they are not DocType fields and must not be
        # persisted. Semgrep's static check can't tell the difference.
        settings = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        self._token = settings.get_password("token")  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._url = settings.url  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._version = settings.version  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._business_id = settings.business_id  # nosemgrep: frappe-modifying-but-not-committing-other-method
        self._app_id = settings.app_id  # nosemgrep: frappe-modifying-but-not-committing-other-method

        self._headers = {  # nosemgrep: frappe-modifying-but-not-committing-other-method
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }

    def on_trash(self):
        template_type = self.get("template_type") or "Meta"
        if template_type != "Meta":
            return
        self.get_settings()
        url = f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}"
        try:
            make_request("DELETE", url, headers=self._headers)
        except Exception:
            res = frappe.flags.integration_request.json().get("error", {})
            if res.get("error_user_title") == "Message Template Not Found":
                frappe.msgprint(
                    "Deleted locally", res.get("error_user_title", "Error"), alert=True
                )
            else:
                frappe.throw(
                    msg=res.get("error_user_msg"),
                    title=res.get("error_user_title", "Error"),
                )

    def get_header(self):
        """Get header format."""
        header = {"type": "header", "format": self.header_type}
        if self.header_type == "TEXT":
            header["text"] = self.header
            if self.sample:
                samples = self.sample.split(", ")
                header.update({"example": {"header_text": samples}})
        else:
            pdf_link = ''
            if not self.sample:
                key = frappe.get_doc(self.doctype, self.name).get_document_share_key()
                link = get_pdf_link(self.doctype, self.name)
                pdf_link = f"{frappe.utils.get_url()}{link}&key={key}"
            header.update({"example": {"header_handle": [self._media_id]}})

        return header

@frappe.whitelist()
def fetch():
    """Fetch templates from meta."""
    """Later improve this code to pass a whatsapp account remove the js funcation so that it is called from whatsapp account doctype """
    whatsapp_accounts = frappe.get_all('WhatsApp Account', filters={'status': 'Active'}, fields=['name', 'token', 'url', 'version', 'business_id'])

    for account in whatsapp_accounts:
        # get credentials
        token = frappe.get_doc("WhatsApp Account", account.name).get_password("token")
        url = account.url
        version = account.version
        business_id = account.business_id

        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

        try:
            response = make_request(
                "GET",
                f"{url}/{version}/{business_id}/message_templates",
                headers=headers,
            )

            for template in response["data"]:
                # set flag to insert or update
                flags = 1
                if frappe.db.exists("WhatsApp Templates", {"actual_name": template["name"]}):
                    doc = frappe.get_doc("WhatsApp Templates", {"actual_name": template["name"]})
                else:
                    flags = 0
                    doc = frappe.new_doc("WhatsApp Templates")
                    doc.template_name = template["name"]
                    doc.actual_name = template["name"]

                doc.status = template["status"]
                doc.language_code = template["language"]
                doc.category = template["category"]
                doc.id = template["id"]
                doc.whatsapp_account = account.name

                # update components
                for component in template["components"]:

                    # update header
                    if component["type"] == "HEADER":
                        doc.header_type = component["format"]

                        # if format is text update sample text
                        if component["format"] == "TEXT":
                            doc.header = component["text"]
                    # Update footer text
                    elif component["type"] == "FOOTER":
                        doc.footer = component["text"]

                    # update template text
                    elif component["type"] == "BODY":
                        doc.template = component["text"]
                        if component.get("example"):
                            # Check if 'body_text' exists before trying to access it
                            if component["example"].get("body_text"):
                                doc.sample_values = ",".join(
                                    component["example"]["body_text"][0]
                                )

                    # Update buttons
                    elif component["type"] == "BUTTONS":
                        doc.set("buttons", [])
                        frappe.db.delete("WhatsApp Button", {"parent": doc.name, "parenttype": "WhatsApp Templates"})
                        typeMap = {
                            "URL": "Visit Website",
                            "PHONE_NUMBER": "Call Phone",
                            "QUICK_REPLY": "Quick Reply",
                            "FLOW": "Flow",
                            "MPM": "Multi-Product Message",
                            "CATALOG": "Catalog"
                        }

                        for i, button in enumerate(component.get("buttons", []), start=1):
                            btn_type_raw = button.get("type")
                            if btn_type_raw not in typeMap:
                                frappe.log_error("WhatsApp Fetch Error", f"Unknown WhatsApp Button Type: {btn_type_raw}")
                                continue

                            btn = {}
                            btn["button_type"] = typeMap[button["type"]]
                            btn["button_label"] = button.get("text")
                            btn["sequence"] = i

                            if button["type"] == "URL":
                                btn["website_url"] = button.get("url")
                                if "{{" in btn["website_url"]:
                                    btn["url_type"] = "Dynamic"
                                else:
                                    btn["url_type"] = "Static"

                                if button.get("example"):
                                    btn["example_url"] = ",".join(button["example"])
                            elif button["type"] == "PHONE_NUMBER":
                                btn["phone_number"] = button.get("phone_number")
                            elif button["type"] == "FLOW":
                                btn["flow"] = button.get("flow")

                            doc.append("buttons", btn)

                upsert_doc_without_hooks(doc, "WhatsApp Button", "buttons")

            return "Successfully fetched templates from meta"

        except Exception as e:
            # Check if frappe.flags.integration_request is set and has a .json() method
            if hasattr(frappe.flags.integration_request, 'json'):
                try:
                    res = frappe.flags.integration_request.json().get("error", {})
                    error_message = res.get("error_user_msg", res.get("message"))
                    frappe.throw(
                        msg=error_message,
                        title=res.get("error_user_title", "Error"),
                    )
                except (json.JSONDecodeError, KeyError):
                    # Handle cases where the response is not valid JSON or lacks the 'error' key
                    frappe.throw(f"An unexpected error occurred while fetching templates: {e}")
            else:
                # Handle cases where frappe.flags.integration_request doesn't exist or isn't a proper response object
                frappe.throw(f"An unexpected server error occurred: {e}")

@frappe.whitelist()
def field_names_builder_data(for_doctype):
    """Return the field tree of a doctype for the Field Names builder dialog."""
    if not frappe.db.exists("DocType", for_doctype):
        frappe.throw(_("DocType {0} does not exist").format(for_doctype))

    SKIP = {
        "Section Break", "Column Break", "Tab Break", "Button",
        "HTML", "Image", "Heading", "Read Only",
    }

    def clean_fields(meta):
        return [
            {
                "fieldname": df.fieldname,
                "label": df.label or df.fieldname,
                "fieldtype": df.fieldtype,
                "options": df.options,
            }
            for df in meta.fields
            if df.fieldname and df.fieldtype not in SKIP
        ]

    meta = frappe.get_meta(for_doctype)
    fields = []
    for df in meta.fields:
        if not df.fieldname or df.fieldtype in SKIP:
            continue
        entry = {
            "fieldname": df.fieldname,
            "label": df.label or df.fieldname,
            "fieldtype": df.fieldtype,
            "options": df.options,
        }
        if df.fieldtype == "Table" and df.options:
            child_meta = frappe.get_meta(df.options)
            entry["child_doctype"] = df.options
            entry["child_fields"] = clean_fields(child_meta)
        fields.append(entry)
    return fields


@frappe.whitelist()
def field_path_values(for_doctype, fieldname, child_doctype=None, txt=None):
    """Distinct existing values of a field, for the Where Value autocomplete."""
    doctype = child_doctype or for_doctype
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("DocType {0} does not exist").format(doctype))

    table = frappe.qb.DocType(doctype)
    column = frappe.qb.Field(fieldname)
    query = frappe.qb.from_(table).select(column).distinct().where(column.isnotnull())
    if txt:
        query = query.where(column.like("%{0}%".format(txt)))
    rows = query.limit(25).run()
    return [r[0] for r in rows]


@frappe.whitelist()
def preview_field_path(for_doctype, path):
    """Resolve a field path against the latest record of the doctype (or the reference record).

    Returns ``{"value": ..., "record": ..., "error": ...}`` for the
    builder's live preview. ``path`` may be any supported field path,
    e.g. ``items[0].rate``, ``Item Price[item_code=ELEC-001].price_list_rate``
    or ``=literal``.
    """
    if not frappe.db.exists("DocType", for_doctype):
        frappe.throw(_("DocType {0} does not exist").format(for_doctype))

    record = frappe.db.get_value(for_doctype, filters={}, fieldname="name", order_by="modified desc")
    if not record:
        return {"value": None, "record": None, "error": _("No records found for {0}").format(for_doctype)}
    try:
        ref_doc = frappe.get_doc(for_doctype, record)
        wm = frappe.new_doc("WhatsApp Message")
        value = wm._resolve_field_path(ref_doc, path)
        return {"value": _json_safe(value), "record": record}
    except Exception as e:
        return {"value": None, "record": record, "error": str(e)}


def _json_safe(value):
    """Make resolver values JSON-safe for the whitelist response."""
    if isinstance(value, frappe.model.document.Document):
        return {"__doc": value.name}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@frappe.whitelist()
def build_payload(doc):
    """Build the Evolution template payload JSON from the builder fields.

    ``doc`` is the unsaved form document (fields only). Returns the
    generated JSON string, ready to be stored in ``template_payload``.
    """
    if isinstance(doc, str):
        doc = json.loads(doc)

    d = frappe.new_doc("WhatsApp Templates")
    for key, value in (doc or {}).items():
        if key in {"doctype", "name", "__islocal", "__onload", "__unsaved"}:
            continue
        if key in {"evo_buttons", "evo_sections", "evo_cards", "evo_section_rows"}:
            continue
        try:
            setattr(d, key, value)
        except Exception:
            pass

    d.set("evo_buttons", [frappe._dict(r) for r in (doc or {}).get("evo_buttons") or []])
    d.set("evo_sections", [frappe._dict(r) for r in (doc or {}).get("evo_sections") or []])
    d.set("evo_cards", [frappe._dict(r) for r in (doc or {}).get("evo_cards") or []])
    d.set("evo_section_rows", [frappe._dict(r) for r in (doc or {}).get("evo_section_rows") or []])

    payload = d._build_evolution_payload()
    if not payload:
        frappe.throw(_("Enable at least one component (Send Buttons / Send List / Send Carousel) and fill its fields"))
    d._validate_evolution_payload(payload)
    return json.dumps(payload, indent=2)

def upsert_doc_without_hooks(doc, child_dt, child_field):
    """Insert or update a parent document and its children without hooks."""
    if frappe.db.exists(doc.doctype, doc.name):
        doc.db_update()
        frappe.db.delete(child_dt, {"parent": doc.name, "parenttype": doc.doctype})
    else:
        doc.db_insert()
    for d in doc.get(child_field):
        d.parent = doc.name
        d.parenttype = doc.doctype
        d.parentfield = child_field
        d.db_insert()
