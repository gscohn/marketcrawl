# marketcrawl

A Bluesky bot that DMs you the closing prices of the stocks and indexes you
follow, shortly after the US stock market closes, along with any earnings
releases and quarterly or annual reports those companies filed with the SEC.

```
Fri Oct 30 close

📉 S&P 500 7,580.07 -1.82%
💥 NASDAQ Composite 25,437.00 -2.86%
📉 AAPL 344.63 -1.23% 🔊
📈 BRK-B 542.49 +0.10%

Filings
AAPL earnings release (8-K) https://www.sec.gov/Archives/edgar/data/...
```

📈 up, 📉 down, 🚀/💥 a big day (5% or more; 2% for an index), 🔊 twice the
usual trading volume.

DM it `add AAPL`, `remove AAPL`, `list`, `about` or `help`. It sends nothing
on weekends and market holidays.

## Built on SportsCrawl

marketcrawl is a spin-off of [SportsCrawl](https://github.com/cruftbox/sportscrawl)
by cruftbox ([@sportscrawl.app](https://bsky.app/profile/sportscrawl.app) on
Bluesky), which DMs you last night's scores for your teams every morning. If
you follow sports, go follow it.

Almost everything that makes marketcrawl reliable is SportsCrawl's work,
unchanged: the scheduler, the retries, the "never message anyone twice"
bookkeeping, and the whole Bluesky DM side. marketcrawl adds a Yahoo Finance
price source, an SEC EDGAR filings source, an NYSE calendar, a prices
formatter and the ticker commands, in new files next to the originals. The
original's spec is in `sportscrawl-spec.md`, and it's worth reading: it's a
model of how to think through a small service before building it.

**Status:** working locally, printing to the terminal or sending test DMs;
not yet deployed.

- [How it works, explained simply](docs/HOW-IT-WORKS.md), including how to run it
- License: MIT, same as the original (see `LICENSE`)
