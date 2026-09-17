"""Request-body reader shared by the portal write endpoints.

Kept fastapi-free (duck-typed request) so it is unit-testable without the web
stack. Accepts EITHER application/json OR form-encoded input: the V2 client posts
JSON via sendCsrfJSON, while some callers post form-encoded. Returns a dict for
JSON, else the request's FormData (preserves multi-values / file uploads). Both
support .get(). CSRF still rides the X-Portal-CSRF-Token header (or a csrf_token
field), so it validates for both. This is the single choke point that prevents
the JSON-sent-but-form-read mismatch (which made writes look like empty bodies,
e.g. "Message body is required").
"""


async def read_body(request):
    ctype = request.headers.get("content-type", "") or ""
    if ctype.startswith("application/json"):
        try:
            j = await request.json()
            return j if isinstance(j, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    try:
        return await request.form()
    except Exception:  # noqa: BLE001
        return {}
