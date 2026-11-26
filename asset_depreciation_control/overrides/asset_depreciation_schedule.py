import frappe
from frappe import _

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	AssetDepreciationSchedule,
)

from asset_depreciation_control import schedule_rules as rules
from asset_depreciation_control.overrides.asset import HOLD_FLAG


class ControlledAssetDepreciationSchedule(AssetDepreciationSchedule):
	def onload(self):
		self.set_onload("asset_docstatus", rules.get_asset_docstatus(self.asset))

	@frappe.whitelist()
	def submit(self):
		# Called by ERPNext from within Asset.on_submit: keep the schedule in Draft.
		if self.docstatus == 0 and frappe.flags.get(HOLD_FLAG) == self.asset:
			return
		return super().submit()

	def _validate_links(self):
		# Frappe refuses links to cancelled documents before any validate hook runs,
		# so stale Journal Entry links have to be dropped here (amend, or an entry
		# cancelled while the schedule was still a draft).
		if self.docstatus == 0 or (
			not self.is_new() and frappe.db.get_value(self.doctype, self.name, "docstatus") == 0
		):
			rules.clear_cancelled_journal_entries(self)
		super()._validate_links()

	def validate(self):
		super().validate()

		# A schedule inserted already submitted is ERPNext replacing one after a
		# repair, value adjustment, split, disposal or shift allocation.
		if self.is_new() and self.docstatus == 1:
			return

		# While the Asset is in Draft, ERPNext regenerates the schedule itself.
		if rules.get_asset_docstatus(self.asset) == 1:
			rules.validate_draft(self)

	def before_submit(self):
		if not self.is_new() and rules.get_asset_docstatus(self.asset) == 1:
			rules.validate_before_submit(self)

	def update_shift_depr_schedule(self):
		"""Recalculate a shift-based draft only when a shift was changed.

		ERPNext recalculates on every save, which would discard edited amounts.
		"""
		before = self.get_doc_before_save()
		if before and self.docstatus == 0 and rules.get_asset_docstatus(self.asset) == 1:
			old_shifts = {row.name: row.shift for row in before.get("depreciation_schedule")}
			if not any(
				row.name in old_shifts and row.shift != old_shifts[row.name]
				for row in self.get("depreciation_schedule")
			):
				return

		super().update_shift_depr_schedule()

	def validate_update_after_submit(self):
		# Deliberately ignores `flags.ignore_validate_update_after_submit`.
		rules.validate_locked(self)
		super().validate_update_after_submit()

	def before_cancel(self):
		# Cancelling writes the whole document, so it must match what is stored.
		rules.validate_locked(self, fieldnames=rules.SOURCE_FIELDS)

	def on_trash(self):
		posted = rules.get_posted_rows(self.get("depreciation_schedule"))
		if self.docstatus == 0 and posted:
			frappe.throw(
				_("This schedule has posted depreciation ({0}) and cannot be deleted.").format(
					", ".join(row.journal_entry for row in posted)
				)
			)

	def before_rename(self, old, new, merge=False):
		if self.docstatus != 0:
			rules.throw_locked(_("It cannot be renamed."))
