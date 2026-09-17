"""Chat body cleaning and send pacing (Fremkit wave 11).

Two halves, both of them the whole filter policy for this wave (owner ruling
2026-09-04: 500 characters, links replaced, @everyone and @here stripped, a
per-account rate limit, and NO word list):

  * `clean_body(text) -> (ok, cleaned, reason)`, pure. What comes out is what
    gets stored: the cleaning is not a rendering hint the page could decide to
    ignore. Refuses with the refusal TOKEN the route hands back verbatim.
  * the rate windows, which count rows in portal_chat_messages. There is no
    in-memory counter anywhere here on purpose: lastsietch-admin restarts, and a
    restart that resets a spam brake is a brake a spammer can ask for.

A link is never refused. A message is a sentence with a URL in it, and refusing
the whole send teaches players to work around the filter. Wave 11.1 splits what
happens to it: a host on ALLOWED_HOSTS survives VERBATIM so the page can make it
clickable, and everything else collapses to '[link: host]' so the reader can
still see where they were being sent without being able to click it. The same
reasoning covers @everyone: it is removed, not refused.

Nothing here logs a body, cleaned or raw.
"""
import ipaddress
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from database import get_db

# Cleaned length, the owner's number. MAX_RAW is the pre-clean bound: cleaning
# normalises and scans the whole string, so an unbounded body would be work done
# before the length refusal rather than instead of it. It is deliberately well
# above MAX_BODY, so the 501st character is refused by the real cap and not by
# this one.
MAX_BODY = 500
MAX_RAW = 4000
# "keep single newlines, max 5" -> at most five line breaks, so six lines.
MAX_LINES = 6

# Two brakes, both per account. Ten in thirty seconds is the burst ceiling; one
# per second is what stops a held Enter key from spending the whole burst in one
# frame. An identical body inside ten seconds is a double-send, not a burst.
RATE_WINDOW_S = 30
RATE_MAX_IN_WINDOW = 10
MIN_INTERVAL_S = 1
DUPLICATE_WINDOW_S = 10

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"@(?:everyone|here)", re.IGNORECASE)
# Every C0/C1 control except the newline, plus the zero-width, bidi-override
# and word-joiner codepoints a handle can be smuggled through. Written as
# escapes rather than as the characters themselves: an invisible codepoint
# inside a character class is a line no reviewer can check. A tab is left to
# the whitespace collapse below.
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")
_HSPACE_RE = re.compile(r"[^\S\n]+")
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
# What a resolvable name is allowed to be made of. urlsplit is not a browser and
# does not treat a backslash as an authority terminator, so
# 'https://lastsietch.com\evil.test/' parses HERE with the whole thing as the
# host and THERE as our host plus a path. Requiring the host to be plain
# hostname characters is what keeps that disagreement on the refusing side.
_HOSTNAME_RE = re.compile(r"[a-z0-9.-]+")

LINK_TOKEN = "[link]"

# --------------------------------------------------------------------------- #
# The link allowlist (wave 11.1, owner ruling 2026-09-05).
#
# This tuple is duplicated in src/lib/chat/links.js on purpose: the page decides
# what to turn into an <a> from its OWN copy and never from the stored body, so
# a server that is wrong (or a row stored before this wave) cannot talk the
# client into rendering a live anchor. Two lists that must agree is a test, and
# both suites pin them against each other.
#
# lastsietch.com is the only entry that also covers subdomains. The others are
# third parties whose subdomains we do not control, and www.youtube.com is
# listed by name rather than as a wildcard for exactly that reason.
# --------------------------------------------------------------------------- #
ALLOWED_HOSTS = (
    "lastsietch.com",
    "discord.com",
    "discord.gg",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "duneawakening.wiki.gg",
    "dune-awakening.fandom.com",
)
WILDCARD_HOSTS = ("lastsietch.com",)

# A kept URL is capped well under MAX_BODY: a 400-character tracking URL is a
# wall of text in the middle of a sentence, and the host is the part a reader
# needs. Over the cap it degrades to the same token an unlisted host gets.
MAX_LINK_CHARS = 200
# The DNS limit. Anything longer did not come from a name resolver, so it is not
# a host worth echoing back inside the token.
MAX_HOST_CHARS = 253


def url_host(url: str) -> str:
    """The lowercased host of one matched URL token, or '' when none parses.

    urlsplit's `hostname` does the work: it lowercases, drops userinfo and the
    port, and unwraps an IPv6 literal's brackets. That matters more than it
    looks. 'https://lastsietch.com@evil.test/' has a host of evil.test and a
    reader scanning the text sees ours, so the allowlist has to be asked about
    the host a BROWSER would resolve and never about the prefix of the string."""
    text = url if _SCHEME_RE.match(url) else "//" + url
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host if len(host) <= MAX_HOST_CHARS else ""


def url_userinfo(url: str) -> bool:
    """Whether the authority carries a user or a password.

    Asked of the AUTHORITY and never of the whole string. '@' is an ordinary
    path character and 'https://www.youtube.com/@LastSietch' is how every
    YouTube channel link is written, so a substring test here would collapse the
    most-pasted allowlisted URL on the list. A parse that raises is treated as
    userinfo: an authority nothing can read is not one to keep verbatim."""
    text = url if _SCHEME_RE.match(url) else "//" + url
    try:
        parts = urlsplit(text)
        return parts.username is not None or parts.password is not None
    except ValueError:
        return True


def host_allowed(host: str) -> bool:
    """Whether a URL on this host survives verbatim.

    An IP literal and a punycode label are refused BEFORE the list is consulted.
    Neither can ever be on it, and both are ways to show a reader one name and
    send them to another."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(host):
        return False
    if any(label.startswith("xn--") for label in host.split(".")):
        return False
    if host in ALLOWED_HOSTS:
        return True
    return any(host.endswith("." + base) for base in WILDCARD_HOSTS)


def _keeps_url(url: str, host: str) -> bool:
    """Whether this exact string survives as itself.

    Userinfo disqualifies a URL even on an allowlisted host. The host is where
    the click GOES and the allowlist has already approved it, but the string is
    what the reader SEES, and 'https://your-bank.example@discord.gg/x' reads as
    a bank link to everyone who is not parsing URLs for a living."""
    if url_userinfo(url):
        return False
    if len(url) > MAX_LINK_CHARS:
        return False
    return host_allowed(host)


def _link_sub(match) -> str:
    url = match.group(0)
    host = url_host(url)
    # A host that is not made of hostname characters (an IPv6 literal, or the
    # backslash disagreement above) is not a name worth showing the reader, and
    # echoing it back verbatim would put whatever it contains in the message.
    # It degrades to the bare token instead.
    if not host or not _HOSTNAME_RE.fullmatch(host):
        return LINK_TOKEN
    if _keeps_url(url, host):
        return url
    return "[link: %s]" % host


def clean_body(text):
    """(ok, cleaned, reason). `reason` is None on success and a refusal token on
    failure; this wave only ever refuses 'bad_request', which covers a body that
    is not a string, is too long, or is empty once the filters have run.

    Order is load-bearing. Links are resolved BEFORE whitespace collapses, so a
    URL broken across a newline is two tokens rather than one greedy match, and
    mentions are stripped before the trim so '@everyone' alone refuses as empty
    instead of storing a blank line."""
    if not isinstance(text, str):
        return False, "", "bad_request"
    if len(text) > MAX_RAW:
        return False, "", "bad_request"

    out = unicodedata.normalize("NFC", text)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _CONTROL_RE.sub("", out)
    out = _URL_RE.sub(_link_sub, out)
    out = _MENTION_RE.sub("", out)

    lines = [_HSPACE_RE.sub(" ", line).strip() for line in out.split("\n")]
    lines = [line for line in lines if line]
    if len(lines) > MAX_LINES:
        # The overflow is folded onto the last kept line rather than dropped: a
        # filter that silently eats the end of a message is worse than one that
        # reflows it.
        lines = lines[:MAX_LINES - 1] + [" ".join(lines[MAX_LINES - 1:])]
    cleaned = "\n".join(lines).strip()

    if not cleaned:
        return False, "", "bad_request"
    if len(cleaned) > MAX_BODY:
        return False, "", "bad_request"
    return True, cleaned, None


# --------------------------------------------------------------------------- #
# Rate windows. Backed by portal_chat_messages itself, so they survive a restart.
# --------------------------------------------------------------------------- #

def _borrow(conn):
    """(handle, should_close). A helper called INSIDE somebody else's write
    transaction must never open a second connection: SQLite would queue the new
    one behind a lock its own caller is holding, which is a self-deadlock."""
    if conn is not None:
        return conn, False
    return get_db(), True


def _now():
    return datetime.now(timezone.utc)


def _stamp(when) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def recent_send_times(account_id: int, limit: int = RATE_MAX_IN_WINDOW, conn=None):
    """This account's last `limit` send times, newest first. Deleted messages
    count: a spammer deleting their own rows must not buy back the window."""
    handle, close = _borrow(conn)
    try:
        rows = handle.execute(
            "SELECT created_utc FROM portal_chat_messages"
            " WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (int(account_id), int(limit)),
        ).fetchall()
    finally:
        if close:
            handle.close()
    return [r["created_utc"] for r in rows]


def retry_after(account_id: int, now=None, conn=None) -> int:
    """Whole seconds this account must wait, or 0 when it may send. The larger
    of the two brakes, and never 0 when a brake is engaged: a countdown the page
    renders as "0" is a button that looks enabled and is not."""
    now = now or _now()
    times = [t for t in (_parse(v) for v in recent_send_times(account_id, conn=conn))
             if t is not None]
    if not times:
        return 0

    wait = 0.0
    since_last = (now - times[0]).total_seconds()
    if since_last < MIN_INTERVAL_S:
        wait = max(wait, MIN_INTERVAL_S - since_last)

    in_window = [t for t in times if (now - t).total_seconds() < RATE_WINDOW_S]
    if len(in_window) >= RATE_MAX_IN_WINDOW:
        oldest = min(in_window)
        wait = max(wait, RATE_WINDOW_S - (now - oldest).total_seconds())

    return max(1, int(math.ceil(wait))) if wait > 0 else 0


def is_duplicate(account_id: int, cleaned: str, now=None, conn=None) -> bool:
    """The same CLEANED body from the same account inside the duplicate window.
    Compared after cleaning so two sends that differ only in trailing spaces are
    the one message the reader would have seen twice."""
    now = now or _now()
    cutoff = _stamp(now - timedelta(seconds=DUPLICATE_WINDOW_S))
    handle, close = _borrow(conn)
    try:
        row = handle.execute(
            "SELECT 1 FROM portal_chat_messages"
            " WHERE account_id = ? AND body = ? AND created_utc >= ? LIMIT 1",
            (int(account_id), str(cleaned), cutoff),
        ).fetchone()
    finally:
        if close:
            handle.close()
    return row is not None
