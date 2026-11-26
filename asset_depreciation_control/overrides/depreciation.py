import frappe
from frappe import _

from erpnext.assets.doctype.asset import asset as erpnext_asset
from erpnext.assets.doctype.asset import depreciation
from erpnext.assets.doctype.asset_depreciation_schedule import (
	asset_depreciation_schedule as erpnext_schedule,
)

from asset_depreciation_control.overrides.asset import get_draft_schedules
from asset_depreciation_control.schedule_rules import SCHEDULE


@frappe.whitelist()
def make_depreciation_entry(asset_depr_schedule_name, date=None):
	"""ERPNext's endpoint does not check the schedule is submitted. Drafts must not post."""
	if frappe.db.get_value(SCHEDULE, asset_depr_schedule_name, "docstatus") != 1:
		frappe.throw(_("Submit {0} before making depreciation entries.").format(asset_depr_schedule_name))

	return depreciation.make_depreciation_entry(asset_depr_schedule_name, date)


@frappe.whitelist()
def split_asset(asset_name, split_qty):
	"""ERPNext splits the Active schedule and fails with a traceback if there is none."""
	drafts = get_draft_schedules(asset_name)
	if drafts and frappe.db.get_value("Asset", asset_name, "docstatus") == 1:
		frappe.throw(_("Submit {0} before splitting the Asset.").format(", ".join(drafts)))

	return erpnext_asset.split_asset(asset_name, split_qty)


@frappe.whitelist()
def get_asset_depr_schedule_doc(asset_name, status, finance_book=None):
	"""The Asset form asks for the Active schedule and breaks if there is none.

	While the schedule of a submitted Asset is still in Draft, return the draft.
	"""
	doc = erpnext_schedule.get_asset_depr_schedule_doc(asset_name, status, finance_book)
	if not doc and status == "Active":
		doc = erpnext_schedule.get_asset_depr_schedule_doc(asset_name, "Draft", finance_book)

	return doc


@frappe.whitelist()
def get_asset_depreciation_schedule(asset_name, status, finance_book=None):
	"""Later ERPNext 15 releases load the Asset form through this endpoint instead."""
	frappe.has_permission("Asset", "read", asset_name, throw=True)
	return get_asset_depr_schedule_doc(asset_name, status, finance_book)
