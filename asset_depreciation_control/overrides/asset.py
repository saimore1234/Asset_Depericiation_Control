import frappe
from frappe import _
from frappe.utils import get_link_to_form

from erpnext.assets.doctype.asset.asset import Asset

from asset_depreciation_control.schedule_rules import SCHEDULE, get_posted_rows

HOLD_FLAG = "adc_hold_schedules_for_asset"


def get_draft_schedules(asset):
	return frappe.get_all(SCHEDULE, filters={"asset": asset, "docstatus": 0}, pluck="name")


class ControlledAsset(Asset):
	def on_submit(self):
		"""Submit the Asset as usual, but leave its depreciation schedules in Draft.

		ERPNext's `on_submit` submits every draft schedule. The flag makes
		ControlledAssetDepreciationSchedule.submit skip that one call.
		"""
		previous = frappe.flags.get(HOLD_FLAG)
		frappe.flags[HOLD_FLAG] = self.name
		try:
			super().on_submit()
		finally:
			frappe.flags[HOLD_FLAG] = previous

		drafts = get_draft_schedules(self.name) if self.calculate_depreciation else []
		if drafts:
			frappe.msgprint(
				_(
					"Depreciation schedules kept in Draft: {0}. Review and submit them; no depreciation is posted until then."
				).format(", ".join(get_link_to_form(SCHEDULE, name) for name in drafts)),
				indicator="orange",
				title=_("Submit the Depreciation Schedule"),
			)

	def before_cancel(self):
		if hasattr(super(), "before_cancel"):
			super().before_cancel()

		# ERPNext only reverses entries of Active schedules, so a draft one must have none.
		for name in get_draft_schedules(self.name):
			posted = get_posted_rows(frappe.get_doc(SCHEDULE, name).get("depreciation_schedule"))
			if posted:
				frappe.throw(
					_("Cancel Journal Entry {0} of draft schedule {1} before cancelling the Asset.").format(
						", ".join(row.journal_entry for row in posted), get_link_to_form(SCHEDULE, name)
					)
				)
