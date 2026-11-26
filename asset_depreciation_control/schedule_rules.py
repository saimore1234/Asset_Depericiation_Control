"""Server-side rules for Asset Depreciation Schedules.

Depreciation amounts are never recalculated here: ERPNext generates them. These
rules only check what a user changed and keep the running total consistent.
"""

import frappe
from frappe import _
from frappe.model import no_value_fields, table_fields
from frappe.utils import cint, cstr, flt, formatdate, getdate

from erpnext.assets.doctype.asset.depreciation import get_acc_frozen_upto

SCHEDULE = "Asset Depreciation Schedule"

# Parent fields that decide how the rows were calculated. They are read-only in the
# form; this keeps them read-only for API calls and scripts as well.
SOURCE_FIELDS = (
	"asset",
	"company",
	"finance_book",
	"finance_book_id",
	"gross_purchase_amount",
	"opening_accumulated_depreciation",
	"opening_number_of_booked_depreciations",
	"depreciation_method",
	"total_number_of_depreciations",
	"frequency_of_depreciation",
	"rate_of_depreciation",
	"expected_value_after_useful_life",
	"daily_prorata_based",
	"shift_based",
)

ROW_FIELDS = (
	"idx",
	"schedule_date",
	"depreciation_amount",
	"accumulated_depreciation_amount",
	"journal_entry",
	"shift",
)


def normalise(doc, fieldname):
	"""Return a value in a form that can be compared with the stored one."""
	value = doc.get(fieldname)
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else ("Int" if fieldname == "idx" else "Data")

	if fieldtype in ("Currency", "Float", "Percent"):
		return flt(value, doc.precision(fieldname))
	if fieldtype in ("Int", "Check"):
		return cint(value)
	if fieldtype == "Date":
		return getdate(value) if value else None
	return cstr(value)


def row_values(row, fields=ROW_FIELDS):
	return tuple(normalise(row, f) for f in fields)


def get_asset_docstatus(asset):
	return frappe.db.get_value("Asset", asset, "docstatus") if asset else None


def get_journal_entry_status(rows):
	"""Map each Journal Entry linked from `rows` to its docstatus."""
	names = list({row.journal_entry for row in rows if row.journal_entry})
	if not names:
		return {}

	return dict(
		frappe.get_all("Journal Entry", filters={"name": ["in", names]}, fields=["name", "docstatus"], as_list=True)
	)


def get_posted_rows(rows):
	"""Rows linked to a draft or submitted Journal Entry."""
	status = get_journal_entry_status(rows)
	return [row for row in rows if row.journal_entry and status.get(row.journal_entry) != 2]


# --- draft rules -------------------------------------------------------------


def validate_draft(doc):
	"""Check a schedule of a submitted Asset while a user edits or submits it."""
	before = doc.get_doc_before_save()
	rows = doc.get("depreciation_schedule")

	# Row order is the order of the list; keep idx in step with it.
	for idx, row in enumerate(rows, start=1):
		row.idx = idx

	if before:
		validate_source_fields(doc, before)

	old_rows = {row.name: row for row in before.get("depreciation_schedule")} if before else {}
	posted_before = get_posted_rows(list(old_rows.values()))

	validate_posted_rows_unchanged(doc, posted_before)
	validate_journal_entry_links(doc, old_rows, is_new=not before)

	posted_names = {row.name for row in get_posted_rows(rows)}
	validate_rows(doc, old_rows, posted_before, posted_names, is_new=not before)
	set_accumulated_depreciation(doc, posted_names)
	validate_depreciable_value(doc, posted_names)


def validate_source_fields(doc, before):
	for fieldname in SOURCE_FIELDS:
		if normalise(doc, fieldname) != normalise(before, fieldname):
			frappe.throw(
				_("{0} comes from the Asset and cannot be changed on the depreciation schedule.").format(
					frappe.bold(_(doc.meta.get_label(fieldname)))
				)
			)


def clear_cancelled_journal_entries(doc):
	"""A cancelled Journal Entry no longer posts its period, so drop the stale link."""
	rows = doc.get("depreciation_schedule")
	status = get_journal_entry_status(rows)

	for row in rows:
		if row.journal_entry and status.get(row.journal_entry) == 2:
			row.journal_entry = None


def validate_posted_rows_unchanged(doc, posted_before):
	current = {row.name: row for row in doc.get("depreciation_schedule")}

	for old in posted_before:
		row = current.get(old.name)
		if not row:
			frappe.throw(
				_("Row {0} dated {1} is posted by Journal Entry {2} and cannot be deleted.").format(
					old.idx, formatdate(old.schedule_date), old.journal_entry
				),
				title=_("Posted Depreciation"),
			)

		if row_values(row) != row_values(old):
			frappe.throw(
				_("Row {0} dated {1} is posted by Journal Entry {2} and cannot be changed or moved.").format(
					old.idx, formatdate(old.schedule_date), old.journal_entry
				),
				title=_("Posted Depreciation"),
			)


def validate_journal_entry_links(doc, old_rows, is_new):
	"""Users cannot attach Journal Entries to rows; only posting does that."""
	for row in doc.get("depreciation_schedule"):
		if not row.journal_entry:
			continue

		if is_new:
			# an amended or duplicated schedule may carry links over from its source
			if not frappe.db.exists(
				"Journal Entry Account",
				{"parent": row.journal_entry, "reference_type": "Asset", "reference_name": doc.asset},
			):
				frappe.throw(
					_("Row {0}: Journal Entry {1} does not belong to Asset {2}.").format(
						row.idx, row.journal_entry, doc.asset
					)
				)
		elif row.name not in old_rows or old_rows[row.name].journal_entry != row.journal_entry:
			frappe.throw(_("Row {0}: a Journal Entry cannot be linked by editing the schedule.").format(row.idx))


def validate_rows(doc, old_rows, posted_before, posted_names, is_new):
	rows = doc.get("depreciation_schedule")
	available_for_use_date = frappe.db.get_value("Asset", doc.asset, "available_for_use_date")
	frozen_upto = get_acc_frozen_upto()
	last_posted_date = max((getdate(r.schedule_date) for r in posted_before), default=None)
	previous_date = None

	for row in rows:
		if not row.schedule_date:
			frappe.throw(_("Row {0}: Schedule Date is required.").format(row.idx))

		date = getdate(row.schedule_date)
		if previous_date and date <= previous_date:
			frappe.throw(
				_("Row {0}: Schedule Date {1} must be after the previous row's date {2}.").format(
					row.idx, formatdate(date), formatdate(previous_date)
				)
			)
		previous_date = date

		if row.name in posted_names:
			continue

		if flt(row.depreciation_amount, row.precision("depreciation_amount")) <= 0:
			frappe.throw(_("Row {0}: Depreciation Amount must be greater than zero.").format(row.idx))

		old = old_rows.get(row.name)
		fields = ("schedule_date", "depreciation_amount")
		if not is_new and old and row_values(old, fields) == row_values(row, fields):
			# untouched rows keep whatever ERPNext generated
			continue

		if available_for_use_date and date < getdate(available_for_use_date):
			frappe.throw(
				_("Row {0}: Schedule Date cannot be before the Available-for-use Date {1}.").format(
					row.idx, formatdate(available_for_use_date)
				)
			)

		if last_posted_date and date <= last_posted_date:
			frappe.throw(
				_("Row {0}: Schedule Date must be after the last posted depreciation on {1}.").format(
					row.idx, formatdate(last_posted_date)
				)
			)

		if frozen_upto and date <= frozen_upto:
			frappe.throw(
				_("Row {0}: accounts are frozen up to {1}.").format(row.idx, formatdate(frozen_upto))
			)


def set_accumulated_depreciation(doc, posted_names):
	"""Accumulated depreciation is derived; posted rows keep their stored value."""
	accumulated = flt(doc.opening_accumulated_depreciation)

	for row in doc.get("depreciation_schedule"):
		if row.name in posted_names:
			accumulated = flt(row.accumulated_depreciation_amount)
			continue

		precision = row.precision("accumulated_depreciation_amount")
		row.depreciation_amount = flt(row.depreciation_amount, row.precision("depreciation_amount"))
		accumulated = flt(accumulated + row.depreciation_amount, precision)
		row.accumulated_depreciation_amount = accumulated


def get_finance_book_row(doc):
	finance_books = frappe.get_all(
		"Asset Finance Book",
		filters={"parenttype": "Asset", "parent": doc.asset},
		fields=["idx", "finance_book", "value_after_depreciation", "expected_value_after_useful_life"],
		order_by="idx",
	)

	for row in finance_books:
		if cint(doc.finance_book_id) == row.idx:
			return row

	for row in finance_books:
		if cstr(row.finance_book) == cstr(doc.finance_book):
			return row

	frappe.throw(_("Asset {0} has no finance book matching this schedule.").format(doc.asset))


def get_remaining_depreciable_value(doc):
	"""What may still be depreciated.

	ERPNext keeps `value_after_depreciation` current: it already reflects opening
	depreciation, submitted depreciation entries, value adjustments and repairs.
	Draft Journal Entries linked to rows are not reflected yet, so deduct them.
	"""
	finance_book = get_finance_book_row(doc)
	rows = doc.get("depreciation_schedule")
	status = get_journal_entry_status(rows)
	in_draft_entries = sum(
		flt(row.depreciation_amount) for row in rows if row.journal_entry and status.get(row.journal_entry) == 0
	)

	return (
		flt(finance_book.value_after_depreciation)
		- flt(finance_book.expected_value_after_useful_life)
		- in_draft_entries
	)


def get_unposted_total(doc, posted_names):
	return sum(
		flt(row.depreciation_amount) for row in doc.get("depreciation_schedule") if row.name not in posted_names
	)


def get_tolerance(doc):
	return 0.5 / (10 ** cint(doc.precision("gross_purchase_amount") or 2))


def validate_depreciable_value(doc, posted_names):
	unposted = get_unposted_total(doc, posted_names)
	remaining = get_remaining_depreciable_value(doc)

	if unposted - remaining > get_tolerance(doc):
		currency = frappe.get_cached_value("Company", doc.company, "default_currency")
		frappe.throw(
			_("Unposted depreciation of {0} exceeds the remaining depreciable value of {1}.").format(
				frappe.format_value(unposted, {"fieldtype": "Currency"}, currency=currency),
				frappe.format_value(remaining, {"fieldtype": "Currency"}, currency=currency),
			),
			title=_("Depreciable Value Exceeded"),
		)


def validate_before_submit(doc):
	if not doc.get("depreciation_schedule"):
		frappe.throw(_("Add at least one depreciation row before submitting."))

	posted_names = {row.name for row in get_posted_rows(doc.get("depreciation_schedule"))}
	shortfall = get_remaining_depreciable_value(doc) - get_unposted_total(doc, posted_names)

	if shortfall > get_tolerance(doc):
		frappe.msgprint(
			_("This schedule leaves {0} of the depreciable value undepreciated.").format(
				flt(shortfall, doc.precision("gross_purchase_amount"))
			),
			indicator="orange",
			alert=True,
		)


# --- submitted lock ----------------------------------------------------------


def throw_locked(detail):
	frappe.throw(
		_("A submitted depreciation schedule is locked. {0}").format(detail),
		frappe.UpdateAfterSubmitError,
		title=_("Cannot Update After Submit"),
	)


def validate_rows_locked(doc, stored):
	"""Reject added, removed, reordered or edited rows."""
	rows = doc.get("depreciation_schedule")
	old_rows = stored.get("depreciation_schedule")

	if [r.name for r in rows] != [r.name for r in old_rows]:
		throw_locked(_("Rows cannot be added, deleted or reordered."))

	for row, old in zip(rows, old_rows, strict=True):
		fields = ROW_FIELDS
		if not row.journal_entry and is_reversed(old.journal_entry):
			fields = tuple(f for f in ROW_FIELDS if f != "journal_entry")

		if row_values(row, fields) != row_values(old, fields):
			throw_locked(_("Row {0} cannot be changed.").format(old.idx))


def is_reversed(journal_entry):
	"""Restoring a disposed Asset reverses an entry, then unlinks it from its row."""
	return bool(
		journal_entry and frappe.db.exists("Journal Entry", {"reversal_of": journal_entry, "docstatus": 1})
	)


def validate_locked(doc, fieldnames=None):
	stored = frappe.get_doc(SCHEDULE, doc.name)

	if fieldnames is None:
		fieldnames = [
			df.fieldname
			for df in doc.meta.fields
			if df.fieldtype not in no_value_fields and df.fieldtype not in table_fields and not df.allow_on_submit
		]

	for fieldname in fieldnames:
		if normalise(doc, fieldname) != normalise(stored, fieldname):
			throw_locked(_("{0} cannot be changed.").format(_(doc.meta.get_label(fieldname))))

	validate_rows_locked(doc, stored)
