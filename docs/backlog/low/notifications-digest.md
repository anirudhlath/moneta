# Notifications digest for series events and promo warnings

## Summary
Design doc §11 lists push notifications as "Later". A cheap first version:
a cron-friendly digest (`moneta sync --notify` or `moneta digest`) that pushes
new series events (missed payment, price increase, new subscription) and
promo-expiry / deferred-interest warnings to a notification channel
(ntfy.sh or email).

## Context / motivation
Missed-payment and deferred-interest warnings only exist inside
`moneta recurring --events` / `moneta obligations` output today — they're
useful precisely when the user *doesn't* think to run a command. A digest
after each scheduled sync lands most of the value of push notifications
without a web/mobile frontend.

## Acceptance criteria
- A way to run sync + digest unattended (cron/launchd): new-since-last-digest
  series events and any `deferred_interest_risk` obligations are sent; nothing
  sent when there's nothing new (no noise).
- Channel is pluggable but v1 ships one concrete target (ntfy.sh topic URL in
  config is the cheapest; secrets stay in config/env, never in code).
- "New since last digest" is tracked persistently so repeats aren't re-sent.
- Failures to deliver don't fail the sync itself; they log a warning.
