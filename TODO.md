# Sportscrawl TODO

Outstanding work, roughly in the order it should happen. The spec is the
design; this is what has not been built or decided yet.

## Before testers

- [ ] **Separate Bluesky app password for the server.** Production currently
      shares the one on straylight, so revoking either takes down the other and
      Bluesky gives no way to tell them apart. Two minutes in Settings →
      Privacy and Security → App Passwords, with DM access ticked, then swap it
      into `/home/sportscrawl/.env` and restart.
- [ ] **Watch one clean 07:00 run.** The first unattended run stalled silently;
      the heartbeat and watchdog went in afterwards but have not yet proven
      themselves through a morning.
- [ ] Ten known testers, two weeks, before any public post (spec section 16).

## Women's World Cup 2027

The tournament is **June–July 2027 in Brazil**. ESPN slug is `fifa.wwc`,
confirmed present in the competitions dropdown. It returns zero events today,
which is correct: it has not been drawn yet.

- [ ] Add `soccer/fifa.wwc` to `COMPETITIONS` in `app.py` and to
      `SOCCER_LEAGUES_AT_LAUNCH` in `sportscrawl/source.py`.
- [ ] **Fix the competition lifecycle rule first — see below.** Adding the
      Women's World Cup before that is actively harmful: a subscription taken
      in 2026 for a 2027 tournament is exactly the case the current rule would
      throw away.

Timing choice, worth making deliberately: add it now so early subscribers can
opt in and simply hear nothing until the tournament starts, or add it a month
out so the picker is not cluttered with a competition that cannot produce a
line for the better part of a year. The empty digest rule means an early
subscription costs the subscriber nothing.

## Competition lifecycle is unimplemented, and the rule is wrong

Spec section 10 says a competition subscription auto-closes when its slug has
returned no events for 14 days. `league_state.last_event_date` is tracked and
surfaced on the admin page, but **nothing acts on it** — no auto-close exists.

That is fortunate, because the rule as written cannot tell **"this tournament
has finished"** from **"this tournament has not started yet."** Both look like
a slug with no recent events. Implemented as specified, it would close every
Women's World Cup subscription about two weeks after someone made it, ten
months before a ball was kicked, and send a message saying the competition had
ended.

- [ ] Decide the distinguishing signal. Candidates: only close a competition
      that has *ever* returned an event to us (`last_event_date` is not null),
      which is enough to protect a future tournament; or read a season end date
      from the scoreboard payload rather than inferring from absence.
- [ ] Implement auto-close, with the closing message from section 10.
- [ ] Test both directions: a finished tournament closes, a not-yet-started one
      survives indefinitely.

## Smaller

- [ ] Collapsed league summaries could show selected teams as chips instead of
      auto-expanding. A returning subscriber with picks in every league gets all
      eight drawers open and loads all 205 crests — about 208 requests against
      3 for a new subscriber.
- [ ] `MIN_BUCKET` suppression makes the admin page mostly dashes at current
      scale. `ADMIN_MIN_BUCKET` can lower it for testing; decide whether that
      stays.
- [ ] Bootstrap is 232KB and now dominates first paint. A subset would cut most
      of it, at the cost of a build step this project does not have.
- [ ] Backup: nightly pull of the SQLite file to bandersnatch, pulled from the
      NAS rather than pushed, so a compromised server cannot write to it
      (spec section 13). Not set up.

## Known ceilings, not tasks

See spec section 17. Text only forever, Bluesky's per-account send limits, and
ESPN disappearing without notice are accepted constraints rather than work.
