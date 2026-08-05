# Backup and monitoring

Subscriber data is the only irreplaceable state in this project. Scores,
rosters and crests are all refetchable; team selections are not, and the
`events` table is the retention record the whole experiment exists to gather.
Losing it does not break the service, it ends the measurement.

Everything below is running. Two things need a human, and they are marked
**ACTION** and listed again at the bottom.

## The chain

```
host2  12:00 America/New_York   sportscrawl-backup.timer
                                  -> backup.sh
                                  -> /home/sportscrawl/backups/sportscrawl-<ts>.db.gz
                                     (14 days retained)

bandersnatch  10:00 America/Los_Angeles   cron
                                  -> /share/Backup/sportscrawl-pull.sh
                                  -> rsync over ssh, read only
                                  -> /share/Backup/sportscrawl/
                                     (90 days retained)
```

An hour of margin sits between the two. The server stages a fortnight; the NAS
holds the long copy.

## Why it is a pull

The NAS reaches into the server, never the other way round. The server holds
no credential for the NAS, so an attacker who takes the server cannot reach,
alter or delete the backups. That property is the whole reason for the
awkwardness, and it is worth keeping.

The key is restricted further on the server side. `~scbackup/.ssh/authorized_keys`
forces every session through `/home/scbackup/backup-pull-only`, which accepts
exactly one shape of command:

```
rsync --server --sender … /home/sportscrawl/backups/…
```

Anything else is refused. Verified: `ssh … "cat /home/sportscrawl/.env"` returns
`this key may only pull /home/sportscrawl/backups/`.

## Why a dedicated account

The first attempt authorised the key on the `sportscrawl` service account and
failed with `protocol version mismatch -- is your shell clean?`. The cause was
not rsync: that account has `/sbin/nologin`, so the forced command never ran
and the nologin message was written into rsync's protocol stream.

Giving the service account a shell would have fixed it and weakened the thing
worth protecting. Instead `scbackup` exists solely to be pulled from. It is in
the `sportscrawl` group, the backup directory is `750`, and the database
directory stays `700`, so the pull account can read snapshots and cannot read
the live database or the env file. The service account now has no
`authorized_keys` at all.

## Why not `cp`

`backup.sh` does **not** copy the database file. The live database is in WAL
mode, so committed data is split between `sportscrawl.db` and
`sportscrawl.db-wal`. At the time of writing that is a 4KB main file and a 3MB
write-ahead log. A `cp` of the `.db` alone produces a file that opens without
error, reports no corruption, and is missing nearly everything.

It uses SQLite's backup API, which walks the live database and writes one
consistent file while the scheduler and poller keep writing.

Every snapshot is then opened, `PRAGMA integrity_check` is run, and rows are
counted before older snapshots are pruned. A backup nobody has opened is a
hypothesis.

## Restoring

```bash
systemctl stop sportscrawl-web sportscrawl-scheduler sportscrawl-poller
cd /home/sportscrawl/data
mv sportscrawl.db sportscrawl.db.displaced          # keep the old one
gunzip -c /path/to/sportscrawl-<timestamp>.db.gz > sportscrawl.db
chown sportscrawl:sportscrawl sportscrawl.db
rm -f sportscrawl.db-wal sportscrawl.db-shm         # stale, belong to the old file
systemctl start sportscrawl-web sportscrawl-scheduler sportscrawl-poller
/home/sportscrawl/app/deploy.sh                     # verifies what the app can see
```

Deleting the stale `-wal` and `-shm` matters. They belong to the replaced
database and SQLite will try to apply them to the restored one.

Restoring loses everything since the snapshot: at most a day of subscriptions
and one day of events. `last_sent_date` moving backwards means some
subscribers may receive a second copy of a digest they already had, which is
the right way round for this failure.

**Test a restore once before it is needed**, into a scratch path rather than
over the live database.

## Monitoring

`daily_report.py` DMs the operator once a day: subscriber counts, digests and
failure notices in the last 24 hours, churn if any, data source problems if
any, and the age of the newest backup. It names no subscriber and stays under
the 1000 grapheme DM cap.

It shouts in three cases: no backup found, a backup older than 36 hours, or
any league with consecutive fetch failures. On a normal day it is five boring
lines, which is the point. A report that lists every healthy league every
morning trains you to skim past the one line that matters.

This is monitoring by DM rather than by dashboard because the operator reads
DMs already, and the admin page requires deciding to look.

## What is not covered

- **The env file is not backed up.** It holds the app password, the Flask
  secret and `EVENTS_PEPPER`. Losing the pepper severs every existing
  `events` row from its subscriber, which destroys the retention join even
  though the rows survive. It is deliberately excluded from an automated copy
  because it is a full-access credential; keep it in a password manager.
- **Crests and rosters** are refetchable, so they are not in the snapshot.
  `deploy.sh` reseeds them when the table is empty.
- **Bluesky conversation history** is not ours to back up or delete.

## Permissions, and a failure worth remembering

The backup directory is `750`, group `sportscrawl`, and snapshots are `640`.
Group readable so `scbackup` can pull them; never world readable, because they
contain every subscriber's DID.

An early version of `backup.sh` ran `chmod 700` on that directory on every
run. The first pull succeeded, against a directory that had been widened by
hand, and the next snapshot silently closed it again. rsync then failed with
`change_dir "/home/sportscrawl/backups" failed: Permission denied (13)`, and
only the first night's copy would ever have existed.

This is the shape backup failures usually take: they work once, under
conditions nobody wrote down, and stop without telling anyone. It was caught
by running the whole chain rather than each half, and the daily report now
catches the residue by shouting when the newest snapshot is over 36 hours old.

## ACTION: one thing still needs a human

**Separate app password for the server.** Production still shares the one on
straylight, so revoking either takes down the other and Bluesky offers no way
to tell them apart.

The NAS cron is live: `0 10 * * * /share/Backup/sportscrawl-pull.sh`, loaded
into the running daemon and persisted in `/etc/config/crontab`.
