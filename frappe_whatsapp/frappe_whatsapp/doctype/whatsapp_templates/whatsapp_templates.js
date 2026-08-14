// Copyright (c) 2022, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.listview_settings['WhatsApp Templates'] = {
	onload(listview) {
		listview.page.add_action_item(__('New Meta Template'), () => {
			frappe.new_doc('WhatsApp Templates', {
				template_type: 'Meta'
			});
		});
		listview.page.add_action_item(__('New Revolution Template'), () => {
			frappe.new_doc('WhatsApp Templates', {
				template_type: 'Revolution'
			});
		});
	}
};

frappe.ui.form.on('WhatsApp Templates', {
	build_payload(frm) {
		if (frm.doc.template_type !== 'Revolution') {
			frappe.msgprint(__('Template Type must be Revolution to build a payload'));
			return;
		}
		const problems = precheck_builder(frm);
		if (problems.length) {
			frappe.msgprint({
				title: __('Cannot build payload'),
				message: problems.join('<br>'),
				indicator: 'orange'
			});
			return;
		}
		frappe.call({
			method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.build_payload',
			args: { doc: frm.doc },
			callback(r) {
				if (r.message) {
					frm.set_value('template_payload', r.message);
					frm.refresh_field('template_payload');
					const built = JSON.parse(r.message);
					const buttons = ((built.buttons || {}).buttons || []).length;
					const sections = ((built.list || {}).sections || []).length;
					const cards = ((built.carousel || {}).cards || []).length;
					const indicator = buttons || cards ? 'green' : 'orange';
					frappe.show_alert({
						message: __('Built: {0} buttons, {1} sections, {2} cards', [buttons, sections, cards]),
						indicator
					});
				}
			}
		});
	},
	refresh(frm) {
		refresh_section_options(frm);
		refresh_field_names_preview(frm);
	},
	build_field_names(frm) {
		open_field_names_builder(frm);
	},
	field_names(frm) {
		refresh_field_names_preview(frm);
	}
});

function splitFieldNames(value) {
	if ((value || '').includes('\n')) {
		return value.split('\n').map(s => s.trim()).filter(Boolean);
	}
	const parts = [];
	let current = '';
	let depth = 0;
	for (const ch of (value || '')) {
		if (ch === '[') depth += 1;
		else if (ch === ']') depth = Math.max(0, depth - 1);
		if (ch === ',' && depth === 0) {
			const part = current.trim();
			if (part) parts.push(part);
			current = '';
		} else {
			current += ch;
		}
	}
	const last = current.trim();
	if (last) parts.push(last);
	return parts;
}

function escapeHtml(str) {
	return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function refresh_field_names_preview(frm) {
	const df = frm.fields_dict.field_names_preview;
	if (!df) return;
	const paths = splitFieldNames(frm.doc.field_names);
	const html = paths.length
		? `<div style="max-height:240px;overflow:auto;font-family:var(--font-family-monospace);font-size:12px;">` +
			paths.map((p, i) =>
				`<div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border-color);">` +
				`<span style="min-width:36px;color:var(--text-muted);white-space:nowrap;">&#123;&#123;${i + 1}&#125;&#125;</span>` +
				`<span style="word-break:break-all;">${escapeHtml(p)}</span>` +
				`</div>`
			).join('') + `</div>`
		: `<div class="text-muted small">No field names yet — use the Build Field Names button.</div>`;
	frm.set_df_property('field_names_preview', 'options', html);
	frm.refresh_field('field_names_preview');
}

function open_field_names_builder(frm) {
	const forDoctype = frm.doc.for_doctype;
	const fieldsCache = {};
	const fieldBy = (dt, fieldname) => (fieldsCache[dt] || []).find(f => f.fieldname === fieldname);
	const fieldOptions = list => list.map(f => ({ label: `${f.label} (${f.fieldname})`, value: f.fieldname }));

	const d = new frappe.ui.Dialog({
		title: __('Build Field Names'),
		size: 'large',
		fields: [
			{
				fieldname: 'source_doctype',
				fieldtype: 'Link',
				options: 'DocType',
				label: __('DocType'),
				reqd: 1,
				description: __('The For DocType, or any other doctype to look up records from it (e.g. Item Price)'),
				change() {
					const dt = d.get_value('source_doctype');
					if (!dt) return;
					loadFields(dt, () => {
						const df = d.fields_dict.field;
						df.df.hidden = false;
						df.df.options = fieldOptions(fieldsCache[dt] || []);
						d.set_value('field', null);
						d.refresh();
						updateGenerated();
					});
				}
			},
			{
				fieldname: 'field',
				fieldtype: 'Select',
				label: __('Field'),
				options: [],
				hidden: 1,
				change() {
					onFieldChange();
				}
			},
			{
				fieldname: 'child_field',
				fieldtype: 'Select',
				label: __('Child Field'),
				options: [],
				hidden: 1,
				change() {
					updateGenerated();
				}
			},
			{
				fieldname: 'row_mode',
				fieldtype: 'Select',
				label: __('Row Mode'),
				options: [],
				hidden: 1,
				change() {
					onModeChange();
				}
			},
			{
				fieldname: 'where_field',
				fieldtype: 'Select',
				label: __('Where Field'),
				options: [],
				hidden: 1,
				change() {
					d.set_value('where_value', '');
					d.set_value('where_ref_field', '');
					onModeChange();
				}
			},
			{
				fieldname: 'where_ref_field',
				fieldtype: 'Select',
				label: __('Use Reference Field'),
				options: [],
				hidden: 1,
				description: __('Take the value from the reference record instead of searching'),
				change() {
					const val = d.get_value('where_ref_field');
					d.set_value('where_value', val ? `{${val}}` : '');
					updateGenerated();
				}
			},
			{
				fieldname: 'where_value',
				fieldtype: 'Autocomplete',
				label: __('Where Value'),
				hidden: 1,
				ignore_validation: 1,
				description: __('Search existing values from the database, or type {fieldname} to take it from the reference record'),
				get_query() {
					const wf = currentWhereField();
					const f = currentField();
					return {
						query: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.field_path_values',
						params: {
							for_doctype: d.get_value('source_doctype'),
							fieldname: wf ? wf.fieldname : '',
							child_doctype: (f && f.fieldtype === 'Table') ? (f.child_doctype || '') : ''
						}
					};
				},
				change() {
					updateGenerated();
				}
			},
			{ fieldname: 'generated_code', fieldtype: 'Data', label: __('Generated Code'), read_only: 1 },
			{ fieldname: 'preview_out', fieldtype: 'HTML', label: __('Result Preview'), options: '<span class="text-muted">—</span>' },
			{
				fieldname: 'add_btn',
				fieldtype: 'Button',
				label: __('Add to Field Names'),
				click() {
					addToFieldNames();
				}
			}
		],
		primary_action_label: __('Done'),
		primary_action() {
			d.hide();
		}
	});

	function loadFields(dt, cb) {
		if (fieldsCache[dt]) {
			cb();
			return;
		}
		frappe.call({
			method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.field_names_builder_data',
			args: { for_doctype: dt },
			callback(r) {
				fieldsCache[dt] = r.message || [];
				cb();
			}
		});
	}

	function currentField() {
		return fieldBy(d.get_value('source_doctype'), d.get_value('field'));
	}

	function onFieldChange() {
		const dt = d.get_value('source_doctype');
		const f = currentField();
		if (!f) return;
		const isSameDoc = dt === forDoctype;
		const isTable = f.fieldtype === 'Table';
		const childList = f.child_fields || [];

		d.fields_dict.where_field.df.hidden = true;
		d.fields_dict.where_value.df.hidden = true;
		d.fields_dict.child_field.df.options = fieldOptions(childList);

		if (isTable) {
			d.fields_dict.row_mode.df.hidden = false;
			d.fields_dict.row_mode.df.options = isSameDoc
				? ['First Row', 'Row Where', 'Sum All Rows', 'Count Rows']
				: ['Record Where'];
			d.set_value('row_mode', isSameDoc ? 'First Row' : 'Record Where');
			if (childList.length) {
				d.set_value('child_field', childList[0].fieldname);
			}
			d.fields_dict.where_field.df.options = fieldOptions(childList);
		} else {
			d.fields_dict.row_mode.df.hidden = false;
			d.fields_dict.row_mode.df.options = ['Record Where', 'Sum All Rows', 'Count Rows'];
			d.set_value('row_mode', 'Record Where');
			d.fields_dict.where_field.df.options = fieldOptions(fieldsCache[dt] || []);
		}
		d.refresh();
		onModeChange();
	}

	function onModeChange() {
		const f = currentField();
		if (!f) {
			d.refresh();
			return;
		}
		const mode = d.get_value('row_mode');
		const isSameDoc = d.get_value('source_doctype') === forDoctype;
		const needsWhere = mode === 'Row Where' || mode === 'Record Where';
		const needsChild = f.fieldtype === 'Table' && mode !== 'Count Rows';
		d.fields_dict.where_field.df.hidden = !needsWhere;
		d.fields_dict.where_value.df.hidden = !needsWhere;
		d.fields_dict.child_field.df.hidden = !needsChild;
		d.refresh();
		setupWhereValue();
		updateGenerated();
	}

	function currentWhereField() {
		const dt = d.get_value('source_doctype');
		const f = currentField();
		if (!f) return null;
		const list = (f.child_fields && f.child_fields.length) ? f.child_fields : (fieldsCache[dt] || []);
		return list.find(w => w.fieldname === d.get_value('where_field')) || null;
	}

	function setupWhereValue() {
		const wf = currentWhereField();
		const rdf = d.fields_dict.where_ref_field;
		const dt = d.get_value('source_doctype');
		const f = currentField();
		const childDoctype = (f && f.fieldtype === 'Table') ? (f.child_doctype || '') : '';
		const canRef = forDoctype && (fieldsCache[forDoctype] || []).length;
		const wv = d.fields_dict.where_value;

		rdf.df.hidden = !(wf && canRef);
		rdf.df.options = canRef ? fieldOptions(fieldsCache[forDoctype]) : [];
		d.refresh();
		if (!wf) return;

		const isSelect = wf.fieldtype === 'Select' && wf.options && wf.options.includes('\n');
		if (isSelect) {
			wv.set_data(wf.options.split('\n').map(s => s.trim()).filter(Boolean));
		}
	}

	function generateCode() {
		const dt = d.get_value('source_doctype');
		const f = currentField();
		if (!f) return '';
		const mode = d.get_value('row_mode');
		const child = d.get_value('child_field');
		const isSameDoc = dt === forDoctype;
		const whereField = d.get_value('where_field');
		const whereValue = d.get_value('where_value');

		if (f.fieldtype === 'Table') {
			if (mode === 'First Row') return `${f.fieldname}[0].${child}`;
			if (mode === 'Row Where' || mode === 'Record Where') {
				return `${f.fieldname}[${whereField}=${whereValue}].${child}`;
			}
			if (mode === 'Sum All Rows') return `sum(${f.fieldname}[].${child})`;
			if (mode === 'Count Rows') return `count(${f.fieldname}[])`;
		} else {
			if (mode === 'Record Where') return `${dt}[${whereField}=${whereValue}].${f.fieldname}`;
			if (mode === 'Sum All Rows') return `sum(${dt}[].${f.fieldname})`;
			if (mode === 'Count Rows') return `count(${dt}[])`;
			return f.fieldname;
		}
		return '';
	}

	let previewTimer = null;

	function updateGenerated() {
		const code = generateCode();
		const nextNumber = splitFieldNames(frm.doc.field_names).length + 1;
		d.set_value('generated_code', code ? `{{${nextNumber}}}  ${code}` : '');
		setPreviewHtml(code ? '<span class="text-muted">Resolving preview…</span>' : '');
		clearTimeout(previewTimer);
		if (!code) return;
		previewTimer = setTimeout(() => {
			frappe.call({
				method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.preview_field_path',
				args: { for_doctype: forDoctype || d.get_value('source_doctype'), path: code },
				callback(r) {
					const res = r.message || {};
					if (res.error) {
						setPreviewHtml(`<span class="text-danger">✗ ${escapeHtml(res.error)}</span>`);
						return;
					}
					const raw = Array.isArray(res.value) ? JSON.stringify(res.value) : res.value;
					const value = res.value == null ? '(no value)' : String(raw);
					setPreviewHtml(`<b>→</b> ${escapeHtml(value)}`);
				},
				error(r) {
					setPreviewHtml(`<span class="text-danger">✗ ${escapeHtml(r.message || r || 'request failed')}</span>`);
				}
			});
		}, 400);
	}

	function setPreviewHtml(html) {
		const fld = d.fields_dict && d.fields_dict.preview_out;
		if (fld && fld.$wrapper) {
			fld.$wrapper.html(html);
		}
	}

	function addToFieldNames() {
		const code = generateCode();
		if (!code) {
			frappe.show_alert({ message: __('Pick a field first'), indicator: 'orange' });
			return;
		}
		if (!frm.doc.for_doctype && d.get_value('source_doctype')) {
			frm.set_value('for_doctype', d.get_value('source_doctype'));
			frm.refresh_field('for_doctype');
		}
		const current = splitFieldNames(frm.doc.field_names);
		if (current.includes(code)) {
			frappe.show_alert({ message: __('Already in Field Names'), indicator: 'orange' });
			return;
		}
		current.push(code);
		frm.set_value('field_names', current.join('\n'));
		frm.refresh_field('field_names');
		refresh_field_names_preview(frm);
		updateGenerated();
	}

	d.show();
	if (forDoctype) {
		loadFields(forDoctype, () => {
			const df = d.fields_dict.field;
			df.df.hidden = false;
			df.df.options = fieldOptions(fieldsCache[forDoctype] || []);
			d.set_value('field', (fieldsCache[forDoctype] || [])[0] && fieldsCache[forDoctype][0].fieldname);
			d.refresh();
			updateGenerated();
		});
	}
}

function precheck_builder(frm) {
	const problems = [];
	if (frm.doc.evo_enable_buttons && !(frm.doc.evo_buttons || []).length) {
		problems.push(__('Send Buttons is checked but no buttons were added — add at least one button in the Buttons grid'));
	}
	if (frm.doc.evo_enable_list) {
		(frm.doc.evo_sections || []).forEach(section => {
			const rows = (frm.doc.evo_section_rows || []).filter(r => r.section === section.title);
			if (!rows.length) {
				problems.push(__('Send List is checked but section "{0}" has no rows — add rows to it first', [section.title]));
			}
		});
	}
	if (frm.doc.evo_enable_carousel) {
		(frm.doc.evo_cards || []).forEach(card => {
			if (!Object.prototype.hasOwnProperty.call(card, 'button_type')) {
				problems.push(__('The form has outdated card fields — please reload this page.'));
				return;
			}
			const label = card.title || card.body || card.name;
			if (!card.image_url) {
				problems.push(__('Send Carousel is checked but card "{0}" has no Image URL — WhatsApp carousel cards must include media', [label]));
			}
			if (!card.button_type && !card.display_text) {
				problems.push(__('Send Carousel is checked but card "{0}" is missing Button Type and Display Text', [label]));
			} else if (!card.button_type) {
				problems.push(__('Send Carousel is checked but card "{0}" is missing Button Type', [label]));
			} else if (!card.display_text) {
				problems.push(__('Send Carousel is checked but card "{0}" is missing Display Text', [label]));
			}
		});
	}
	return problems;
}

frappe.ui.form.on('WhatsApp Evo Section', {
	evo_sections_add(frm) {
		refresh_section_options(frm);
	},
	evo_sections_remove(frm) {
		refresh_section_options(frm);
	}
});

function refresh_section_options(frm) {
	const grid = frm.fields_dict.evo_section_rows && frm.fields_dict.evo_section_rows.grid;
	if (!grid) return;
	const options = (frm.doc.evo_sections || []).map(s => s.title).join('\n');
	grid.update_docfield_property('section', 'options', options);
	grid.rows.forEach(row => {
		const field = row.fields_dict && row.fields_dict.section;
		if (field && field.set_options) field.set_options(options || null);
	});
}
