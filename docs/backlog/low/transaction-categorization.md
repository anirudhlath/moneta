# Add transaction categorization

## Summary
Design §5 lists a `category` field on `Transaction`, but it is not
implemented in v1 — no `category` column exists on the `Transaction`
model in `src/moneta/models.py`, and no pipeline or view populates or
consumes one. Add it when spending-by-category views are wanted.

## Context
v1's headline goal (design §2) is spending power and cash-flow honesty,
not category-level budgeting — design §3 explicitly lists "budgeting
categories/envelopes" as a v1 non-goal. Categorization was speculatively
named in the schema section but nothing in v1 (power, cashflow, networth,
obligations, recurring) reads or writes a category, so it was correctly
left out of the models rather than added as dead schema.

## Acceptance criteria
- Deferred until a feature needs spending-by-category (e.g. a
  category-breakdown view or budget-envelope tracking).
- When implemented: add `Transaction.category`, a classification pipeline
  (rule-based with LLM fallback, consistent with merchant normalization
  in `src/moneta/pipelines/normalize.py`) that never writes money values
  from LLM output (design §9), and a view/endpoint that consumes it.
