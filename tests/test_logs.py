"""Logging tests.

The command log existed for a day writing nothing: logging.getLogger with
nothing configured discards INFO records silently, so the privacy policy's
claim about what reaches the server log was untrue.
"""

from __future__ import annotations

import logging

from sportscrawl import logs
from sportscrawl.chat import Message
from sportscrawl.commands import CommandHandler
from sportscrawl.db import connect, migrate


def reset():
    """Undo configure() completely.

    Removing handlers is not enough: setLevel(INFO) persists on the logger and
    would make the unconfigured case look configured, which is exactly the
    distinction under test.
    """
    logs.CONFIGURED = False
    root = logging.getLogger("sportscrawl")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.NOTSET)
    root.propagate = True


def test_info_records_are_discarded_without_configuration():
    reset()
    assert logging.getLogger("sportscrawl.commands").getEffectiveLevel() > logging.INFO


def test_configure_makes_info_records_emit(capsys):
    reset()
    logs.configure()
    logging.getLogger("sportscrawl.commands").info("hello")
    assert "hello" in capsys.readouterr().out


def test_configure_is_idempotent(capsys):
    """gunicorn imports the app in several workers; a handler per import would
    duplicate every line."""
    reset()
    logs.configure()
    logs.configure()
    logging.getLogger("sportscrawl.commands").info("once")
    assert capsys.readouterr().out.count("once") == 1


def test_the_command_log_records_the_did_and_command_not_the_body(tmp_path, capsys):
    """Spec section 12: the DID and which command matched, never the text."""
    reset()
    logs.configure()
    db = connect(str(tmp_path / "log.db"))
    migrate(db)
    handler = CommandHandler(db, "pepper", "https://sportscrawl.app")

    from datetime import datetime, timezone
    secret = "please cancel my subscription immediately"
    handler.handle(
        Message(message_id="m1", convo_id="c1", sender_did="did:plc:alice",
                text=secret, sent_at=""),
        datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc),
    )

    out = capsys.readouterr().out
    assert "did=did:plc:alice" in out
    assert "matched=unknown" in out
    assert secret not in out
    assert "subscription" not in out
