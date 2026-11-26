# Asset Depreciation Control

Controlled editing of ERPNext **Asset Depreciation Schedules**: the schedule stays in
Draft after the Asset is submitted, can be adjusted, and is locked once submitted.

Developed and tested on Frappe 15.81.0 and ERPNext 15.78.1. It changes no ERPNext or
Frappe file, adds no DocType, custom field or role, and stores no data of its own.

## Why it is needed

In standard ERPNext v15:

1. `Asset.on_submit` calls `convert_draft_asset_depr_schedules_into_active`, which
   submits every draft schedule. After submitting an Asset there is no draft left to edit.
2. The schedule's client script disables the grid unless the method is *Manual* or the
   schedule is shift-based.

## Workflow

1. Create an Asset, configure depreciation and submit it.
2. The depreciation schedule remains **Draft**. A message and a headline on the Asset link to it.
3. Open the schedule. Edit dates and amounts, add or delete rows, and save.
4. Submit the schedule. It becomes **Active** and locked. Depreciation can now be posted.

**Nothing is depreciated while a schedule is Draft.** The daily posting job ignores it and
the manual posting endpoint rejects it. Periods already due are posted on the next run
after you submit. Depreciation reports that read submitted schedules do not show drafts.

Until the schedule is submitted, do not expect to scrap, sell, split, capitalise, repair
with capitalisation, value-adjust or shift-allocate the Asset: ERPNext needs an Active
schedule for these.

While the Asset itself is still a draft, ERPNext regenerates the schedule on each Asset
save, as it does without this app. Edit the schedule after submitting the Asset.

## Rules for a draft schedule (submitted Asset)

| Rule | Detail |
|---|---|
| Dates | Required, strictly increasing in row order. Changed or new rows must not be before the available-for-use date, on or before the last posted period, or in a frozen accounting period. |
| Amounts | Unposted rows must be greater than zero. |
| Depreciable value | The total of unposted rows may not exceed the Asset finance book's `value_after_depreciation - expected_value_after_useful_life`, less any draft Journal Entries linked to rows. |
| Accumulated depreciation | Derived from the amounts on every save. It is not editable. |
| Under-depreciation | Allowed. A notice is shown on submit. |
| Asset values | Gross amount, method, finance book and the other values copied from the Asset cannot be changed. |
| Shift-based schedules | ERPNext recalculates amounts only when you change a shift, which replaces manually edited amounts. |

## Accounting safeguards

* A row linked to a draft or submitted Journal Entry cannot be edited, moved or deleted,
  and its schedule cannot be deleted.
* Journal Entry links cannot be added or changed by editing the schedule.
* If a linked Journal Entry is cancelled, the link is dropped on the next save and the row
  is editable again.
* The app never creates, changes or cancels Journal Entries. ERPNext's own posting and
  cancellation flows do that, unchanged.
* An Asset cannot be cancelled while a draft schedule has a posted row.

## Submitted schedules

Rejected on the server, through the form, REST/RPC APIs and document APIs: changing any
field, adding, deleting, reordering or renaming, including saving or deleting a single
child row. `ignore_permissions`, `ignore_validate` and
`ignore_validate_update_after_submit` do not open the lock. Cancelling a document that was
modified in memory is rejected too.

Cancellation works as standard.

### Correcting a submitted schedule

Cancel it, then Amend, edit and submit.

**Warning: cancelling a schedule makes ERPNext cancel every depreciation Journal Entry
linked to it.** The amended draft shows those periods as unposted, and they are posted
again after you submit. Check with accounts first.

The same applies to Assets submitted before this app was installed: their schedules are
already submitted.

## Permissions

Standard DocType, role, user and company permissions apply. ERPNext gives *Accounts User*
and *Quality Manager* access to Asset Depreciation Schedule. No new role is required.
Anyone with submit permission on the schedule can approve their own edits. If you need a
second approver, add a Frappe Workflow or restrict Submit in Role Permissions Manager.

## Configuration

None. The app is active on every site where it is installed.

## Installation

From the bench directory, with the app folder present in `apps/` and listed in
`sites/apps.txt`:

```bash
bench --site <site> backup
bench --site <site> install-app asset_depreciation_control
bench --site <site> migrate
bench --site <site> clear-cache
bench build --app asset_depreciation_control
bench restart          # production; in development stop and run `bench start`
```

To bring it to another bench, first commit the app to git, then run:

```bash
bench get-app <git-url-of-this-app>
```

followed by the commands above. Try it on a staging site before production.

## Uninstallation

Submit all draft schedules of submitted Assets first (or cancel those Assets). Without the
app nothing submits them for you, and ERPNext's Asset flows expect an Active schedule.

```bash
bench --site <site> uninstall-app asset_depreciation_control
bench --site <site> clear-cache
bench remove-app asset_depreciation_control   # only if no site uses it
bench restart
```

## Technical architecture

| File | Role |
|---|---|
| `schedule_rules.py` | Draft validations and the submitted lock |
| `overrides/asset.py` | Keeps schedules in Draft on Asset submit; guards Asset cancel |
| `overrides/asset_depreciation_schedule.py` | Applies the rules in the document lifecycle |
| `overrides/depreciation_schedule.py` | Blocks direct writes to child rows |
| `overrides/depreciation.py` | Three replaced whitelisted endpoints |
| `public/js/*.js` | Grid behaviour and the Asset headline (convenience only) |
| `tests/` | Automated tests |

### Hooks

* `override_doctype_class`: `Asset`, `Asset Depreciation Schedule`
* `doc_events`: `Depreciation Schedule` (`validate`, `before_update_after_submit`, `on_trash`)
* `override_whitelisted_methods`:
  * `make_depreciation_entry`: refuses draft schedules
  * `split_asset`: clear message when the schedule is still draft
  * `get_asset_depr_schedule_doc`: returns the draft to the Asset form, whose chart
    otherwise fails with no Active schedule
* `doctype_js`: `Asset`, `Asset Depreciation Schedule`

There are no monkey patches. `Asset.on_submit` is not copied: the override sets a
request-local flag, calls the original, and the schedule's `submit()` skips itself while
the flag is set.

## Tests

```bash
bench --site <development-site> set-config allow_tests true
bench --site <development-site> run-tests --app asset_depreciation_control
```

Never run tests on production. The tests create their own company and data inside a
transaction that is rolled back. They need a Fiscal Year covering today.

Covered: draft editing (amount, date, add, delete), derived accumulated depreciation,
depreciable value and salvage, invalid rows, frozen periods, posted-row protection,
submitted locking (save, APIs, child rows, add, delete, reorder, cancel), roles and company
permissions, posting without duplicates, Asset and schedule cancellation, amendment,
scrap, value adjustment, split and each depreciation method.

## Known limitations

* The lock covers document APIs. `frappe.db.set_value`, `db_set`, SQL and console access
  bypass it, as they bypass all Frappe validation.
* A schedule **inserted directly as submitted** is not checked by the draft rules, because
  ERPNext does this after repairs, value adjustments, splits, disposals and shift
  allocations. A user with create and submit permission could do the same through the API,
  but only when the Asset has no other uncancelled schedule for that finance book.
* Total Number of Depreciations keeps the Asset's value when rows are added or removed.
* ERPNext sets *Expected Value After Useful Life* from the generated schedule when it is
  left empty (relevant to reducing-balance methods). That residual stays protected.
* If a later period is posted while an earlier one is not (frozen accounts), review the
  accumulated column of the later row after editing.
* Selling, repair, capitalisation and shift allocation have no dedicated tests here.
* The browser behaviour has no automated tests.
* ERPNext's own Asset tests expect schedules to be submitted with the Asset, so they are
  not expected to pass on a site with this app installed.

## Upgrading

The app depends on these ERPNext internals. Check them and run the tests after upgrading:

* `Asset.on_submit` and `convert_draft_asset_depr_schedules_into_active`
* `AssetDepreciationSchedule.update_shift_depr_schedule`
* `get_acc_frozen_upto`
* the three overridden endpoints and their signatures

Only one app can override a DocType class, so another app overriding `Asset` or
`Asset Depreciation Schedule` would conflict. ERPNext v16 is not supported.

## License

MIT
