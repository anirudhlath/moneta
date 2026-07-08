# Real LLM classification with MONETA_LLM_MODEL set

**Feature:** `LiteLLMClassifier` (`src/moneta/llm.py`) against a real provider
**Priority:** high
**Type:** integration

## Prerequisites
- A real LiteLLM-supported model string and valid credentials for that provider (e.g. `MONETA_LLM_MODEL=openai/gpt-4o-mini` plus `OPENAI_API_KEY`, per LiteLLM's provider docs).

## Test Steps
1. `export MONETA_LLM_MODEL=<real model string>` and set the provider's API key env var.
2. `uv run moneta sync` against real data containing dirty/unrecognized descriptors (ones that fail `looks_clean()` in `normalize.py` — heavy on digits, cryptic abbreviations) and ambiguous transfer pairs (two same-amount candidates within the 6-day window).
3. `uv run moneta review` — inspect the resulting `merchant` and `transfer_pair` resolutions; confirm the LLM produced sensible canonical merchant names (title case, no leftover store numbers) and correct transfer-pair picks rather than hallucinated matches.
4. Check that an irregular/variable-amount recurring cluster gets a reasonable `is_recurring` judgment.
5. Negative-path test: set `MONETA_LLM_MODEL` to a real-looking but invalid model string, or intentionally break the API key, then re-run `uv run moneta sync`. Confirm sync does **not** crash — ambiguous items should degrade to the review queue via the `except Exception` branch in `LiteLLMClassifier.classify_json`.
6. Check logs during step 5 for the loguru warning `"LLM classification failed: {}"` — it's the only place this failure is currently visible; nothing surfaces in the CLI output.

## Expected Result
A real LLM improves merchant normalization and transfer disambiguation quality over the rule-only path. On provider error or bad credentials, `moneta sync` still completes successfully — items land in the review queue instead of crashing the sync.

## Notes
- SDD ledger (Task 5): "LiteLLMClassifier error path + build_classifier untested (all tests use FakeLLM); degrade-to-review contract unverified for real classifier" — this test case is the first real verification of that contract.
- Also Task 5: "normalize.py accepts empty-string LLM merchant silently (worth `.strip()` guard)" — watch for a blank merchant landing on a transaction if the model returns `""`.
- Every uncached descriptor and every ambiguous cluster/pair is a live, paid API call — budget accordingly when testing against a large real history.
