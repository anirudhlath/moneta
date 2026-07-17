# Give bill/habit/not_recurring a first-class Classification type

## Summary
The bill/habit/not_recurring concept is spelled five different ways across
the recurring-detection path, each with its own ad hoc conversion at the
boundary: the LLM's JSON string (`"bill" | "habit" | "not_recurring"`), the
CLI's `b/h/n` prompt token, review.py's `{is_recurring, discretionary}`
resolution dict, recurring.py's force-map pairs (now `ForcedAnswer(is_recurring,
discretionary)`), and the stored `RecurringSeries.status` + `.discretionary`
columns. A `Classification` `StrEnum` (`bill`, `habit`, `not_recurring`)
threaded through the force map and the resolution boundary — CLI POSTs
`{"classification": "habit"}`, server owns the sole decode to
`(is_recurring, discretionary)` — would remove the impossible
`(is_recurring=False, discretionary=True)` state the current pair-of-bools
representation allows, and collapse the CLI's and API's duplicate
bill/habit/not_recurring → boolean-pair mapping into one place.

## Context
Surfaced by the simplify pass on `feature/detection-correctness`, 2026-07-16.
That pass extracted the shared `recurring_cluster_item` factory (models.py)
and the shared `CLASSIFICATION_TAXONOMY` prompt text (llm.py) and introduced
`ForcedAnswer` as a `NamedTuple` in recurring.py — both steps toward this
ticket, but stopped short of unifying the *type* the classification travels
as. Today:
- The LLM answers `{"classification": "bill" | "habit" | "not_recurring"}`
  (recurring.py's `_LLM_PROMPT`, review.py's `_VERIFY_PROMPT`).
- `apply_resolution`/`_validated` (review.py) and `/recurring/{id}/habit` +
  `/recurring/{id}/not-a-bill` (api.py) each independently encode the same
  three-way choice as `{"is_recurring": bool, "discretionary": bool}`, which
  admits a fourth, meaningless combination.
- The CLI's `_review_one` (cli/main.py) maps `b/h/n` keystrokes to that same
  bool pair, duplicating the mapping API resolution already needs to make
  sense of.
- `detect_recurring`'s force map (recurring.py) stores `ForcedAnswer` bool
  pairs derived from the resolution dict — one more hop of the same
  conversion.

## Acceptance criteria
- A `Classification` `StrEnum` (`bill`, `habit`, `not_recurring`) lives in
  models.py alongside the other lifecycle enums.
- The CLI POSTs `{"classification": "bill" | "habit" | "not_recurring"}` for
  recurring_cluster resolutions instead of hand-building the bool pair;
  `/recurring/{id}/habit` and `/recurring/{id}/not-a-bill` do the same
  internally.
- The server (review.py's `apply_resolution`/`_validated`) owns the single
  `Classification -> (is_recurring, discretionary)` decode; no other call
  site re-derives it.
- `ForcedAnswer` (or its replacement) cannot represent
  `is_recurring=False, discretionary=True` — either by construction (a
  `Classification` field) or a validated invariant.
- Existing recurring-detection and review-resolution tests are updated to
  the new wire shape rather than deleted; behavior (which series form, which
  become discretionary) is unchanged.
