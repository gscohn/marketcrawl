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

📈 up, 📉 down, 🚀/💥 a big day (5% or more; 2% for an index), 🎩 an
all-time high close, 🕳️ the lowest close in a year, 🔊 twice the usual
trading volume.

DM it `add AAPL`, `remove AAPL`, `list`, `test` (your message now, from the
latest close), `quote AAPL` (the price right now, or the last close when the market's
shut; `quote` alone for everything you follow), `about`
or `help`. It sends nothing on weekends and market holidays.

It also writes one short public post per trading day on
[@marketcrawl.bsky.social](https://bsky.app/profile/marketcrawl.bsky.social):
the three big indexes and the five biggest swings in company value (so
Nvidia falling 4%, about $200 billion, outranks a small company doubling).
`python feed.py preview` shows today's.

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

**Status:** live. DM [@marketcrawl.bsky.social](https://bsky.app/profile/marketcrawl.bsky.social)
`help` to try it.

- [How it works, explained simply](docs/HOW-IT-WORKS.md), including how to run it
- License: MIT, same as the original (see `LICENSE`)
