# How it works, explained simply

marketcrawl is a fork of [sportscrawl](https://github.com/cruftbox/sportscrawl),
a Bluesky bot that DMs people the scores for the teams they follow once a day.
marketcrawl keeps almost all of that machinery and changes three things:
**what it fetches** (stock prices and SEC filings instead of scores), **when
it sends** (after the stock market closes instead of at a time you pick), and
**how you sign up** (DM commands instead of a web page).

This page walks through the whole thing, original parts first.

## The big picture

Think of the bot as a small post office with two workers sharing one filing
cabinet.

```
            ┌──────────────── the filing cabinet ────────────────┐
            │  SQLite database: who follows what, who got what   │
            └───────▲────────────────────────────────▲───────────┘
                    │                                │
   Worker 1: the mail room                Worker 2: the front desk
   (scheduler: run_market.py)             (poller + commands)
   every minute: is anyone's              every few seconds: any new
   message due? if so, fetch              DMs? if so, work out what
   prices, write it, send it              they want and reply
                    │                                │
                    ▼                                ▼
              Yahoo Finance                    Bluesky DMs
```

- **The filing cabinet** is one SQLite file. SQLite is a whole database in a
  single file: no server to run. The tables are created by the numbered
  scripts in `migrations/`.
- **The mail room** (`sportscrawl/scheduler.py`) wakes up every minute. For
  each subscriber it asks: "is your message due?" If anyone's is, it fetches
  the data once, writes each person's message, and sends them one by one.
- **The front desk** (`sportscrawl/poller.py` + a command handler) checks the
  bot's DMs, figures out what each message means (`add AAPL`, `stop`...), does
  it, and replies.

They're separate programs on purpose. The original author's reason: web
servers often run several copies of themselves at once, and if the mail room
lived inside one, every copy would send everyone's message, so people would
get duplicates.

## The mail room, step by step

Every minute, `Scheduler.tick(now)`:

1. **Who's due?** For each active subscriber, a "classify" function answers
   *not due yet*, *due*, or *expired* (too late, skip today).
   - sportscrawl: due at the time you picked, e.g. 7:00am your time.
   - marketcrawl (`market_schedule.py`): due 15 minutes after the stock
     market's closing bell, using `nyse_calendar.py` to know when that is
     (4:00pm New York time, 1:00pm on short days, never on holidays).
2. **Fetch once.** If anyone is due, ask the "source" for data, once for
   everybody. A hundred people following AAPL cost one download, not a hundred.
   - sportscrawl: `source.py` asks ESPN for scoreboards.
   - marketcrawl: `market_source.py` asks Yahoo for price charts, and
     `yahoo.py` turns Yahoo's JSON into tidy `Quote` objects. If filings are
     switched on, `edgar_source.py` also asks the SEC what each company
     filed since the last message, and `edgar.py` keeps only the earnings
     releases, 10-Qs and 10-Ks. Both land in the same pile for the formatter.
3. **Write each message.** A "formatter" turns the data plus one person's
   follows into text. It's a *pure function*: no network, no database, no
   clock, so the same input always gives the same output, which makes it
   easy to test.
   - marketcrawl: `market_formatter.py`. It picks the emoji too: 📈/📉 for
     an ordinary day, 🚀/💥 for a big one (5%+, or 2%+ for an index), 🎩
     for a record close, 🕳️ for the lowest close in a year, and 🔊 when
     twice the usual number of shares changed hands. Records need every
     close since the stock began, so that long history is downloaded about
     once a day per symbol.
4. **Send, then write it down.** After each send, the database records "sent
   today", one message at a time. If the program crashes halfway, it picks up
   where it left off instead of sending everyone a second copy.

### What happens when something breaks

The original author thought hard about failure, and marketcrawl inherits all
of it:

- **Retries.** `http.py` tries each download up to three times, waiting a
  little longer each time.
- **Don't apologise too fast.** If some data is missing at send time, the
  scheduler waits up to 10 minutes, retrying each minute, before sending
  what it has with a "Some prices are missing." line.
- **One bad send doesn't stop the others.** Each person's send is wrapped so
  one failure is logged and everyone else still gets theirs.
- **Too late is worse than never.** If the computer was off and the message
  is more than 90 minutes late, it's skipped for the day.
- **Nothing to say means saying nothing.** Weekends and holidays send no
  message at all, not a "markets closed" note.

### The public post

Once a day the bot also writes a short public post: how the S&P 500, Nasdaq
and Dow did, the three biggest swings in company value, and an invitation to
DM it. "Biggest swing" means the most dollars of company value gained or
lost (market value × % change), not the biggest percentage: Amazon moving 2%
is worth more attention than a tiny company doubling. Candidates come from
Yahoo's lists of the 100 largest companies and the day's top gainers and
losers. It's built as one more
subscriber, a pretend one called the feed (`market_feed.py`), so it gets the
mail room's once-a-day and retry rules for free. Only the writing (300
characters max, never an apology in public) and the delivery (a post, not a
DM) are different. `feed.py enable` switches it on; for now posts are a dry
run, printed instead of posted.

## The front desk

The poller reads new DMs and hands each one to a command handler, which
returns the reply text. It never sends anything itself; the poller does that.
Because the handler just returns text, every command can be tested without
Bluesky.

- sportscrawl's handler (`commands.py`) knows `stop`, `start`, `delete`,
  `config` (which sends a link to a web settings page) and `privacy`.
- marketcrawl's handler (`market_commands.py`) adds `add`, `remove`,
  `list`, `test`, `about` and `help`, and passes everything else to the
  original.
- `test` answers with the message for the latest close straight away,
  whatever the hour, so people can see what they signed up for. It doesn't
  count as that day's message. It's rationed: once per person every 10
  minutes, and 30 an hour for everyone together, because each one downloads
  prices.
- `run_market_poller.py` is the original `run_poller.py` with the market
  handler plugged in. The Bluesky side (logging in, reading the inbox,
  replying) is the original's code, unchanged.

You don't need a password, because Bluesky already proves who sent each DM.
The sender's ID is the only identity the bot uses.

## How marketcrawl plugs in without rewriting the original

The original scheduler was built so that its data source could be swapped.
marketcrawl adds new files and gives the scheduler three optional settings:

| setting | sportscrawl (default) | marketcrawl |
|---|---|---|
| `classifier` | your chosen send time | market close + 15 min |
| `formatter` | scores message | prices message |
| `leagues` | a fixed list of leagues | a function: "whatever symbols people follow right now" |

One more trick: each stock symbol is stored as if it were its own tiny
"league" (`market/AAPL`). So the original's per-league bookkeeping (which
leagues failed, the health table, the admin counts) works for stocks without
any changes.

## Trying it on your own computer

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt flask
echo "EVENTS_PEPPER=$(openssl rand -hex 16)" > .env

.venv/bin/python -m pytest                     # run every test
.venv/bin/python replay_market.py              # the last two weeks of closes, in seconds
.venv/bin/python dev_dm.py "add AAPL sp500"    # pretend to DM the bot
.venv/bin/python dev_dm.py "list"
.venv/bin/python run_market.py --once          # one tick of the mail room
```

`run_market.py` prints messages to the terminal rather than sending DMs. To
see filings too, add an `SEC_CONTACT=you@example.com` line to `.env`: the SEC
refuses requests that don't say who's asking.

With a Bluesky bot account (set `BSKY_IDENTIFIER` and `BSKY_APP_PASSWORD`,
an app password with DM access):

```
.venv/bin/python run_market_poller.py                          # answer DMs
.venv/bin/python run_market.py --once --send --only did:plc:…  # send for real, to one person
.venv/bin/python run_market.py --once --at 2026-10-30T20:20:00Z  # pretend it's just after a close
```

`--only` is the original project's safety catch: while testing, the bot
refuses to message anyone not on that list.

## Running it on a server

Everything for a server is in `deploy/market/`. It expects a fresh Ubuntu
24.04 machine you can SSH into as root.

1. `ssh root@<server> 'bash -s' < deploy/market/setup_market_server.sh`
   installs everything, makes a `marketcrawl` user, downloads the code and
   locks the machine down to key-only SSH. It doesn't start the bot.
2. On the server, fill in `/home/marketcrawl/.env` (the Bluesky login and
   the pepper). Use a separate Bluesky app password from the one on your
   own computer, so either can be revoked alone.
3. `systemctl start marketcrawl-poller marketcrawl-scheduler`.

After that, `deploy/market/deploy_market.sh` (run on the server) updates it
to the latest code on GitHub. Logs: `journalctl -u marketcrawl-poller` (or
`-scheduler`). A backup of the database is taken daily at noon New York time
into `/home/marketcrawl/backups` and kept for 14 days; keep a copy off the
machine too (for example, your hosting provider's backups).

## Known limits

- **The calendar is typed in by hand** and covers 2026–2027. After that it
  stops with a clear error until next year's NYSE dates are added to
  `nyse_calendar.py`.
- **Yahoo's chart API is unofficial.** Like ESPN's, it can change without
  notice.
- **Only US market hours.** A Toronto symbol like SHOP.TO works, but on a
  Canadian holiday when New York is open it will show as missing.
- **After-hours trading is ignored.** The price is the regular 4:00pm close.
- **Filings are US companies only.** Indexes, most funds and foreign stocks
  don't file with the SEC, so they never have any.
- **Earnings call transcripts aren't included.** There's no free, official
  source; the SEC only has what companies file.
