// UI only. Every rule here is also enforced on the server.
(function () {
	const TABLE = "depreciation_schedule";

	function asset_is_submitted(frm) {
		return frm.doc.__onload && frm.doc.__onload.asset_docstatus === 1;
	}

	function is_editable_draft(frm) {
		return frm.doc.docstatus === 0 && asset_is_submitted(frm);
	}

	function set_grid_state(frm) {
		const locked = frm.doc.docstatus > 0;
		// For a draft Asset, leave ERPNext's own behaviour in place.
		if (!locked && !asset_is_submitted(frm)) return;

		const grid = frm.fields_dict[TABLE].grid;
		frm.set_df_property(TABLE, "read_only", locked ? 1 : 0);
		grid.df.cannot_add_rows = locked ? 1 : 0;
		grid.df.cannot_delete_rows = locked ? 1 : 0;
		grid.cannot_add_rows = locked;
		grid.toggle_enable("schedule_date", !locked);
		grid.toggle_enable("depreciation_amount", !locked);
		if (frm.doc.shift_based) grid.toggle_enable("shift", !locked);
		frm.refresh_field(TABLE);

		if (!locked) {
			(grid.grid_rows || [])
				.filter((row) => row.doc && row.doc.journal_entry)
				.forEach((row) => {
					["schedule_date", "depreciation_amount", "shift"].forEach((field) =>
						row.toggle_editable(field, false)
					);
				});
		}
	}

	function set_accumulated_depreciation(frm) {
		if (!is_editable_draft(frm)) return;

		let accumulated = flt(frm.doc.opening_accumulated_depreciation);
		(frm.doc[TABLE] || []).forEach((row) => {
			if (row.journal_entry) {
				accumulated = flt(row.accumulated_depreciation_amount);
				return;
			}
			accumulated = flt(
				accumulated + flt(row.depreciation_amount),
				precision("accumulated_depreciation_amount", row)
			);
			row.accumulated_depreciation_amount = accumulated;
		});
		frm.refresh_field(TABLE);
	}

	frappe.ui.form.on("Asset Depreciation Schedule", {
		refresh(frm) {
			set_grid_state(frm);

			if (is_editable_draft(frm)) {
				frm.set_intro(
					__(
						"Draft: you can edit, add and delete unposted rows. No depreciation is posted until this schedule is submitted."
					),
					"orange"
				);
			}
		},
	});

	frappe.ui.form.on("Depreciation Schedule", {
		depreciation_amount: set_accumulated_depreciation,
		depreciation_schedule_remove: set_accumulated_depreciation,
		depreciation_schedule_move: set_accumulated_depreciation,

		before_depreciation_schedule_remove(frm, cdt, cdn) {
			if (frm.doctype !== "Asset Depreciation Schedule") return;

			const row = locals[cdt][cdn];
			if (row.journal_entry) {
				frappe.throw(__("Row {0} is posted and cannot be deleted.", [row.idx]));
			}
		},
	});
})();
