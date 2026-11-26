// Tell the user when a submitted Asset still has a depreciation schedule in Draft.
frappe.ui.form.on("Asset", {
	async refresh(frm) {
		const schedule = "Asset Depreciation Schedule";
		if (frm.doc.docstatus !== 1 || !frm.doc.calculate_depreciation) return;
		if (!frappe.model.can_read(schedule)) return;

		const drafts = await frappe.db.get_list(schedule, {
			filters: { asset: frm.doc.name, docstatus: 0 },
			fields: ["name"],
		});
		if (!drafts.length) return;

		const links = drafts.map((d) => frappe.utils.get_form_link(schedule, d.name, true)).join(", ");
		frm.dashboard.set_headline(
			__("Depreciation is not active yet. Review and submit the draft schedule: {0}", [links]),
			"orange"
		);
	},
});
