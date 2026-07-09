# Plaid Link update mode for ITEM_LOGIN_REQUIRED

## Summary
Repair a broken item in place instead of unlink + re-link (which creates duplicate
accounts because Plaid account_ids are per-item).

## Context
Hosted Link supports update mode: create a link token with `access_token` set, complete
re-auth in the browser, keep the same item/account ids.

## Acceptance criteria
- `moneta setup plaid-relink <item-id>` runs hosted-link update mode for the item.
- Synced accounts keep their rows (no duplicates).
