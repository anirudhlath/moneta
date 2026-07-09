# Brokerage accounts without holdings data are invisible to net worth

## Summary
Net worth counts brokerage value only via `Holding` rows (by design — vested-only RSU rule). On real SimpleFIN data, Fidelity delivers **no holdings** for the two Walmart accounts (RCU ~$15,000, RSU ~$105,452 by balance), so ~$120K of brokerage value contributes nothing to net worth. Robinhood delivers holdings fine.

## Context
Discovered on first real sync (2026-07-09). The design intent (never count unvested) is correct for the RSU account — its raw balance includes unvested shares, which is exactly the Origin/Copilot failure being fixed. But the current vesting CSV import can only UPDATE existing holdings by symbol; it cannot CREATE the missing WMT holding, and the CSV schema (`symbol,vested_quantity,unvested_quantity`) carries no price/market value, so there is no path to get these accounts into net worth at all.

## Options
1. Extend the vesting CSV schema with a `market_value` (or `price`) column and let import create holdings on a named account — vested-only math then works for the RSU account; RCU covered the same way.
2. Balance-fallback: brokerage accounts with zero holdings count their balance as fully vested — simple, but silently wrong for RSU-style accounts (re-introduces the inflation bug for exactly the account that motivated the app).
3. Hybrid: fallback per-account opt-in flag (`moneta accounts --count-balance ID`).

Option 1 (possibly + 3 for the RCU) looks most faithful to the design. Needs a decision.

## Acceptance criteria
- Walmart RSU account contributes vested-only value to net worth after a vesting import
- Walmart RCU account's value appears in net worth
- Raw RSU balance (including unvested) is never summed into net worth
- `unvested_potential` reflects the unvested WMT value
