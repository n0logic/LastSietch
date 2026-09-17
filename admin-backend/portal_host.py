"""The V2 portal served at the ROOT of its own host (wave 13a).

Until now the SPA lived at `lastsietch.com/portal/v2/*` and Classic lived at
`lastsietch.com/portal/*`. Both now answer on `portal.lastsietch.com`, with the
SPA at that host's root, so one address holds one login and every old deep link
301s to its new home.

Nothing about the app moved to make that true. `portal_v2_spa` still serves
`/portal/v2/...` and every portal router still owns `/portal/...`; the
middleware below rewrites the incoming path on a root host so the SAME routes
answer at the root. Keeping the routing table where it is means the legacy host
keeps working byte for byte during and after the cutover, and the redirect
table has exactly one implementation instead of one here and one in Caddy.

Pure stdlib apart from `config`, so a suite can import it with no fastapi. The
middleware is raw ASGI for the same reason and because it must run OUTSIDE the
`security_headers` middleware: that one picks the CSP off `request.url.path`,
so the path has to be rewritten before it looks.
"""
import re
from urllib.parse import quote

from config import DISCORD_OAUTH_REDIRECT_URI, PORTAL_ROOT_HOSTS

# The hosts that serve the SPA at their root. Compared against the `Host` header
# value, lowercased, EXACTLY: `portal.lastsietch.com:8078` is a different value
# and is not one of these unless the operator listed it.
ROOT_HOSTS = PORTAL_ROOT_HOSTS

V2_PREFIX = "/portal/v2"
# Paths that keep their spelling on a root host: the Classic pages and the OAuth
# flow under /portal/, the read API the SPA calls, and the admin app's static
# mount that the portal templates load their CSS from.
PASS_PREFIXES = ("/portal/", "/api/dune/", "/static/")
RETURN_TO_MAX = 512

# A bare DNS name: at least two labels of letters, digits and hyphens. `localhost`
# has no dot and an IPv4 literal is all digits, so neither survives this, which is
# the point: an OAuth redirect URI has to name a host Discord can be told about.
_DNS_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Query bytes a URL may carry raw. `query_string` reaches a raw ASGI middleware
# exactly as the client sent it, so a CR or an LF in there would be a second
# response header of the caller's choosing. This range leaves an already
# percent-encoded query byte for byte (it includes `%`, `&` and `=`, so nothing
# is escaped twice) and escapes everything else, spaces and controls included.
_QUERY_RAW_MIN = 0x21
_QUERY_RAW_MAX = 0x7E

# Sub-delimiters a path may carry raw. Everything else in a rewritten Location is
# percent-encoded, because scope["path"] arrives already decoded and a literal
# `%` or `?` in a path segment would otherwise change what the browser asks for.
_LOCATION_SAFE = "/@:,;=+$&!*'()~"


# The default ports. A browser sends `Host: portal.lastsietch.com` for :443, but
# a proxy or a probe may spell it out and the two name the same host. Any OTHER
# port is left alone, so an operator who listed `host:8078` still matches it.
_DEFAULT_PORTS = (":443", ":80")


def _normalise(host) -> str:
    """The `Host` header reduced to the value the list is compared against."""
    if not isinstance(host, str):
        return ""
    name = host.strip().lower()
    for port in _DEFAULT_PORTS:
        if name.endswith(port):
            name = name[:-len(port)]
            break
    # `portal.lastsietch.com.` is the fully qualified spelling of the same name.
    # A resolver treats the trailing root label as noise and so must this, or the
    # one character turns the portal host back into an unknown one.
    return name.rstrip(".")


def is_root_host(host) -> bool:
    """True when this `Host` header names a host that serves the SPA at its root."""
    return _normalise(host) in ROOT_HOSTS


def decide(path: str):
    """(verb, target) for one path on a root host.

    verb is "redirect" (answer 301 here, the caller re-attaches the query),
    "pass" (the path already spells a real route) or "rewrite" (hand the app
    the /portal/v2 path that this root path stands for)."""
    if path == V2_PREFIX or path.startswith(V2_PREFIX + "/"):
        # The old deep links. `/portal/v2guilds` is NOT one of them and falls
        # through to the pass rule below, which is why this tests the separator
        # rather than a bare prefix.
        #
        # EVERY leading separator on the remainder collapses to one.
        # `/portal/v2//evil.example.com/x` would otherwise redirect to
        # `//evil.example.com/x`, which a browser reads as another ORIGIN: an
        # open redirect on the one address every old link points at. Backslashes
        # go with the slashes because a browser normalises them into slashes
        # before it resolves the Location.
        return ("redirect", "/" + path[len(V2_PREFIX):].lstrip("/\\"))
    if path == "/portal" or path == "/portal/":
        # Classic's own landing redirected to the SPA; on this host the SPA is
        # the root, so the hop lands there directly.
        return ("redirect", "/")
    if path.startswith(PASS_PREFIXES):
        return ("pass", path)
    return ("rewrite", V2_PREFIX + path)


def v2_home(host) -> str:
    """Where "the V2 app" is on this host. Legacy hosts keep today's answer;
    after the cutover the edge never sends them one of these."""
    return "/" if is_root_host(host) else "/portal/v2/"


def oauth_redirect_uri(host) -> str:
    """The redirect_uri to hand Discord for a request that arrived on `host`.

    BOTH the authorize step and the token exchange call this with the host their
    own request carried, so the two can never disagree: Discord rejects an
    exchange whose redirect_uri is not the one the code was minted for, and a
    hardcoded value would break the moment a player started the flow on one host
    and came back on the other."""
    name = _normalise(host)
    if is_root_host(name) and _DNS_NAME_RE.match(name) and not _IPV4_RE.match(name):
        return "https://%s/portal/oauth/callback" % name
    return DISCORD_OAUTH_REDIRECT_URI


def safe_return_to(value, host) -> str:
    """A relative path this host may bounce a player back to, or the default.

    Anything that could leave the site is refused rather than sanitised: a
    protocol-relative `//evil.test` is a redirect off-site that starts with a
    slash, a backslash is one that several browsers normalise into one, and
    whitespace or a control character is how a header is smuggled apart."""
    default = "/" if is_root_host(host) else "/portal/account"
    if not isinstance(value, str) or not value or len(value) > RETURN_TO_MAX:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    if "://" in value or "\\" in value:
        return default
    for ch in value:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return default
    return value


def _safe_query(query: bytes) -> str:
    """The query string as it can go into a Location header. See _QUERY_RAW_MIN."""
    out = []
    for byte in query:
        if _QUERY_RAW_MIN <= byte <= _QUERY_RAW_MAX:
            out.append(chr(byte))
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _host_header(scope) -> str:
    for name, value in scope.get("headers") or ():
        if name == b"host":
            return value.decode("latin-1")
    return ""


class PortalHostMiddleware:
    """Raw ASGI. On a root host it answers the redirects itself and rewrites the
    path for everything else; every other host passes through untouched.

    Registered LAST in main.py so it is the outermost user middleware. It has to
    run before `security_headers`, which reads `request.url.path` to choose the
    CSP: a page served at `/` on the portal host is the SPA and needs
    `_NEXTGEN_CSP`, and it only looks like the SPA once the path says
    `/portal/v2/`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not is_root_host(_host_header(scope)):
            await self.app(scope, receive, send)
            return

        verb, target = decide(scope.get("path", "/"))
        if verb == "redirect":
            query = scope.get("query_string") or b""
            location = quote(target, safe=_LOCATION_SAFE)
            if query:
                location += "?" + _safe_query(query)
            await _redirect(send, location)
            return
        if verb == "rewrite":
            # A copy: the caller's scope belongs to the server, and a rewritten
            # path must not leak back into keep-alive bookkeeping.
            scope = dict(scope)
            scope["path"] = target
            raw = scope.get("raw_path")
            # raw_path is the percent-encoded original. Prefixing it keeps that
            # encoding intact rather than re-encoding a decoded path.
            scope["raw_path"] = (V2_PREFIX.encode("ascii") + raw) if raw else target.encode("utf-8")
        await self.app(scope, receive, send)


async def _redirect(send, location: str):
    """301 with an empty body. no-store because these are the old addresses:
    a cached permanent redirect on a path we may want back is a long problem."""
    await send({
        "type": "http.response.start",
        "status": 301,
        "headers": [
            (b"location", location.encode("utf-8")),
            (b"cache-control", b"no-store"),
            (b"content-length", b"0"),
        ],
    })
    await send({"type": "http.response.body", "body": b""})
