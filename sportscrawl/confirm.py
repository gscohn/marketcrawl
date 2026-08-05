"""The message sent after someone saves their settings.

Without it, a subscriber who configures at 2pm for a 5:30am delivery hears
nothing for fifteen hours and has no way to tell a working setup from a broken
one. That silence is the most likely moment for someone to conclude the thing
does not work.

Pure: text in, text out, no network and no database.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _clock(hour: int, minute: int) -> str:
    """5:30am, 7:00am. Matches the config page's own wording."""
    return "%d:%02d%s" % (hour % 12 or 12, minute, "am" if hour < 12 else "pm")


def next_delivery(send_time: str, tz: str, now: datetime, already_sent_today=False):
    """When the next digest is due, in the subscriber's own timezone.

    Returns (local_datetime, "today"|"tomorrow").
    """
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    hour, minute = (int(part) for part in send_time.split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if already_sent_today or due <= local:
        return due + timedelta(days=1), "tomorrow"
    return due, "today"


def confirmation_text(send_time, tz, team_count, competition_count, now,
                      already_sent_today=False) -> str:
    when, day = next_delivery(send_time, tz, now, already_sent_today)
    hour, minute = (int(part) for part in send_time.split(":"))
    clock = _clock(hour, minute)

    lines = ["Settings saved."]

    if not team_count and not competition_count:
        # Saying "your first digest arrives tomorrow" to someone who picked
        # nothing would be a promise the scheduler will not keep.
        lines.append("")
        lines.append(
            "You have not picked any teams yet, so nothing will be sent. "
            "Open the settings link again to choose some."
        )
        return "\n".join(lines)

    picks = []
    if team_count:
        picks.append("%d team%s" % (team_count, "" if team_count == 1 else "s"))
    if competition_count:
        picks.append(
            "%d competition%s"
            % (competition_count, "" if competition_count == 1 else "s")
        )

    lines.append("")
    lines.append("%s, delivered at %s." % (" and ".join(picks), clock))
    lines.append("")
    lines.append(
        "Your next digest arrives %s at %s. Quiet days are normal: nothing is "
        "sent when none of your teams play." % (day, clock)
    )
    return "\n".join(lines)
