# marketcrawl: notes for coding agents

A fork of cruftbox/sportscrawl that sends US market closing prices instead of
sports scores. Read `docs/HOW-IT-WORKS.md` first.

## Rules
- **Leave upstream files alone where possible.** New behaviour goes in new
  files (`sportscrawl/market_*.py`, `yahoo.py`, `nyse_calendar.py`,
  `*_market.py`, `dev_dm.py`). Changes to upstream files should be small,
  optional, and default to the original behaviour, so `git pull upstream main`
  stays easy. Right now only `scheduler.py` and `http.py` differ from upstream.
- **Comment new code for a beginner (ELI5):** say what a piece does and why
  in plain words. Match the original author's comment voice when editing
  their files.
- **Keep the formatter, parsers and calendar pure** (no network, database or
  clock), and test them against saved fixtures in `tests/fixtures/`.
- **One runtime dependency (`requests`).** Don't add pandas, yfinance or
  similar without a strong reason.
- **No personal information in this repo:** no local paths, hostnames,
  emails, keys or notes about anyone's own setup. Secrets go in `.env`
  (gitignored) or the host's secret store.

## Commands
- Tests: `.venv/bin/python -m pytest`
- Replay recent closes: `.venv/bin/python replay_market.py`
- Pretend DM: `.venv/bin/python dev_dm.py "add AAPL"`
- One scheduler tick: `.venv/bin/python run_market.py --once`
