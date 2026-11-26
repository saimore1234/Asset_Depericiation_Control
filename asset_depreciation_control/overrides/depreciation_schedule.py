"""Guards for the `Depreciation Schedule` child DocType.

Saving the parent writes rows without running child hooks, so these only fire when
someone saves or deletes a row on its own, which would skip every parent rule.
"""

import frappe
from frappe import _

from asset_depreciation_control.schedule_rules import SCHEDULE, get_asset_docstatus, throw_locked


def prevent_direct_change(doc, method=None):
	parents = {(doc.parenttype, doc.parent)}
	if doc.name:
		stored = frappe.db.get_value(doc.doctype, doc.name, ["parenttype", "parent"])
		if stored:
			parents.add(tuple(stored))

	for parenttype, parent in parents:
		if parenttype != SCHEDULE:
			continue

		schedule = frappe.db.get_value(SCHEDULE, parent, ["docstatus", "asset"], as_dict=True)
		if not schedule:
			continue

		if schedule.docstatus != 0:
			throw_locked(_("Rows of {0} cannot be changed.").format(parent))

		if get_asset_docstatus(schedule.asset) == 1:
			frappe.throw(_("Change depreciation rows by saving {0}, not row by row.").format(parent))
