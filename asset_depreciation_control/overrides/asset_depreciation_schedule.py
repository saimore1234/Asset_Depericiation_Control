import frappe
from frappe import _
from frappe.utils import get_link_to_form

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

		asset_docstatus = rules.get_asset_docstatus(self.asset)
		if asset_docstatus == 1:
			rules.validate_draft(self)
		elif asset_docstatus == 0 and self.get_doc_before_save() and not self.flags.adc_from_asset:
			# a user editing the schedule, not ERPNext saving it along with the Asset
			rules.validate_before_asset_submit(self)

	def before_submit(self):
		if not self.is_new() and rules.get_asset_docstatus(self.asset) == 1:
			rules.validate_before_submit(self)

	def prepare_draft_asset_depr_schedule_data(
		self,
		asset_doc,
		row,
		date_of_disposal=None,
		date_of_return=None,
		update_asset_finance_book_row=True,
	):
		"""ERPNext calls this on every save of a draft Asset, including the one made by Submit,
		and rebuilds every schedule that is not Manual. Keep the rows instead, unless something
		they were calculated from has changed.
		"""
		args = (asset_doc, row, date_of_disposal, date_of_return, update_asset_finance_book_row)
		if self.is_new() or self.docstatus != 0 or date_of_disposal or date_of_return:
			return super().prepare_draft_asset_depr_schedule_data(*args)

		self.flags.adc_from_asset = True
		rows = self.get("depreciation_schedule")
		if rows and not rules.calculation_inputs_changed(self, asset_doc, row):
			self.flags.adc_keep_rows = True
			self.set_draft_asset_depr_schedule_details(asset_doc, row)
			return

		old = rules.dates_and_amounts(rows)
		super().prepare_draft_asset_depr_schedule_data(*args)

		if old and rules.dates_and_amounts(self.get("depreciation_schedule")) != old:
			frappe.msgprint(
				_(
					"{0} was recalculated because the depreciation details of the Asset changed. Any manual changes to it were replaced."
				).format(get_link_to_form(self.doctype, self.name)),
				indicator="orange",
				alert=True,
			)

	def update_shift_depr_schedule(self):
		"""Recalculate a shift-based draft only when a shift was changed.

		ERPNext recalculates on every save, which would discard edited amounts.
		"""
		if self.flags.adc_keep_rows:
			return

		before = self.get_doc_before_save()
		if (
			before
			and self.docstatus == 0
			and not self.flags.adc_from_asset
			and rules.get_asset_docstatus(self.asset) in (0, 1)
			and not rules.shifts_changed(self, before)
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
