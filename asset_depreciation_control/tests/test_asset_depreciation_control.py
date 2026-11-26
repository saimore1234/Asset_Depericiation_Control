"""Tests for Asset Depreciation Control.

All data (company, accounts, assets, entries) is created inside the test
transaction and rolled back. Existing companies are never touched.
"""

import frappe
from frappe import client
from frappe.desk.form.save import savedocs
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_months, flt, get_last_day, getdate, today

from erpnext.accounts.utils import get_fiscal_year
from erpnext.assets.doctype.asset.asset import split_asset
from erpnext.assets.doctype.asset.depreciation import (
	get_depreciable_asset_depr_schedules_data,
	make_depreciation_entry,
	scrap_asset,
)

from asset_depreciation_control.overrides.depreciation import (
	make_depreciation_entry as guarded_make_depreciation_entry,
)
from asset_depreciation_control.overrides.depreciation import get_asset_depr_schedule_doc
from asset_depreciation_control.overrides.depreciation import split_asset as guarded_split_asset

SCHEDULE = "Asset Depreciation Schedule"
COMPANY = "_ADC Test Company"
ABBR = "_ADCT"
CATEGORY = "_ADC Computers"
ITEM = "_ADC Laptop"
LOCATION = "_ADC Location"
USER = "adc.accounts.user@example.com"


def make_account(name, root_type, account_type=None):
	existing = frappe.db.exists("Account", {"account_name": name, "company": COMPANY})
	if existing:
		return existing

	root = frappe.db.get_value(
		"Account", {"company": COMPANY, "root_type": root_type, "parent_account": ["is", "not set"]}
	)
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"parent_account": root,
				"company": COMPANY,
				"account_type": account_type,
			}
		)
		.insert()
		.name
	)


def make_fixtures():
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": ABBR,
				"default_currency": "INR",
				"country": "India",
			}
		).insert()

	fixed = make_account("_ADC Fixed Asset", "Asset", "Fixed Asset")
	accumulated = make_account("_ADC Accumulated Depreciation", "Asset", "Accumulated Depreciation")
	expense = make_account("_ADC Depreciation", "Expense", "Depreciation")
	disposal = make_account("_ADC Disposal Gain Loss", "Expense")
	cost_center = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 0})

	company = frappe.get_doc("Company", COMPANY)
	company.accumulated_depreciation_account = accumulated
	company.depreciation_expense_account = expense
	company.disposal_account = disposal
	company.depreciation_cost_center = cost_center
	company.save()

	if not frappe.db.exists("Asset Category", CATEGORY):
		frappe.get_doc(
			{
				"doctype": "Asset Category",
				"asset_category_name": CATEGORY,
				"accounts": [
					{
						"company_name": COMPANY,
						"fixed_asset_account": fixed,
						"accumulated_depreciation_account": accumulated,
						"depreciation_expense_account": expense,
					}
				],
			}
		).insert()

	if not frappe.db.exists("Item", ITEM):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_name": ITEM,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_fixed_asset": 1,
				"asset_category": CATEGORY,
			}
		)
		if item.meta.has_field("gst_hsn_code"):
			item.gst_hsn_code = frappe.db.get_value("GST HSN Code", {"name": ["like", "8471%"]})
		item.insert()

	if not frappe.db.exists("Location", LOCATION):
		frappe.get_doc({"doctype": "Location", "location_name": LOCATION}).insert()


def period_start():
	"""First day of the current fiscal year, so postings fall in an open year."""
	return getdate(get_fiscal_year(today(), company=COMPANY)[1])


def make_asset(submit=True, quantity=1, salvage=0, shift_based=0, periods=6, offset=0, **finance_book):
	start = add_days(period_start(), offset)
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"asset_name": "_ADC Asset",
			"company": COMPANY,
			"item_code": ITEM,
			"asset_category": CATEGORY,
			"location": LOCATION,
			"is_existing_asset": 1,
			"asset_owner": "Company",
			"asset_quantity": quantity,
			"opening_accumulated_depreciation": 0,
			"opening_number_of_booked_depreciations": 0,
			"purchase_date": start,
			"available_for_use_date": start,
			"gross_purchase_amount": 60000,
			"purchase_amount": 60000,
			"calculate_depreciation": 1,
			"finance_books": [
				{
					"depreciation_method": "Straight Line",
					"frequency_of_depreciation": 1,
					"total_number_of_depreciations": periods,
					"depreciation_start_date": get_last_day(start),
					"expected_value_after_useful_life": salvage,
					"shift_based": shift_based,
					"total_number_of_booked_depreciations": 0,
					**finance_book,
				}
			],
		}
	)
	asset.insert()
	if submit:
		asset.submit()
	return asset


def get_schedule(asset, docstatus=0):
	name = frappe.db.get_value(SCHEDULE, {"asset": asset.name, "docstatus": docstatus})
	return frappe.get_doc(SCHEDULE, name) if name else None


def reload(doc):
	return frappe.get_doc(doc.doctype, doc.name)


def amounts(doc):
	return [flt(d.depreciation_amount) for d in doc.depreciation_schedule]


def accumulated(doc):
	return [flt(d.accumulated_depreciation_amount) for d in doc.depreciation_schedule]


def journal_entries(asset):
	return frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Asset", "reference_name": asset.name, "docstatus": 1},
		pluck="parent",
		distinct=True,
	)


class TestAssetDepreciationControl(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		make_fixtures()
		frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", None)

	def setUp(self):
		frappe.set_user("Administrator")
		self.asset = make_asset()
		self.schedule = get_schedule(self.asset)

	def tearDown(self):
		frappe.set_user("Administrator")

	def assertRejected(self, fn, message=None, exception=frappe.ValidationError):
		"""The call must fail and leave nothing behind."""
		frappe.db.savepoint("adc_reject")
		try:
			if message:
				self.assertRaisesRegex(exception, message, fn)
			else:
				self.assertRaises(exception, fn)
		finally:
			frappe.db.rollback(save_point="adc_reject")
			frappe.clear_messages()

	def post_first_period(self):
		"""Post row 1 of the draft schedule through ERPNext's internal function."""
		make_depreciation_entry(self.schedule.name, self.schedule.depreciation_schedule[0].schedule_date)
		return reload(self.schedule)

	def submit_schedule(self):
		self.schedule.submit()
		return reload(self.schedule)

	# --- workflow -------------------------------------------------------------

	def test_asset_submission_keeps_schedule_in_draft(self):
		self.assertEqual(self.asset.docstatus, 1)
		self.assertEqual(self.schedule.docstatus, 0)
		self.assertEqual(self.schedule.status, "Draft")
		self.assertEqual(amounts(self.schedule), [10000] * 6)

	def test_a_edit_amount(self):
		self.schedule.depreciation_schedule[0].depreciation_amount = 8000
		self.schedule.depreciation_schedule[1].depreciation_amount = 12000
		self.schedule.save()
		self.assertEqual(amounts(reload(self.schedule))[:2], [8000, 12000])

	def test_b_edit_date(self):
		new_date = add_days(self.schedule.depreciation_schedule[0].schedule_date, -5)
		self.schedule.depreciation_schedule[0].schedule_date = new_date
		self.schedule.save()
		self.assertEqual(reload(self.schedule).depreciation_schedule[0].schedule_date, getdate(new_date))

	def test_c_add_row(self):
		last = self.schedule.depreciation_schedule[-1]
		last.depreciation_amount = 4000
		date = get_last_day(add_months(last.schedule_date, 1))
		self.schedule.append("depreciation_schedule", {"schedule_date": date, "depreciation_amount": 6000})
		self.schedule.save()

		saved = reload(self.schedule)
		self.assertEqual(len(saved.depreciation_schedule), 7)
		self.assertEqual(saved.depreciation_schedule[-1].schedule_date, date)
		self.assertEqual(accumulated(saved)[-1], 60000)

	def test_d_delete_row(self):
		self.schedule.remove(self.schedule.depreciation_schedule[2])
		self.schedule.save()

		saved = reload(self.schedule)
		self.assertEqual(len(saved.depreciation_schedule), 5)
		self.assertEqual([d.idx for d in saved.depreciation_schedule], [1, 2, 3, 4, 5])
		self.assertEqual(accumulated(saved), [10000, 20000, 30000, 40000, 50000])

	def test_e_accumulated_depreciation_is_derived(self):
		self.schedule.depreciation_schedule[0].depreciation_amount = 5000
		self.schedule.depreciation_schedule[3].accumulated_depreciation_amount = 999999
		self.schedule.save()
		self.assertEqual(accumulated(reload(self.schedule)), [5000, 15000, 25000, 35000, 45000, 55000])

	def test_f_depreciable_value_cannot_be_exceeded(self):
		self.schedule.depreciation_schedule[0].depreciation_amount = 10001
		self.assertRejected(self.schedule.save, "exceeds the remaining depreciable value")
		self.assertEqual(amounts(reload(self.schedule)), [10000] * 6)

	def test_f_salvage_value_is_respected(self):
		schedule = get_schedule(make_asset(salvage=6000))
		self.assertEqual(sum(amounts(schedule)), 54000)
		schedule.depreciation_schedule[0].depreciation_amount += 100
		self.assertRejected(schedule.save, "exceeds the remaining depreciable value")

	def test_invalid_rows_are_rejected(self):
		for value in (0, -100):
			doc = reload(self.schedule)
			doc.depreciation_schedule[0].depreciation_amount = value
			self.assertRejected(doc.save, "greater than zero")

		doc = reload(self.schedule)
		doc.depreciation_schedule[1].schedule_date = doc.depreciation_schedule[0].schedule_date
		self.assertRejected(doc.save, "must be after the previous row")

		doc = reload(self.schedule)
		doc.depreciation_schedule[0].schedule_date = add_days(period_start(), -1)
		self.assertRejected(doc.save, "Available-for-use")

		doc = reload(self.schedule)
		doc.depreciation_schedule[0].schedule_date = None
		self.assertRejected(doc.save, "Schedule Date is required")

	def test_reorder_in_draft_must_stay_chronological(self):
		rows = self.schedule.depreciation_schedule
		rows[0], rows[1] = rows[1], rows[0]
		self.assertRejected(self.schedule.save, "must be after the previous row")

	def test_asset_values_cannot_be_changed_through_the_schedule(self):
		self.schedule.gross_purchase_amount = 999999
		self.assertRejected(self.schedule.save, "comes from the Asset")

	def test_frozen_period_cannot_be_edited(self):
		frappe.db.set_single_value(
			"Accounts Settings", "acc_frozen_upto", self.schedule.depreciation_schedule[0].schedule_date
		)
		frappe.db.set_single_value("Accounts Settings", "frozen_accounts_modifier", None)
		try:
			self.schedule.depreciation_schedule[0].depreciation_amount = 9000
			self.assertRejected(self.schedule.save, "frozen")
		finally:
			frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", None)

	def test_manual_edits_survive_shift_based_save(self):
		if not frappe.db.exists("Asset Shift Factor", {"default": 1}):
			frappe.get_doc(
				{"doctype": "Asset Shift Factor", "shift_name": "_ADC Single", "shift_factor": 1, "default": 1}
			).insert()

		schedule = get_schedule(make_asset(shift_based=1))
		schedule.depreciation_schedule[0].depreciation_amount = 7000
		schedule.save()
		schedule = reload(schedule)
		schedule.save()
		self.assertEqual(amounts(reload(schedule))[0], 7000)

	# --- posted rows ------------------------------------------------------------

	def test_g_posted_row_is_protected(self):
		schedule = self.post_first_period()
		self.assertTrue(schedule.depreciation_schedule[0].journal_entry)
		entries = journal_entries(self.asset)

		def change(field, value):
			doc = reload(schedule)
			doc.depreciation_schedule[0].set(field, value)
			return doc.save

		first_date = schedule.depreciation_schedule[0].schedule_date
		self.assertRejected(change("depreciation_amount", 1), "cannot be changed")
		self.assertRejected(change("schedule_date", add_days(first_date, -1)), "cannot be changed")
		self.assertRejected(change("journal_entry", None), "cannot be changed")
		self.assertRejected(change("accumulated_depreciation_amount", 1), "cannot be changed")

		doc = reload(schedule)
		doc.remove(doc.depreciation_schedule[0])
		self.assertRejected(doc.save, "cannot be deleted")

		self.assertRejected(reload(schedule).delete, "cannot be deleted")
		self.assertEqual(journal_entries(self.asset), entries)

	def test_g_unposted_rows_stay_editable_after_posting(self):
		schedule = self.post_first_period()
		schedule.depreciation_schedule[1].depreciation_amount = 5000
		schedule.save()
		self.assertEqual(amounts(reload(schedule))[:2], [10000, 5000])

		# the remaining value is 50000 now, not 60000
		schedule = reload(schedule)
		schedule.depreciation_schedule[1].depreciation_amount = 10001
		self.assertRejected(schedule.save, "exceeds the remaining depreciable value")

		# no new period before a posted one
		schedule = reload(schedule)
		schedule.depreciation_schedule[1].schedule_date = add_days(
			schedule.depreciation_schedule[0].schedule_date, -2
		)
		self.assertRejected(schedule.save)

	def test_journal_entry_cannot_be_linked_by_hand(self):
		entry = self.post_first_period().depreciation_schedule[0].journal_entry
		doc = reload(self.schedule)
		doc.depreciation_schedule[1].journal_entry = entry
		self.assertRejected(doc.save, "cannot be linked")

	def test_cancelled_journal_entry_releases_the_row(self):
		entry = self.post_first_period().depreciation_schedule[0].journal_entry
		frappe.get_doc("Journal Entry", entry).cancel()

		doc = reload(self.schedule)
		doc.depreciation_schedule[0].depreciation_amount = 9000
		doc.save()
		self.assertFalse(reload(doc).depreciation_schedule[0].journal_entry)

	def test_asset_cancel_blocked_while_draft_has_posted_rows(self):
		self.post_first_period()
		self.assertRejected(reload(self.asset).cancel, "before cancelling the Asset")

	def test_draft_schedule_does_not_post(self):
		self.assertRejected(lambda: guarded_make_depreciation_entry(self.schedule.name, today()), "Submit")
		frappe.db.set_single_value("Accounts Settings", "book_asset_depreciation_entry_automatically", 1)
		due = [d.schedule_date for d in self.schedule.depreciation_schedule][-1]
		self.assertNotIn(self.schedule.name, [r[0] for r in get_depreciable_asset_depr_schedules_data(due)])

	def test_endpoint_override_is_registered(self):
		self.assertEqual(
			frappe.get_hooks("override_whitelisted_methods")[
				"erpnext.assets.doctype.asset.depreciation.make_depreciation_entry"
			][-1],
			"asset_depreciation_control.overrides.depreciation.make_depreciation_entry",
		)

	# --- submitted lock -----------------------------------------------------------

	def assertLocked(self, mutate, **flags):
		before = reload(self.schedule)
		doc = reload(self.schedule)
		mutate(doc)
		doc.flags.update(flags)
		self.assertRejected(doc.save)

		after = reload(self.schedule)
		self.assertEqual(
			[(d.name, d.idx, d.schedule_date, d.depreciation_amount) for d in after.depreciation_schedule],
			[(d.name, d.idx, d.schedule_date, d.depreciation_amount) for d in before.depreciation_schedule],
		)

	def test_h_submitted_schedule_is_locked(self):
		doc = self.submit_schedule()
		self.assertEqual((doc.docstatus, doc.status), (1, "Active"))

		def amount(d):
			d.depreciation_schedule[0].depreciation_amount = 1

		def date(d):
			d.depreciation_schedule[0].schedule_date = add_days(d.depreciation_schedule[0].schedule_date, -1)

		def total(d):
			d.depreciation_schedule[0].accumulated_depreciation_amount = 1

		def parent(d):
			d.gross_purchase_amount = 1

		for mutate in (amount, date, total, parent):
			self.assertLocked(mutate)

		# flags that skip Frappe's own check do not open the lock
		self.assertLocked(amount, ignore_validate_update_after_submit=True)
		self.assertLocked(amount, ignore_validate=True, ignore_validate_update_after_submit=True)
		self.assertLocked(amount, ignore_permissions=True)

	def test_h_reorder_is_rejected(self):
		self.submit_schedule()

		def swap(d):
			first, second = d.depreciation_schedule[0], d.depreciation_schedule[1]
			first.idx, second.idx = 2, 1

		self.assertLocked(swap)

	def test_i_server_apis_cannot_modify(self):
		doc = self.submit_schedule()
		row = doc.depreciation_schedule[0]

		self.assertRejected(lambda: client.set_value("Depreciation Schedule", row.name, "depreciation_amount", 1))
		self.assertRejected(lambda: client.set_value(SCHEDULE, doc.name, "gross_purchase_amount", 1))

		payload = reload(doc).as_dict()
		payload["depreciation_schedule"][0]["depreciation_amount"] = 1
		self.assertRejected(lambda: client.save(frappe.as_json(payload)))
		self.assertRejected(lambda: savedocs(frappe.as_json(payload), "Update"))

		def save_row_directly():
			child = frappe.get_doc("Depreciation Schedule", row.name)
			child.depreciation_amount = 1
			child.save()

		self.assertRejected(save_row_directly, "locked")

		def move_row_directly():
			child = frappe.get_doc("Depreciation Schedule", row.name)
			child.idx = 6
			child.save()

		self.assertRejected(move_row_directly, "locked")
		self.assertRejected(lambda: client.delete("Depreciation Schedule", row.name))
		self.assertEqual(amounts(reload(doc)), [10000] * 6)

	def test_j_row_addition_is_rejected(self):
		doc = self.submit_schedule()
		values = {"schedule_date": add_months(period_start(), 8), "depreciation_amount": 1}

		self.assertLocked(lambda d: d.append("depreciation_schedule", dict(values)))

		child = {
			"doctype": "Depreciation Schedule",
			"parent": doc.name,
			"parenttype": SCHEDULE,
			"parentfield": "depreciation_schedule",
			**values,
		}
		self.assertRejected(lambda: frappe.get_doc(dict(child)).insert())
		self.assertRejected(lambda: client.insert(frappe.as_json(child)))
		self.assertEqual(len(reload(doc).depreciation_schedule), 6)

	def test_k_row_deletion_is_rejected(self):
		doc = self.submit_schedule()
		self.assertLocked(lambda d: d.remove(d.depreciation_schedule[-1]))
		self.assertRejected(
			lambda: frappe.delete_doc("Depreciation Schedule", doc.depreciation_schedule[-1].name)
		)
		self.assertRejected(doc.delete)
		self.assertEqual(len(reload(doc).depreciation_schedule), 6)

	def test_cancel_cannot_smuggle_in_changes(self):
		self.submit_schedule()
		doc = reload(self.schedule)
		doc.depreciation_schedule[0].depreciation_amount = 1
		self.assertRejected(doc.cancel, "locked")
		self.assertEqual(reload(doc).docstatus, 1)

	# --- permissions ------------------------------------------------------------

	def make_user(self, roles):
		if frappe.db.exists("User", USER):
			user = frappe.get_doc("User", USER)
		else:
			user = frappe.get_doc(
				{"doctype": "User", "email": USER, "first_name": "ADC", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		user.set("roles", [{"role": r} for r in roles])
		user.save(ignore_permissions=True)
		frappe.clear_cache(user=USER)

	def test_accounts_user_can_edit_and_submit(self):
		self.make_user(["Accounts User"])
		frappe.set_user(USER)
		doc = reload(self.schedule)
		doc.depreciation_schedule[0].depreciation_amount = 9000
		doc.save()
		doc.submit()
		self.assertEqual(reload(doc).docstatus, 1)

	def test_user_without_role_cannot_edit(self):
		self.make_user(["Stock User"])
		frappe.set_user(USER)
		doc = reload(self.schedule)
		doc.depreciation_schedule[0].depreciation_amount = 9000
		self.assertRejected(doc.save, exception=frappe.PermissionError)

	def test_company_user_permission_is_respected(self):
		self.make_user(["Accounts User"])
		other = frappe.db.get_value("Company", {"name": ["!=", COMPANY]})
		if not other:
			self.skipTest("needs a second company")
		frappe.permissions.add_user_permission("Company", other, USER, ignore_permissions=True)
		frappe.set_user(USER)
		doc = reload(self.schedule)
		doc.depreciation_schedule[0].depreciation_amount = 9000
		try:
			self.assertRejected(doc.save, exception=frappe.PermissionError)
		finally:
			frappe.set_user("Administrator")
			frappe.permissions.remove_user_permission("Company", other, USER)

	# --- regression ---------------------------------------------------------------

	def test_l_post_submitted_schedule_once(self):
		self.schedule.depreciation_schedule[0].depreciation_amount = 8000
		self.schedule.save()
		doc = self.submit_schedule()
		date = doc.depreciation_schedule[1].schedule_date

		make_depreciation_entry(doc.name, date)
		make_depreciation_entry(doc.name, date)

		doc = reload(doc)
		self.assertEqual(len(journal_entries(self.asset)), 2)
		self.assertEqual([bool(d.journal_entry) for d in doc.depreciation_schedule], [True, True] + [False] * 4)
		asset = reload(self.asset)
		self.assertEqual(flt(asset.finance_books[0].value_after_depreciation), 42000)
		self.assertEqual(asset.status, "Partially Depreciated")

	def test_l_cancel_asset_reverses_entries(self):
		doc = self.submit_schedule()
		guarded_make_depreciation_entry(doc.name, doc.depreciation_schedule[0].schedule_date)
		entry = reload(doc).depreciation_schedule[0].journal_entry

		reload(self.asset).cancel()

		self.assertEqual(reload(self.asset).docstatus, 2)
		self.assertEqual(reload(doc).docstatus, 2)
		self.assertEqual(frappe.db.get_value("Journal Entry", entry, "docstatus"), 2)

	def test_l_cancel_asset_with_draft_schedule(self):
		reload(self.asset).cancel()
		self.assertEqual(reload(self.asset).docstatus, 2)
		self.assertRejected(reload(self.schedule).save, "cancelled")

	def test_l_cancel_and_amend_schedule(self):
		doc = self.submit_schedule()
		make_depreciation_entry(doc.name, doc.depreciation_schedule[0].schedule_date)
		reload(doc).cancel()
		self.assertEqual(journal_entries(self.asset), [])

		amended = frappe.copy_doc(reload(doc))
		amended.amended_from = doc.name
		amended.docstatus = 0
		amended.insert()
		self.assertFalse(amended.depreciation_schedule[0].journal_entry)

		amended.depreciation_schedule[0].depreciation_amount = 9000
		amended.save()
		amended.submit()
		self.assertEqual(reload(amended).status, "Active")

	def test_l_asset_in_draft_behaves_as_standard(self):
		asset = make_asset(submit=False)
		asset.finance_books[0].total_number_of_depreciations = 3
		asset.save()
		self.assertEqual(amounts(get_schedule(asset)), [20000] * 3)

	def test_l_disposal_needs_a_submitted_schedule(self):
		self.assertRejected(lambda: scrap_asset(self.asset.name), "Schedule not found")

	def test_l_scrap_after_submitting_schedule(self):
		self.submit_schedule()
		scrap_asset(self.asset.name)
		self.assertEqual(reload(self.asset).status, "Scrapped")
		self.assertIsNotNone(get_schedule(self.asset, docstatus=1))

	def test_l_value_adjustment_replaces_the_schedule(self):
		old = self.submit_schedule()
		adjustment = frappe.get_doc(
			{
				"doctype": "Asset Value Adjustment",
				"asset": self.asset.name,
				"company": COMPANY,
				"date": today(),
				"new_asset_value": 48000,
				"difference_account": frappe.db.get_value(
					"Account", {"account_name": "_ADC Disposal Gain Loss", "company": COMPANY}
				),
				"cost_center": frappe.db.get_value("Company", COMPANY, "depreciation_cost_center"),
			}
		).insert()
		adjustment.submit()

		self.assertEqual(reload(old).docstatus, 2)
		new = get_schedule(self.asset, docstatus=1)
		self.assertAlmostEqual(sum(amounts(new)), 48000, places=2)

	def test_l_split_asset(self):
		asset = make_asset(quantity=2)
		get_schedule(asset).submit()
		new_asset = split_asset(asset.name, 1)
		self.assertEqual(sum(amounts(get_schedule(asset, docstatus=1))), 30000)
		self.assertEqual(sum(amounts(get_schedule(new_asset, docstatus=1))), 30000)

	def test_l_automatic_posting_job_picks_up_submitted_schedule(self):
		doc = self.submit_schedule()
		due = get_depreciable_asset_depr_schedules_data(doc.depreciation_schedule[-1].schedule_date)
		self.assertIn(doc.name, [r[0] for r in due])

	def test_l_split_needs_a_submitted_schedule(self):
		asset = make_asset(quantity=2)
		self.assertRejected(lambda: guarded_split_asset(asset.name, 1), "before splitting")

	def test_l_generated_schedules_pass_validation_unchanged(self):
		cases = (
			{"depreciation_method": "Straight Line"},
			{"depreciation_method": "Straight Line", "daily_prorata_based": 1},
			{"depreciation_method": "Double Declining Balance"},
			{"depreciation_method": "Written Down Value", "rate_of_depreciation": 40},
			{"depreciation_method": "Manual"},
		)
		for finance_book in cases:
			with self.subTest(**finance_book):
				# bought mid-month, so the first and last periods are pro-rated
				schedule = get_schedule(make_asset(offset=14, salvage=6000, **finance_book))
				generated = amounts(schedule)
				schedule.save()
				schedule = reload(schedule)
				self.assertEqual(amounts(schedule), generated)
				schedule.submit()
				self.assertEqual(reload(schedule).docstatus, 1)

	def test_l_asset_form_gets_the_draft_schedule(self):
		self.assertEqual(get_asset_depr_schedule_doc(self.asset.name, "Active").name, self.schedule.name)
		self.submit_schedule()
		self.assertEqual(get_asset_depr_schedule_doc(self.asset.name, "Active").docstatus, 1)

	def test_l_overrides_are_registered(self):
		self.assertEqual(type(self.asset).__name__, "ControlledAsset")
		self.assertEqual(type(self.schedule).__name__, "ControlledAssetDepreciationSchedule")
		overrides = frappe.get_hooks("override_whitelisted_methods")
		for method in (
			"erpnext.assets.doctype.asset.asset.split_asset",
			"erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule.get_asset_depr_schedule_doc",
		):
			self.assertTrue(overrides[method][-1].startswith("asset_depreciation_control."))
