// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Account", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.api_type === "Evolution API") {
			frm.add_custom_button(__("Test Connection"), () => {
				frm.call({
					doc: frm.doc,
					method: "test_connection",
					freeze: true,
					freeze_message: __("Testing Evolution API connection..."),
					callback: (r) => {
						if (!r.exc) {
							frm.events.show_connection_state(frm, r.message);
						}
					},
				});
			});

			frm.add_custom_button(__("Reconnect Instance"), () => {
				frm.events.reconnect_instance(frm);
			});

			frm.add_custom_button(__("Get QR Code"), () => {
				frm.events.get_qr_code(frm);
			});

			frm.add_custom_button(__("Configure Webhook"), () => {
				frm.call({
					doc: frm.doc,
					method: "configure_webhook",
					freeze: true,
					freeze_message: __("Configuring Evolution API webhook..."),
					callback: (r) => {
						if (!r.exc) {
							frappe.show_alert({
								message: __("Webhook configured on Evolution API"),
								indicator: "green",
							});
							frm.reload_doc();
						}
					},
				});
			});
		} else {
			frm.add_custom_button(__("Subscribe App to Webhooks"), () => {
				frappe.confirm(
					__("Subscribe this app to webhooks for WhatsApp Business Account {0}?", [
						frm.doc.business_id || frm.doc.account_name,
					]),
					() => {
						frm.call({
							doc: frm.doc,
							method: "subscribe_app",
							freeze: true,
							freeze_message: __("Subscribing app to webhooks..."),
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("App subscribed to webhooks"),
										indicator: "green",
									});
								}
							},
						});
					}
				);
			});
		}
	},

	show_connection_state(frm, m) {
		m = m || {};
		if (m.connected) {
			frappe.show_alert({
				message: __("Connection OK — instance <strong>{0}</strong> is open.", [m.instance_name || ""]),
				indicator: "green",
			});
			return;
		}

		const state = m.state || "unknown";
		const indicator = (state === "connecting" || state === "qr") ? "orange" : "red";
		const d = new frappe.ui.Dialog({
			title: __("Connection State"),
			fields: [
				{
					fieldname: "state_html",
					fieldtype: "HTML",
					options:
						`<p>${__("Instance <strong>{0}</strong> is not connected.", [frappe.utils.escape_html(m.instance_name || "")])}</p>` +
						`<p>${__("Current state: <span class=\"indicator {0}\">{1}</span>", [indicator, frappe.utils.escape_html(state)])}</p>` +
						`<p class="text-muted">${__("Reconnect tries to restore the saved WhatsApp session. If pairing is required, a QR code will be shown.")}</p>`,
				},
			],
			primary_action_label: __("Reconnect"),
			primary_action: () => {
				d.hide();
				frm.events.reconnect_instance(frm);
			},
		});
		d.add_custom_action(__("Get QR Code"), () => {
			d.hide();
			frm.events.get_qr_code(frm);
		});
		d.show();
	},

	reconnect_instance(frm) {
		frm.call({
			doc: frm.doc,
			method: "reconnect_instance",
			freeze: true,
			freeze_message: __("Reconnecting Evolution API instance..."),
			callback: (r) => {
				if (!r.exc) {
					const m = r.message || {};
					if (m.connected) {
						frappe.show_alert({
							message: __("Instance <strong>{0}</strong> reconnected — state is open.", [m.instance_name || ""]),
							indicator: "green",
						});
					} else if (m.qr && m.qr.base64) {
						frm.events.show_qr_code(m.qr);
					} else {
						frappe.show_alert({
							message: __("Reconnect did not restore the connection (state: {0}).", [m.state || "unknown"]),
							indicator: "orange",
						});
					}
				}
			},
		});
	},

	get_qr_code(frm) {
		frm.call({
			doc: frm.doc,
			method: "get_qr_code",
			freeze: true,
			freeze_message: __("Fetching QR code from Evolution API..."),
			callback: (r) => {
				if (!r.exc) {
					const qr = r.message || {};
					if (qr.base64) {
						frm.events.show_qr_code(qr);
					} else {
						frappe.show_alert({
							message: __("Instance is already connected — no QR code needed."),
							indicator: "green",
						});
					}
				}
			},
		});
	},

	show_qr_code(qr) {
		const imgSrc = qr.base64.startsWith("data:") ? qr.base64 : `data:image/png;base64,${qr.base64}`;
		let html = `<div style="text-align:center;">
			<p>${__("Scan this QR code with your WhatsApp app to connect.")}</p>
			<img src="${imgSrc}" style="max-width:300px;width:100%;border:1px solid #ddd;border-radius:4px;" />`;
		if (qr.pairingCode) {
			html += `<p style="margin-top:12px;">${__("Pairing Code:")} <strong>${frappe.utils.escape_html(qr.pairingCode)}</strong></p>`;
		}
		html += `</div>`;

		frappe.msgprint({
			title: __("Scan QR Code"),
			message: html,
			wide: false,
		});
	},
});
