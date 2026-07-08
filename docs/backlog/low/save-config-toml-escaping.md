# Escape values written by save_config_value

## Summary
`save_config_value` (`src/moneta/config.py`) writes config values into TOML
using a raw f-string with no escaping. A value containing a `"` or a
backslash produces invalid or misinterpreted TOML, and the next
`load_settings()` call can fail to parse the config file or silently
misread the value.

## Context
```python
lines = [f'{k} = "{v}"' for k, v in values.items()]
```
This is the only write path into `config.toml` today (used by
`moneta setup simplefin <token>` to persist `simplefin_access_url`, and
generically by any future caller of `save_config_value`). A SimpleFIN
access URL is unlikely to contain a quote in practice, but the function is
a general key/value writer with no guard, so any future caller (or an
unusual bridge URL) can produce a corrupt config file that
`tomllib.loads` in `_read_config_file` raises on for *every* subsequent
`moneta` invocation — a config value can brick the CLI until the user
manually edits/deletes `config.toml`.

## Acceptance criteria
- `save_config_value` escapes `"` and `\` (and rejects/escapes embedded
  newlines) so any string value round-trips through
  write → `tomllib.loads` correctly.
- Test: save a value containing a `"` and a backslash, reload settings,
  assert the value comes back unchanged.
