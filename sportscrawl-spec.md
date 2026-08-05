# Sportscrawl: Build Specification

Status: design complete, not yet implemented. Identity and hosting are set up.

Revised 2026-08-04 to close gaps found in review: state with no table (delete
confirmation, help throttle, poll cursor, per-league failure), admin metrics
not derivable from the schema, and three undefined rules in the formatter.

## Identity

Settled. Do not change these.

| item | value |
|---|---|
| Domain | `sportscrawl.app` |
| Bluesky handle | `sportscrawl.app` |
| Bot DID | `did:plc:kmx5rkipob7xh6lhgxzxxkxq` |

Handle is verified by a DNS TXT record at `_atproto.sportscrawl.app` with the
value `did=did:plc:kmx5rkipob7xh6lhgxzxxkxq`. The DID is permanent and does not
change when the handle does. The bot account's DM setting is already set to
allow messages from everyone.

`.app` is on the HSTS preload list, so HTTPS is mandatory at the browser level.
Every URL in code, templates, and message text must be `https://`. A plain
`http://` link is broken, not redirected.

### Operator

| item | value |
|---|---|
| Operator account | `cruftbox.com` |
| Operator DID | `did:plc:7yoku6ecesrllopmfylrvflp` |

Resolved once via `com.atproto.identity.resolveHandle`. Everything downstream
matches the DID; the handle appears in this document and nowhere in the code.

### Resolved placeholders

Confirmed against the server (see section 13):

| placeholder | value |
|---|---|
| `{{APP_DIR}}` | `/home/sportscrawl/app` |
| `{{SERVICE_USER}}` | `sportscrawl` |

Following the existing per-domain account convention on the host, where each
hosted domain has its own `/home/<name>` with a `public_html` beneath it. The
app directory and the database directory both sit outside `public_html`.

The account does not exist yet. It is the one value to confirm before the first
deploy, since renaming it afterwards touches the systemd units, the env file
path, and the vhost.

## 1. What this is

A Bluesky bot that sends subscribers one direct message per day containing
final scores and today's games for the teams and competitions they follow.

Users interact entirely through Bluesky DMs and a token-authenticated web
config page. There are no accounts, no passwords, and no OAuth flow. A DM's
sender DID is authenticated by the protocol before the application sees it,
and that DID is the only identity the system uses.

The project exists to test whether people want an app in this shape. It is
deliberately small.

## 2. Non-goals

Do not build these. They were considered and explicitly rejected.

- Images, rich media, or rendered layouts in the digest. Bluesky DMs are text
  and emoji only.
- A link-to-web digest. The information goes in the message body. The web page
  is for configuration only.
- Real-time alerts, breaking news, or any message outside the daily digest.
- Standings, records, injuries, broadcast info, or commentary. Scores and
  scheduled games only.
- A feed generator. Possible later, out of scope now.
- Any paid dependency. If the project would require recurring cost beyond
  domain registration, it stops instead.

## 3. Architecture

Two processes on one host, sharing one SQLite file.

```
Apache (443, TLS)
  └─ proxy → gunicorn 127.0.0.1:8000 → Flask (config page, admin, privacy)

systemd service → scheduler + DM poll loop → SQLite
                                           → ESPN API
                                           → chat.bsky.convo.* (via PDS proxy)
```

Critical: the scheduler must NOT run inside the Flask app. Gunicorn runs
multiple workers and each would fire the same job, sending duplicate digests.
Two separate processes, one shared database.

Language: Python. Framework: Flask with Jinja templates, server-rendered. No
JS framework. Server-side scheduling via APScheduler or a plain loop.

Server timezone must be UTC. All user-facing time conversion is explicit
against the subscriber's stored timezone. Never rely on server local time.

## 4. Data model

SQLite with WAL mode enabled and a busy timeout set. Multiple gunicorn workers
plus the scheduler process will contend for the file otherwise.

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
```

### subscribers

| column | type | notes |
|---|---|---|
| did | text PK | the atproto DID, never the handle |
| tz | text | IANA name, e.g. `America/Los_Angeles`. Null until the config page is saved. |
| send_time | text | `HH:MM` local to `tz`, default `07:00` |
| state | text | `active`, `paused` |
| created_at | timestamp | |
| paused_at | timestamp | null unless paused |
| last_sent_date | date | local date of last successful digest |
| last_failure_date | date | local date of last failure notice sent |
| last_evaluated_date | date | local date the scheduler last resolved this subscriber, sent or not |

Handles change; DIDs do not. Store and key on DID everywhere.

`last_evaluated_date` exists because of the empty digest rule in section 6. A
subscriber with no games sends nothing, so `last_sent_date` never advances, and
without a separate marker that subscriber stays in the candidate set for every
remaining tick of the day. The scheduler writes `last_evaluated_date`
unconditionally once it has decided; `last_sent_date` only on a delivered
digest.

`tz` is null on signup. There is no defensible default and guessing wrong sends
someone their digest at 3am, so the scheduler skips any subscriber with a null
`tz` and the config page will not save without one. See section 10.

### subs

| column | type | notes |
|---|---|---|
| id | integer PK | |
| did | text FK | cascade delete |
| kind | text | `team` or `competition` |
| league | text | ESPN league slug |
| team_id | text | ESPN team id, null when kind=`competition` |

`UNIQUE(did, kind, league, team_id)`. The config page writes selections
wholesale and a partial failure mid-save must not leave a duplicate that
renders the same game twice.

Two subscription kinds with different assembly logic:

- `team`: filter a league scoreboard down to events where the team is a
  competitor. Persists indefinitely across seasons.
- `competition`: take the whole slate for that league slug. Used for World Cup,
  Champions League, Concacaf. Needs lifecycle handling (section 10).

### teams

Seeded from ESPN at setup, refreshed occasionally. Used for command matching
and the config page picker.

| column | notes |
|---|---|
| league | ESPN league slug |
| team_id | ESPN id |
| display_name, short_name, nickname, location, abbreviation | for alias matching |
| logo_url | for the config page |

`PRIMARY KEY(league, team_id)`. ESPN team ids are only unique within a league.

### config_tokens

| column | notes |
|---|---|
| token | random, URL-safe, 32+ bytes |
| did | |
| scope | `config` or `admin`, see sections 9 and 11 |
| expires_at | short lifetime, see section 10 |
| used_at | null until first load |

**Not single use.** An earlier revision of this spec said single use, and
building it proved that wrong within minutes: the first test link was dead
before it reached a browser.

The link is delivered in a Bluesky DM, and chat clients fetch URLs to build
link previews. A single-use token would routinely be spent by a preview
crawler before the subscriber ever tapped it, and the same is true of a
browser prefetch, a double click, or a reload. The failure is silent and looks
exactly like a bug in the bot.

So a token is redeemable repeatedly until `expires_at`. `used_at` records the
first redemption, for the return-visit metric in section 11, and invalidates
nothing. Security rests on the token being unguessable and short lived, which
is where it rested anyway — single use was never what made it safe.

### sessions

The token is short-lived; the session it exchanges for is the durable
credential, so it needs a row of its own and a revocation path.

| column | notes |
|---|---|
| sid | random, URL-safe, 32+ bytes, primary key |
| did | text FK, cascade delete |
| scope | `config` or `admin` |
| created_at | timestamp |
| expires_at | 30 days from creation for `config`, 24 hours for `admin` |
| last_seen_at | for the return-visit metric in section 11 |

Scope is checked on every request, not just at exchange. A `config` session
must never reach an admin route because of a missing check on one handler.

Cookie flags: `Secure; HttpOnly; SameSite=Lax; Path=/`. Section 9's "invalidate
prior tokens" does not touch live sessions, and `delete` must destroy both.

### convo_state

Chat transport bookkeeping, keyed by Bluesky conversation, holding no DID. This
is deliberate: the poll loop needs to remember what it has already processed,
and that memory has to survive `delete` or a returning subscriber's old
messages get replayed as fresh commands.

| column | notes |
|---|---|
| convo_id | primary key, from `chat.bsky.convo.*` |
| last_message_id | highest message id already processed, see section 8 |
| last_help_at | timestamp, backs the help throttle in section 9 |
| delete_pending_until | timestamp, null unless a `delete` is awaiting confirmation |

The sender DID arrives on every inbound message, so nothing here needs to store
one. `delete` does not clear this row.

### league_state

Per-league fetch health and lifecycle. Section 7 requires failure tracking per
league and section 10 requires knowing when a competition has ended; both live
here.

| column | notes |
|---|---|
| league | primary key, ESPN slug |
| last_fetch_at | timestamp |
| last_success_at | timestamp, null if never |
| last_event_date | most recent date this slug returned any event |
| consecutive_failures | integer, reset to 0 on success |

### events

Append-only. This is the only table that survives deletion, and it is what
makes section 11 possible at all — retention, churn, and the paused-vs-deleted
split cannot be computed from current state after the row is gone.

| column | notes |
|---|---|
| id | integer PK |
| ts | timestamp, UTC |
| event_type | `signup`, `pause`, `resume`, `delete`, `config_load`, `config_save`, `digest_sent`, `failure_notice` |
| subject | `HMAC-SHA256(did, pepper)` hex, not the DID |
| league | optional dimension, null for account events |
| detail | small JSON for aggregate dimensions only. Never message text, never a handle. |

The pepper is a random secret in the env file alongside the app password. It is
never rotated, because rotating it breaks the join between a subscriber's
signup and their later churn. Without the pepper the column is not reversible
to a DID; with the database and the pepper together it is, so this is
pseudonymisation, not anonymisation, and section 12 says so rather than
claiming more.

Writing a `delete` event at the moment of deletion is the point of the table.
Disclose it in the privacy policy and keep it aggregate-only.

### Deletion

`delete` removes the subscriber row, all `subs` rows, all `config_tokens` and
`sessions` for that DID, and any cached send state. One transaction. It writes
one `delete` row to `events` and leaves `convo_state` alone. See section 9.

## 5. ESPN adapter

Unofficial, undocumented, no auth, no SLA. Verified working as of mid-2026.
Wrap it behind a thin interface so a replacement source could be swapped in
without touching the scheduler.

### Endpoints

```
https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD
https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams
https://site.api.espn.com/apis/site/v2/leagues/dropdown?sport=soccer&limit=500
```

The third one enumerates soccer competitions: 220 of them, each with `slug`,
`name`, and `shortName` in one response, no ref-following. Verified 2026-08-04
and it covers every competition at launch — `fifa.world`, `uefa.champions`,
`eng.1`, `usa.1`, and the whole `concacaf.*` family.

`https://site.api.espn.com/apis/site/v2/sports/soccer`, named in the original
draft, **returns `{"code":404}`**. It is gone. Fallback if the dropdown follows
it: `https://sports.core.api.espn.com/v2/sports/soccer/leagues?limit=1000`
returns the same 220 as `$ref` URLs with the slug embedded in each path, so
slugs can be extracted without 220 follow-up requests.

That one of the three endpoints in this spec died between the draft and the
first line of code is the strongest available argument for the adapter boundary
below. Section 17 says ESPN can disappear without notice; a piece of it already
did.

### Leagues at launch

US pro:

- `football/nfl`
- `basketball/nba`
- `baseball/mlb`
- `hockey/nhl`
- `basketball/wnba`
- `soccer/usa.1` (MLS)
- `soccer/usa.nwsl` (NWSL)

Soccer and tournaments:

- `soccer/eng.1` (Premier League)
- World Cup, Champions League, Concacaf competitions

Do not hardcode soccer slugs. Fetch `/sports/soccer`, cache the result, and
resolve slugs from it. Competition names and slugs change when competitions
are rebranded.

### Known gotchas

**Date boundaries.** The scoreboard takes a single `dates=YYYYMMDD` and does
not support ranges. ESPN's date bucketing is Eastern-flavored, so a 7:30pm
Pacific game carries a UTC timestamp on the following calendar day. Fetching
only "today" will silently drop late West Coast games.

Fix: fetch a three-day window (yesterday, today, tomorrow) per league, then
filter events by converting each event's UTC timestamp into the subscriber's
timezone and comparing local dates.

The window is computed against the **UTC** date, not server local time and not
any subscriber's local date. Three UTC days centred on today covers every
subscriber timezone from UTC-11 to UTC+14 for the local yesterday/today/tomorrow
span the formatter needs. Section 3 already requires the server clock be UTC;
this is the place where getting it wrong silently drops games instead of
crashing.

**Truncation.** College endpoints truncate by default and need `groups=` and a
high `limit=`. Not needed at launch since college is out of scope, but note it
if scope expands.

**Fan-out.** Fetch once per league per cycle into a cache, then assemble every
subscriber's digest from that cache. Never fetch per subscriber. A hundred
subscribers should still cost single-digit API calls.

**Schema drift.** No versioning, no contract. Validate the parse. An empty
`events` array and a failed fetch must be distinguishable, because one means
"no games" (stay silent) and the other means "send a failure notice." A 200
response that fails validation is a failure, not an empty slate.

### HTTP client and caching

| setting | value |
|---|---|
| connect / read timeout | 5s / 10s |
| retries | 2, exponential backoff, only on timeout, connection error, 5xx, 429 |
| User-Agent | `python-requests/{version} sportscrawl/1.0 (+https://sportscrawl.app)` |
| scoreboard cache TTL | 10 minutes |
| `/sports/soccer` slug cache TTL | 24 hours, stale-if-error |
| `/teams` refresh | weekly, manual trigger available |

Two caches with different jobs. The scoreboard cache exists to collapse
fan-out within one send cycle, so its TTL only has to outlive a batch drain.
The slug cache exists so a soccer outage does not take out slug resolution;
serve stale slugs indefinitely rather than failing, since a slug that resolved
yesterday is almost certainly still right.

Identify the client honestly in the User-Agent — but the honest string cannot
go first. **ESPN 403s unrecognised user agents.** Verified 2026-08-04: bare
`sportscrawl/1.0` is refused, and so is `foobar/1.0`, while `curl/8.5.0`,
`python-requests/2.32.5`, and no User-Agent at all are all served. It is an
allowlist of known clients, not a block on bots, and it fails closed on
everything it does not recognise.

So the client token leads and the identification follows it. This is still not
a forged browser: `python-requests` really is the client, `sportscrawl/1.0`
says who is driving it, and anyone reading ESPN's logs can tell exactly what
this is and where to complain. Sending `Mozilla/5.0` would be the line, and it
is not necessary.

Both halves are load-bearing. Drop the prefix and every request 403s; drop the
suffix and the traffic becomes anonymous. Worth a comment in the code, because
this looks like cargo cult until you know the endpoint refuses the obvious
thing.

Every fetch outcome updates `league_state`: `last_fetch_at` always,
`last_success_at` and `last_event_date` on a validated parse,
`consecutive_failures` incremented or reset.

### Team seeding

`/teams` returns id, displayName, shortDisplayName, nickname, location,
abbreviation, and logo URLs per team. This is the source for:

- the canonical `team_id` used as the subscription key
- the alias table used for command matching
- crests on the config page

Team logos are also available directly:
`https://a.espncdn.com/i/teamlogos/{sport}/500/{abbrev}.png`

## 6. Digest formatter

**Build this first.** It is a pure function:

```python
def format_digest(
    events: list[Event],
    subs: list[Sub],
    tz: str,
    now: datetime,
    failed_leagues: set[str],
) -> Digest

@dataclass
class Digest:
    text: str | None        # None means send nothing
    kind: Literal["digest", "partial", "failure", "silent"]
```

Given fetched scoreboard data and a subscription list, return the message
string. No network, no database, no Bluesky. Testable against saved JSON
fixtures in milliseconds. Every hard problem in this project lives here.

`failed_leagues` is required because section 7 wants per-league failure notices
and the function otherwise has no way to tell "no games in MLS" from "MLS did
not answer." `kind` is what the scheduler commits against: which of
`last_sent_date` and `last_failure_date` advances depends on it.

### Which day is a game on

An event belongs to the local date on which it **starts**, converting the
ESPN UTC kickoff timestamp into the subscriber's `tz`. A game starting 22:00
local Tuesday belongs to Tuesday no matter when it ends. Sectioning is then by
status, not by date:

- **Final** — any included event whose status is final. This picks up last
  night's games and also a 10am kickoff already over by a 6pm send time.
- **Today** — included events with local start date equal to today that are not
  final, listed with their start time.
- **Tomorrow** — competition subscriptions only, per the rule below.

Included events are those whose local start date is yesterday or today, plus
tomorrow for competition subscriptions. Yesterday's *scheduled* games that never
went final (postponed, still listed) are dropped; a stale fixture line is worse
than a missing one.

In-progress games appear under Today with their start time and no score. This
is a once-daily digest and a live score is stale minutes later; section 2 rules
out real-time anyway.

### Which team goes first

One rule, deterministic, covering every case:

1. If exactly one competitor is a team the subscriber follows, that team goes
   first. Seeing `Dodgers 3, Padres 6` reads as a loss instantly.
2. If **both** competitors are followed, the home team goes first. Neither
   perspective wins, so venue is the neutral tiebreak.
3. Otherwise, for a final with a winner, the winner goes first. This is the
   case that covers competition subscriptions.
4. Otherwise — a draw, or nothing decided yet — the home team goes first.

Test them in that order. Rules 2 and 3 both match a subscriber who follows both
teams in a finished game, and rule 2 has to win or a fan of both Bay Area teams
gets winner-first ordering.

For scheduled games the subscriber's team keeps first position and takes `vs`
when home, `at` when away, per Output format below.

With **no** followed team, the home side leads and takes `vs`: `Dream vs
Mercury`, not `Mercury at Dream`. An earlier draft of this section said the
opposite, and the reason to settle on `vs` is that it is never wrong.

`at` asserts a venue. Competition slates are exactly where no team is followed,
and tournament matches are routinely played at neutral sites, where `France at
Brazil` falsely says Brazil is hosting. ESPN's `neutralSite` flag cannot rescue
this: it is present and `false` on MLB events but **absent entirely on soccer**,
including every World Cup fixture checked — missing precisely where it would
matter. `vs` claims nothing about venue and reads correctly either way.

So `at` appears only when a subscriber's own team is involved, which is both
where the perspective earns its keep and where neutral sites are rarest.

This resolves an ambiguity in the original draft: the stated requirement that
home and away be distinguishable applies to **scheduled** games only. A final
score line does not encode venue, exactly as the `Dodgers 6, Padres 3` sample
digest below already shows. Adding `vs`/`at` to finished games buys nothing a
fan needs at 7am.

### Output format

Text and emoji only. No markdown, no bold, no monospace. Column alignment will
not hold in a proportional font, so one game per line and no ASCII tables.

Each game line is prefixed with an emoji for its sport, following the existing
Firstlight convention.

| emoji | sport | leagues |
|---|---|---|
| 🏈 | football | NFL |
| 🏀 | basketball | NBA, WNBA |
| ⚾ | baseball | MLB |
| 🏒 | hockey | NHL |
| ⚽ | soccer | MLS, Premier League, World Cup, Champions League, Concacaf |

The emoji identifies the **sport, not the league**. Collisions are intentional
and accepted: all soccer competitions share ⚽ and both basketball leagues
share 🏀. Do not add league headers, league name tags, flags, or competition
badges to disambiguate. The team names carry that information and the emoji is
there for visual scanning only.

```
Tue Aug 4

Final
⚾ Dodgers 6, Padres 3
⚾ Astros 7, Angels 2
⚽ Arsenal 2, Chelsea 1

Today
🏀 Lakers vs Nuggets 7:30p
⚽ Galaxy at Austin FC 5:30p
```

Each emoji costs one grapheme against the message cap, which is cheaper than a
text league prefix.

Home and away must be distinguishable on scheduled games. `vs` for home, `at`
for away, from the subscriber's team's perspective where there is one. Ordering
in every case follows "Which team goes first" above.

For competition subscriptions, include tomorrow's fixtures as well as today's.
Tournament matches cluster and a day's notice is useful there in a way it is
not for a 162-game season.

### Length

Typical output is well under the DM cap. The limit only bites on a World Cup
group-stage day.

**The cap is 1000 graphemes. Verified 2026-08-04**, by sending a deliberately
over-long message and reading the rejection:

```
InvalidRequest: grapheme too big (maximum 1000, got 4960) at $.message.text
```

Graphemes, not bytes and not codepoints. Counting with Python's `len()` counts
codepoints, which over-counts a decomposed accent (`é` as `e` + combining mark
is two codepoints and one grapheme) and never under-counts for the characters
this digest can contain. Over-counting trims a line early; under-counting would
get a whole message rejected at send time. The approximation is therefore safe
in the direction that matters, and precise grapheme segmentation is not worth a
dependency.

For reference, a full 14-match MLS Saturday renders in 397 characters.

Overflow strategy is priority ordering, not byte truncation:

1. Finals involving followed teams
2. Today's scheduled games
3. Everything else

If it still overruns, cut with a count: `+6 more`. Only split into a second
message past a hard threshold, and send the important half first so the
notification preview carries it.

### Newlines

**Verified 2026-08-04.** A 13-line digest sent through
`chat.bsky.convo.sendMessage` and read back with `getMessages` came back byte
for byte identical, newlines intact. The transport preserves them; the entire
format depends on it and it holds.

### Links must carry facets

**Bluesky renders links from facets, not by parsing the text.** A message sent
with a bare URL and no facets displays as inert text that cannot be tapped.
Verified 2026-08-04 the hard way, by sending one.

This is a launch blocker for the help response in section 9, whose entire job
is handing a stranger the config link. Without facets someone gets help and
still cannot pick teams.

Attach an `app.bsky.richtext.facet#link` for every URL in every outbound
message, in the transport rather than at each call site, so no message type can
forget. Do not rely on client-side auto-detection: a URL without a TLD is not
linkified at all.

Facet offsets are **UTF-8 byte** indices, not character indices. A digest
containing `CF Montréal` ahead of a link shifts every later offset by one and
silently produces a link over the wrong range — a bug that would only ever
appear for teams with accented names.

### Empty digest

If a subscriber has no games on a given day, send nothing. This is not
configurable. An NFL-only subscriber would otherwise get "no games today" for
seven months. Returns `kind="silent"`, and the scheduler still advances
`last_evaluated_date` so the subscriber stops being reconsidered every minute
for the rest of the day.

### Partial failure line

When some of a subscriber's leagues have content and others are in
`failed_leagues`, return `kind="partial"`: the normal digest, plus one trailing
line above the attribution. No league names, no technical detail.

```
Some scores are missing this morning.
```

When *every* league the subscriber follows is in `failed_leagues`, there is no
digest to attach anything to; return `kind="failure"` and the standalone notice
from section 7.

### Attribution line

**Removed 2026-08-05, after seeing it in a real thread.**

The original reasoning was that screenshots are the only organic discovery
channel this project has, so the digest should carry the handle. In practice
the message arrives *from* `@sportscrawl.app`, in a conversation with the bot,
with the handle already on screen directly above it. The line told the reader
nothing they could not see, and it cost two lines of a 1000-grapheme budget in
every message, every day, forever.

Screenshots still show the sender. The discovery argument survives; the extra
line was not what carried it.

## 7. Scheduler and delivery

Tick every minute. Select subscribers where `state = 'active'`, `tz` is not
null, the current time in their `tz` is within the lateness window past
`send_time`, and `last_evaluated_date` is not today's local date.

**Lateness window: 0 to 90 minutes.** Do not test for exact minute equality; if
the process was blocked or the server was down at 07:00, a strict check drops
that day's digest entirely. But an unbounded "at or past `send_time`" test is
just as wrong in the other direction — a subscriber who signs up at 15:00 with
a 07:00 send time would get an immediate digest, and an outage lasting until
mid-afternoon would deliver a breakfast digest at teatime. Ninety minutes
absorbs a reboot and a slow deploy. Past it, write `last_evaluated_date` and
skip the day silently. A missing digest is a smaller failure than a digest that
arrives when it is no longer true.

DST: on a spring-forward day a `send_time` inside the skipped hour never
occurs locally. The window comparison must be done on aware datetimes so the
subscriber is picked up at the first real minute after the jump rather than
waiting a year.

**Pacing.** Users pick round numbers. 7:00 and 8:00 will hold most of the
subscriber base and fire simultaneously across four US timezones. Structure
the send as a queue drain with a small gap between messages, not a tight loop.
A batch taking ninety seconds is invisible to recipients and keeps clear of
chat rate limits. Keeping it as a queue also gives throttling, backoff, and
retry somewhere to live later.

**Per-message commit.** Write `last_sent_date` as each message succeeds, not
after the batch. A crash mid-batch must resume, not re-send.

**What each outcome commits.** The `kind` returned by the formatter decides:

| kind | last_sent_date | last_failure_date | last_evaluated_date | events row |
|---|---|---|---|---|
| `digest` | today | — | today | `digest_sent` |
| `partial` | today | today | today | `digest_sent` |
| `silent` | — | — | today | — |
| `failure` | — | today | today | `failure_notice` |

`partial` sets both dates: the subscriber got their scores and got told
something was missing, so neither a re-send nor a second apology should follow.

**Re-check before send.** Confirm the subscriber row still exists and is
active immediately before each send. Otherwise a `delete` landing mid-cycle
results in messaging someone after telling them they were removed.

### Failure notices

When a data fetch fails, subscribers get a short, non-technical notice instead
of a digest. Rules:

- **Per league, not global.** If only a soccer slug fails, MLS subscribers get
  a notice and NBA subscribers get a normal digest. Track failure state per
  league.
- **At their scheduled time**, not when the fetch fails. Nobody wants a 3am
  error message.
- **Once per subscriber per day, maximum.** Guard with `last_failure_date`. A
  single outage must not become 200 apologies.
- No retry promises in the text. Something like: no scores available this
  morning, the data source is not responding.

**Retry before apologising.** A three-minute ESPN blip landing exactly on 07:00
should not cost anyone their digest. When a subscriber comes due and one of
their leagues is failing, retry that league on the next ticks for up to ten
minutes before giving up. Send the notice at the ten-minute mark, still close
enough to their chosen time to read as "this morning." Only after that does the
subscriber's outcome resolve to `partial` or `failure`.

Ten minutes, not the full ninety-minute lateness window: a notice that arrives
an hour and a half late is worse than no notice, and by then the digest itself
would be past its point.

## 8. Chat transport

Bot account authenticates with an app password that has DM access enabled, or
OAuth with `transition:chat.bsky` in addition to `transition:generic`. Chat
endpoints are proxied through the PDS to `did:web:api.bsky.chat`.

The bot account's DM setting must allow messages from everyone, otherwise
nobody can start a conversation. Default is followers-only.

Requiring the user to message first is also correct operationally: it creates
the conversation, which sidesteps deliverability entirely.

Poll `chat.bsky.convo.listConvos` for inbound messages. The sender DID on each
message is the identity. Do not resolve or store handles as identity.

**Cursor and idempotency.** `listConvos` reports each conversation's latest
message; it is not a queue and it does not remember what the bot has acted on.
Without a cursor, a restart re-reads recent history and re-executes commands —
duplicate help replies at best, a replayed `delete confirm` at worst.

For each conversation with unread activity, page `chat.bsky.convo.getMessages`
from `convo_state.last_message_id` forward, process in order, and write
`last_message_id` **after each message is handled**, in the same transaction as
whatever the command changed. Then call `updateRead`. Read markers are a
courtesy to the UI, not the source of truth; the database column is.

Command handling must be idempotent regardless, because the write can still be
lost between the API call and the commit. Re-running `stop` on a paused
subscriber, or `delete confirm` on a DID that no longer exists, must be a
no-op, not an error and not a second reply.

**Message requests. RESOLVED 2026-08-04, and the answer is the good one.**

Tested by DMing the bot cold from `cruftbox.com`, an account it does not
follow. Results against the live API:

| query | result |
|---|---|
| `listConvos` (no params) | **1 convo**, carrying `status: "request"` |
| `listConvos?status=request` | 1 convo, the same one |
| `listConvos?status=accepted` | 0 convos |

The default view **does** include message requests, so the poll loop needs no
filter and no signup is invisible. `getMessages` and `sendMessage` both work on
a request-status conversation without accepting it first, so there is no accept
step to forget either.

Two things follow. Do not "fix" the poll loop by adding `status=accepted`: that
is precisely the filter that would make every new signup invisible and the bot
appear dead to anyone trying it. And do read `convo.status` anyway — it is
available, and a conversation that is still a request is a subscriber who has
never had a reply land in their main inbox.

The app password is a full-access credential for the bot account. Env file,
mode 600, never in the repo.

## 9. Commands

Six public, plus one hidden. Normalize input before matching: lowercase, strip
punctuation and surrounding whitespace.

| command | behavior |
|---|---|
| `config` | issue a fresh token, invalidate prior tokens for that DID, reply with the link |
| `stop` | set `state = 'paused'`. Reply must state how to resume and how to delete. |
| `start` | set `state = 'active'` |
| `delete` | requires confirmation, see below |
| `privacy` | reply with the policy URL |
| anything else | help response |

`stop`, `start`, and `delete confirm` each write their `events` row — `pause`,
`resume`, `delete` — in the same transaction as the state change.

### admin, hidden

`admin` from the operator DID in section Identity replies with a link to the
admin page carrying a fresh `admin`-scoped token. It is not in the help text
and is not one of the six.

From **any other DID**, `admin` is an unknown word and gets the ordinary help
response. No error, no "not authorized", no acknowledgement that the word means
anything — a denial is itself an answer, and this is a one-person command on a
public inbox. Compare the DID, never the handle: a handle can be given up and
re-registered by someone else, which is the entire reason section 4 keys on
DIDs.

The DID lives in `ADMIN_DID` in the env file. It is public information rather
than a secret, but keeping it out of the repo means a leak of the private repo
does not also advertise who runs the bot.

Rate limit it like any other command, and write an `events` row so an
unexpected admin link request is visible after the fact.

Match `stop` before anything else, and accept synonyms: unsubscribe, quit,
cancel, off. It is the opt-out path and must work even when everything else is
broken.

### First contact

Any inbound message from a DID with no subscriber row creates one, before the
command is dispatched: `state = 'active'`, `send_time = '07:00'`, `tz = null`,
and a `signup` row in `events`. The reply is the help response, which carries
the config link.

That subscriber is inert until they visit the config page — no `tz` and no
`subs`, so the scheduler skips them twice over. Nothing is sent to anyone who
DMs the bot once and never configures anything, which is the correct behaviour
and also the reason the config page can require a timezone rather than guessing
one.

`start` on a DID with no row is the same path: create, then activate.

### delete confirmation

`delete` is irreversible and one word from `stop` in a chat box. Never delete
on first message. Reply describing what will be removed and require
`delete confirm` within **five minutes**.

The pending state is `convo_state.delete_pending_until`, set on the first
`delete` and cleared on confirmation, on expiry, or on any other command
arriving in between. It is a column, not an in-memory dict: a scheduler restart
between the two messages must not leave someone's confirmation landing on
nothing, and an in-memory pending flag would also be lost in a way the user
cannot see. A `delete confirm` with no live pending window replies with the
`delete` prompt again rather than deleting.

On delete, in one transaction: subscriber row, subs rows, config tokens,
sessions, send history, plus one `delete` row written to `events`. Then verify
the scheduler cannot message them from an in-memory cache, and that any open
config page session is dead on its next request. `convo_state` survives, so the
message ids already processed stay processed.

A DID that deletes and later DMs again is a brand new subscriber with no
history. Write this cycle as an actual test against a fixture database. It is
the path most likely to rot silently as tables are added, and the one where a
bug breaks a published promise.

### Help response

This is the onboarding message. It is what a stranger gets on first contact,
so it works as an explanation, not a reference card. Must include the config
link, otherwise someone gets help and still cannot pick teams.

```
Sportscrawl sends you a daily score digest
for the teams you follow.

Pick teams and set your delivery time:
https://sportscrawl.app/c/a7f3k9

Commands:
config - get a fresh settings link
stop - pause messages
delete - remove all your data
privacy - privacy policy
```

**Rate limit the help response.** One per conversation per hour, regardless of
how many unknown messages arrive. If another bot ever DMs this one, an
unthrottled help reply creates an infinite loop that burns the chat rate limit.

The throttle is `convo_state.last_help_at`, in the database, not in process
memory. An in-memory counter resets on every deploy and restart, and a bot loop
is exactly the condition that causes restarts — the guard would evaporate at
the moment it is needed.

Do not repeat identical help to the same person in a short window. First
unknown message gets full help; subsequent ones get a short nudge to the link.

## 10. Config page

Route: `https://sportscrawl.app/c/<token>`

The DM channel is already authenticated, so a capability URL delivered over it
is no weaker than the channel itself. No login UI at all.

Token is short-lived and exchanges for a session cookie on load. The link dies
in fifteen minutes; the browser stays authorized for thirty days. `config`
issues a fresh one anytime and invalidates the previous.

The token is **not** single use — see section 4. A link preview crawler would
spend it before the subscriber ever tapped it.

### Session and route hardening

The token gets all the attention, but the session is the credential that lasts,
so it needs the same care:

- Session row per section 4, cookie `Secure; HttpOnly; SameSite=Lax; Path=/`.
- Invalidating tokens does not log anyone out. `delete` destroys sessions; that
  is the only revocation path and it must actually run.
- CSRF token on the settings POST. `SameSite=Lax` blocks the cross-site form
  post but is not a substitute for one.
- Headers: `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`,
  `X-Content-Type-Options: nosniff`, and a CSP with no external origins.
- Rate limit `/c/<token>` per IP. A 32-byte token is not guessable, but the
  route should not be a free oracle either.

A wrong, expired, or already-redeemed token gets one page: ask the bot for a
fresh link. Never distinguish those cases in the response.

### Appearance

Light Firstlight. Same visual language as the existing Firstlight app —
Bootstrap 5.3, dark navbar with an emoji brand mark, system-ui body font,
`container` layout, almost no custom CSS — stripped to a single screen. No
multi-page nav, because there is only one page.

Vendor Bootstrap's CSS into `static/` rather than loading it from a CDN.
Firstlight uses jsDelivr, which is fine for a personal tool on a private
network, but section 12 promises no third parties and a CDN request hands the
visitor's IP to one on the page that exists to configure their subscription.

### Page order

Timezone first, at the top, above everything else:

1. **Timezone.** The one field with no safe default, and nothing below it means
   anything until it is set — a delivery time without a timezone is not a time,
   and the digest preview cannot be rendered. Prefill the dropdown from
   `Intl.DateTimeFormat().resolvedOptions().timeZone` and have the user confirm
   it. Prefilled is not confirmed: the field stays flagged, and save is disabled,
   until they have actually looked at it.
2. Delivery time
3. Team picker with crests, grouped by league
4. Competition toggles (World Cup, Champions League, Concacaf)
5. Live preview of the digest

This is also the onboarding order. A first-contact subscriber arrives here with
`tz = null` and no selections, so the page they land on reads top to bottom as
the setup flow, and a returning subscriber sees their timezone already answered
and scrolls past it.

The config page exists because a dropdown beats typing a timezone into a chat
box. Anything fiddly belongs here, not in the command surface.

Saving writes a `config_save` event and replaces the subscriber's `subs` rows
in one transaction. Loading writes `config_load` and updates
`sessions.last_seen_at`, which is what backs the return-visit metric.

### Competition lifecycle

Competition subscriptions need expiry. When the World Cup final is played,
that subscription is dead weight. Auto-close it with a final message and drop
the row. Recurring competitions with stable slugs roll over naturally.

ESPN will not tell you a tournament is over — there is no "final match" flag —
so detect it by absence: when `league_state.last_event_date` for a competition
slug is more than **14 days** old and the slug is not in a failure state, close
every subscription to it, send one message per affected subscriber, and drop
the rows.

Fourteen days is long enough to survive an international break or a gap between
group and knockout stages, and the check must be skipped entirely while
`consecutive_failures` is non-zero. Otherwise a fortnight of ESPN trouble
silently unsubscribes everyone from the World Cup.

## 11. Admin page

Aggregates only. Do not build a subscriber list view. If the page cannot
display individual rows, the privacy policy claim stays true without relying
on discipline.

| metric | source |
|---|---|
| Subscriber count over time | `events`, cumulative signups minus deletes |
| Active vs paused counts | `subscribers.state` |
| Deleted count | `events`, `delete` rows — the row is gone from `subscribers` |
| Selections by league and by team | `subs` |
| Distribution of delivery times | `subscribers.send_time` |
| Retention at 7, 30, 90 days | `events`, signup cohort vs first pause or delete |
| Stop rate and time-to-stop | `events`, `signup` to `pause` interval |
| Config page return visits | `events`, `config_load` per subject |

Four of those eight cannot be computed from current state at all. Deletion
removes the row, so churn, retention, and time-to-stop are unrecoverable the
moment the subscriber leaves — which is precisely the population the project
exists to measure. That is what the `events` table in section 4 is for, and why
it is the one table `delete` does not touch.

Paused and deleted are different signals and must be tracked separately.
Paused suggests seasonal quiet, deleted suggests churn. That distinction is
the metric the project exists to gather.

**The events table is a privacy commitment, not just a schema decision.** It
outlives deletion by design, so it is bounded hard: pseudonymous subject
column, no handles, no message content, no free text, and every query on this
page is a `GROUP BY`. Suppress any bucket with fewer than five subjects rather
than rendering a cell that identifies one person by arithmetic. Section 12
discloses the table's existence and its contents in plain language.

### Admin access

Two paths, deliberately:

1. **DM `admin`** from the operator DID, per section 9. Fifteen-minute
   single-use token, exchanges for a 24-hour `admin` session. Same machinery as
   the config link, one extra column. This is the path that works from a phone.
2. **HTTP basic auth over TLS**, on an unlinked path carrying `noindex`.

The second exists because the first depends on the DM poll loop, which shares a
process with the scheduler. If that process is wedged, that is exactly the
morning the dashboard is worth opening. An admin page reachable only through
the thing that is broken is not much of an admin page.

Two auth paths on one route is a real cost — two things to get right, and the
scope check in section 4 has to hold on every handler. It is accepted for the
availability reason above and nothing else. Do not add a third.

For debugging one subscriber's digest, write a CLI query. Same capability,
different friction, and the web UI stays aggregate-only.

## 12. Privacy policy

Public URL, linked from the help message, the config page, and the `privacy`
command. Contact method that is not the bot's DM inbox: **michael@pusateri.org**.

It has to be off-channel. Someone who has just sent `delete` should not have to
message the thing they are leaving in order to ask a question about it, and a
`stop` that also stops the support path is not really a stop.

Must state:

- What is stored: DID, timezone, delivery time, team selections, timestamps.
  Plus, for the service to function: a Bluesky conversation id and the id of
  the last message processed in it, so the same command is not run twice.
  Nothing else.
- That a DID resolves to a public Bluesky handle and profile, so subscribers
  are identifiable. Do not imply anonymity.
- That inbound DMs are processed for commands and not retained. **Then
  actually do this**: log the DID and matched command, never the message body.
- That `stop` pauses and retains, `delete` removes everything **the account
  record holds**, and that a counter of the form "someone signed up on this
  date, someone deleted on this date" is kept afterwards. It carries a
  one-way-hashed identifier rather than the DID, it is only ever read as a
  total, and it is what tells the project whether people are staying. Say this
  plainly rather than letting "removes everything" mean something slightly
  untrue. Do not call it anonymous: holding the database and the hash secret
  together would re-link it, so the honest word is pseudonymous.
- That the conversation history remains on Bluesky's chat service after
  deletion. The application deletes what it holds; the message thread is not
  the application's to delete.
- That DMs are not end-to-end encrypted and Bluesky can see their contents.
- No third-party sharing. There is none. ESPN sees the server, not the users.
- That this is a personal project with no uptime guarantee and may end.

## 13. Deployment

Hosted on an external VPS, on a dedicated domain account separate from any
other hosted domain on the box. Not self-hosted at home.

### The actual host

Verified over SSH on 2026-08-04, correcting assumptions in the original draft.

| item | value |
|---|---|
| Host | `host2.pusateri.com`, reachable as `sportscrawl.app` and `cruftbox.com` |
| OS | AlmaLinux **8.10** (not 9) |
| System Python | **3.6.8** (not 3.9) |
| Apache | 2.4.67 |
| SQLite | **3.26.0** (2018) |
| SELinux | **Disabled** |
| Control panel | none; per-domain `/home/<name>/public_html` by convention |

Access is `root@sportscrawl.app` with `~/.ssh/id_rsa`, the same key and user as
the existing `cruftbox` host entry. Both names resolve to one machine — the SSH
host key fingerprints are identical — so `sportscrawl.app` is a vhost and a
domain account on a shared box, not a separate server.

That is what section "Isolation" below is about, and it is now load-bearing
rather than theoretical: the other domains on this host are the threat model.

The reason matters for design decisions: this service messages other people on
a schedule. A residential connection, a power blip, or a reboot mid-project
becomes someone else's missing digest. The workload wants nothing from home
hardware anyway. No GPU, no bulk storage, a few MB of SQLite and some HTTP
calls.

### Source control

Private GitHub repository. Deployment is by git, not by file upload.

The server holds a clone and updates with `git pull`. Use a **read-only deploy
key** scoped to this repository, generated on the server, rather than a
personal SSH key or a personal access token. If the box is compromised, the
attacker gets read access to one repo and nothing else.

Nothing secret or stateful goes in the repo. `.gitignore` must cover:

```
.env
*.db
*.db-wal
*.db-shm
__pycache__/
.venv/
instance/
```

The app password is a full-access credential for the bot's Bluesky account.
It lives in an env file outside the repo, mode 600, owned by
`{{SERVICE_USER}}`, referenced from the systemd units with
`EnvironmentFile=`. Commit a `.env.example` with keys and empty values so the
required configuration is documented in the repo without the secrets.

Four secrets, all in that file:

| key | purpose |
|---|---|
| `BSKY_APP_PASSWORD` | chat transport, section 8 |
| `FLASK_SECRET_KEY` | session cookie signing and CSRF |
| `EVENTS_PEPPER` | HMAC key for `events.subject`, section 4 |
| `ADMIN_PASSWORD` | basic auth on the admin route, section 11 |
| `ADMIN_DID` | operator DID for the hidden `admin` command, section 9 |

`EVENTS_PEPPER` is generated once and never rotated — rotating it severs the
join between a subscriber's signup and their later churn, which is the whole
metric. It must be in the backup, or the analytics history is unreadable after
a restore.

The SQLite file also lives outside the repo working tree. See "Database
placement" below.

### Deploy process

A `deploy.sh` in the repo, run on the server as root:

1. `git pull`
2. Install dependencies if `requirements.txt` changed
3. Run pending schema migrations
4. Seed teams and crests if the table is empty
5. `systemctl restart` the three units, gunicorn first, then the workers
6. **Verify against the running system**

Step 6 is the one experience argued for. Every step exiting zero is not
evidence of a working deploy: the seed script once printed `total 205` while
writing to a database file the web process could not see, and a checklist that
trusts exit codes would have called that a success.

So the script asks the application what it can actually observe: units active,
privacy page 200, stylesheet 200, and the count of teams visible through the
same config loader gunicorn uses. It also refuses to start if the env file has
CRLF line endings, and probes that `/.well-known/` is not proxied to gunicorn.

Three units, not two: `sportscrawl-web`, `sportscrawl-scheduler`, and
`sportscrawl-poller`.

Restarting the scheduler mid-batch must be safe. The per-message
`last_sent_date` commit described in section 7 is what makes it safe, so a
deploy at 7:05am resumes rather than re-sending.

### Liveness

Added after the first unattended run failed. The scheduler was alive for eight
hours, held the correct database, and classified the due subscriber correctly
when asked directly — but delivered nothing and logged nothing. `systemctl
is-active` said `active` throughout.

Two properties every long-running loop here needs:

- **An idle loop must look different from a wedged one.** Python block buffers
  stdout when it is not a TTY, so both workers had produced no output at all
  since starting. `PYTHONUNBUFFERED=1` on every unit, explicit `flush=True`,
  and a heartbeat: the first completed tick, then one line every fifteen
  minutes. systemd's `Started` only means the process spawned.
- **A wedged loop must not wait to be noticed.** `Restart=always` only helps if
  the process dies, and this one never did. A watchdog thread exits the process
  when no tick has completed in five minutes and lets systemd restart it.

Restarting mid-batch is safe, so a watchdog restart costs nothing: the
per-message `last_sent_date` commit in section 7 makes it resume rather than
re-send. Verified — the redeploy immediately after that morning's late digest
did not send a second one.

### Migrations

The database persists across deploys, so schema changes need a plan from the
start. A `migrations/` directory with numbered SQL files and a
`schema_version` table is sufficient. Do not rely on an ORM's auto-create,
which will not alter existing tables. Every migration must be safe to run
twice.

### Isolation

Own system user, own container or venv, not root. Separate vhost with its own
certificate. No shared stack with any WordPress or PHP site on the box. If
that host is ever compromised, the bot's app password must not be readable
from that context.

### Gunicorn

```
gunicorn -w 2 -b 127.0.0.1:8000 app:app
```

Bind to loopback only, never `0.0.0.0`. Apache proxies to it. firewalld exposes
80 and 443 and nothing else. Two workers is ample for a config page.

### systemd

Two units: one for gunicorn, one for the scheduler. If running as user-level
services without root, `loginctl enable-linger {{SERVICE_USER}}` is required
or they die when the login session ends.

### SELinux

**Currently Disabled on this host**, so none of the following is needed today.
Keep it written down anyway: if SELinux is ever set back to enforcing,
Apache-to-gunicorn proxying fails with a 503 and nothing useful in the app log
until:

```
setsebool -P httpd_can_network_connect 1
```

If any app directory is served directly by Apache it also needs
`httpd_sys_content_t` labeling.

Do not enable SELinux as part of this project. Flipping it on a box running
other people's live sites, to benefit a bot that hasn't launched, is somebody
else's outage.

### Python

Alma 8 ships **3.6.8** as system Python, and 3.6 is too old — `zoneinfo` alone
rules it out, and this project is mostly timezone arithmetic.

The newest AppStream module on this host is **python39**:

```
dnf module install python39
/usr/bin/python3.9 -m venv {{APP_DIR}}/.venv
```

3.9 is the target, so: no `match`, no PEP 604 unions at runtime, and
`from __future__ import annotations` at the top of any module using `X | None`
in a signature — including the `Digest` dataclass in section 6. `zoneinfo` is
in the 3.9 standard library, which is the version floor that matters here.

Do not upgrade or symlink over system Python; dnf depends on it.

### SQLite version floor

The system SQLite is **3.26.0**, from 2018, and Python's `sqlite3` links
against it. Two consequences for the schema in section 4:

- No `RETURNING` (needs 3.35). Use `lastrowid` or a follow-up select.
- No `STRICT` tables (needs 3.37). Type affinity is advisory; validate in
  Python.

WAL, `busy_timeout`, upsert, and window functions all predate 3.26 and are
fine. Check this before writing the first migration rather than discovering it
when one fails on the server but passed on the laptop.

### Database placement

The SQLite file lives outside the document root, at
`/home/sportscrawl/data/sportscrawl.db`. Own directory, mode 700, owned by
`{{SERVICE_USER}}`. It is a table of DIDs mapped to delivery times, and it must
not be fetchable over HTTP.

`/home/sportscrawl/public_html` is the Apache-served docroot per the host
convention. Neither `{{APP_DIR}}` nor the data directory is beneath it. Verify
this with an actual request to `https://sportscrawl.app/data/sportscrawl.db`
after deploy; a convention is not a control.

### Backup

Nightly copy to the home NAS. Pull from the server rather than pushing, so a
compromised server cannot write to the NAS. Subscriber data is the only
irreplaceable state; everything else is refetchable.

## 14. Build order

Steps 1 through 4 produce a working service for a single user. That is enough
to find out whether a morning sports digest is something worth reading, before
any work goes into serving other people.

1. **Digest formatter** against saved ESPN JSON fixtures. Pure function, no
   network. This is where the real design work is.
2. **ESPN adapter** with the three-day window, per-league cache, and parse
   validation.
3. **Scheduler** writing to stdout instead of DMs.
4. **Chat send**, to one DID only. Run for a week.
5. **Command handling** and help response. The `events` table gets written from
   here, before the admin page that reads it exists. If event logging lands
   last, the ten testers in section 16 have no signup cohort and the first two
   weeks of retention data — the only cohort that will ever be observed from a
   standing start — is gone.
6. **Config page**, then admin stats, then privacy policy.

## 15. Testing

Priority order:

- Formatter against fixtures, including: empty slate, single game, World Cup
  group day overflow, a late Pacific game that ESPN buckets to tomorrow, a game
  starting 22:00 local that ends after local midnight, an early kickoff already
  final at a late `send_time`, and each branch of the team-order rule.
- Date boundary conversion across all four US timezones, and across a DST
  transition, including a `send_time` inside the spring-forward gap.
- Delete-then-restart cycle against a fixture database.
- Failure notice throttling: one outage must produce at most one message per
  subscriber. Include the partial case — one league down, one healthy — and
  assert the commit table in section 7.
- Scheduler crash mid-batch, verifying resume does not re-send.
- Poll loop restart mid-conversation: replaying `getMessages` from the stored
  cursor must not re-execute a command or re-send help.
- `listConvos` surfaces message requests, per section 8. Cold DM from a second
  account, against the live API, before launch. This is the one test that
  cannot be written against a fixture.
- Competition auto-close does not fire while `consecutive_failures` is
  non-zero.

## 16. Launch

Done:

- Domain registered, resolving, hosted account created
- Bluesky account created, handle set to `sportscrawl.app` and DNS-verified
- DM setting open to everyone

Remaining:

- Profile display name, avatar, and bio explaining what the account does, with
  the config link
- Ten known testers for two weeks before any public announcement
- Then a cruftbox post and a Bluesky post

## 17. Known ceilings

Stated so they are not rediscovered as surprises:

- **Text only, permanently.** Image support in Bluesky DMs has been on the
  roadmap since May 2024 with no sign of arriving. Scores are natively textual
  so this is fine, but the feature set is structurally limited to what fits in
  a text message.
- **The scaling limit is Bluesky's, not the server's.** One VPS with SQLite
  will serve thousands of subscribers comfortably. Sending thousands of DMs
  from one account in a morning window is a per-account limit on someone
  else's service. If that becomes the constraint, the answer is multiple bot
  accounts or a conversation with Bluesky, not a rewrite.
- **`chat.bsky.*` is centralized Bluesky infrastructure**, not on-protocol DMs.
  The atproto part of this project is identity and the inbound command
  channel. Delivery depends on Bluesky the company.
- **ESPN can disappear without notice.** No contract, no versioning. If it
  goes, the project stops rather than taking on a paid data source.
