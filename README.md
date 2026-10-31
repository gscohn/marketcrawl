# marketcrawl

A Bluesky bot that DMs you the closing prices of the stocks and indexes you
follow, shortly after the US stock market closes.

```
Fri Oct 30 close

📉 S&P 500 7,580.07 -1.82%
📉 AAPL 344.63 -1.23%
📈 BRK-B 542.49 +0.10%
```

DM it `add AAPL`, `remove AAPL`, `list` or `help`. It sends nothing on
weekends and market holidays.

marketcrawl is a fork of [sportscrawl](https://github.com/cruftbox/sportscrawl)
by cruftbox, which sends daily sports scores the same way. Almost everything
that makes it reliable comes from there: the scheduler, retries, the
once-per-day bookkeeping and the DM handling. marketcrawl adds a Yahoo Finance
source, an NYSE calendar, a prices formatter and the ticker commands. The
original's spec is in `sportscrawl-spec.md`.

**Status:** working locally and printing to the terminal; not yet deployed as
a bot.

- [How it works, explained simply](docs/HOW-IT-WORKS.md), including how to run it
- License: MIT, same as the original (see `LICENSE`)
