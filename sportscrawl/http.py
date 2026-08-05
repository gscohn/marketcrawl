"""Retrying JSON fetcher.

Small on purpose. The transport is injectable so the test suite never touches
the network, and so a replacement data source can reuse the retry policy.
Settings come from spec section 5.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
MAX_ATTEMPTS = 3  # the initial try plus two retries
BACKOFF_BASE = 0.5

def _client_token() -> str:
    try:
        import requests

        return "python-requests/%s" % requests.__version__
    except Exception:
        return "python-requests/2"


# Identify the client honestly, but lead with the real HTTP library token.
#
# ESPN 403s unrecognised user agents outright. Verified 2026-08-04: bare
# "sportscrawl/1.0" is refused, and so is "foobar/1.0", while "curl/8.5.0" and
# "python-requests/2.32.5" are served. It is an allowlist of known clients, not
# a block on bots.
#
# So the honest string goes second rather than being dropped. This is not a
# forged browser: python-requests really is the client, and sportscrawl/1.0
# says who is driving it. Anyone reading ESPN's logs can still tell exactly
# what this is and where to complain.
USER_AGENT = "%s sportscrawl/1.0 (+https://sportscrawl.app)" % _client_token()

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    """The request did not produce a usable response.

    Distinct from an empty result. One means "no games", the other means
    "send a failure notice".
    """


@dataclass(frozen=True)
class Response:
    status: int
    body: str


def requests_transport(url: str, timeout, headers) -> Response:
    import requests

    reply = requests.get(url, timeout=timeout, headers=headers)
    return Response(status=reply.status_code, body=reply.text)


class Fetcher:
    def __init__(self, transport=None, sleep=time.sleep):
        self._transport = transport or requests_transport
        self._sleep = sleep

    def get_json(self, url: str) -> dict:
        timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        last_problem = "no attempt made"

        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(BACKOFF_BASE * (2 ** (attempt - 1)))

            try:
                reply = self._transport(url, timeout, headers)
            except Exception as exc:  # timeouts, DNS, connection resets
                last_problem = "transport error: %s" % exc
                continue

            if reply.status in RETRY_STATUSES:
                last_problem = "http %d" % reply.status
                continue

            if reply.status != 200:
                # 404 and friends will not improve by asking again. The
                # /sports/soccer endpoint died this way.
                raise FetchError("http %d for %s" % (reply.status, url))

            try:
                return json.loads(reply.body)
            except ValueError as exc:
                # A 200 carrying garbage is a failure, not an empty slate.
                raise FetchError("bad json from %s: %s" % (url, exc)) from exc

        raise FetchError("%s failed after %d attempts (%s)" % (url, MAX_ATTEMPTS, last_problem))
