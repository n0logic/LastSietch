from datetime import datetime, timezone
from urllib.parse import quote


DESTINATIONS = {"reward": "/rewards", "karum": "/karum", "gift": "/settings",
                "transfer": "/settings", "rescue": "/maps", "mail": "/mailbox"}


def utc_stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def compose_activity(ledger, trades, deliveries, alerts, limit=100):
    items = []
    unavailable = []

    def add(kind, at, summary, href, detail=None):
        stamp = utc_stamp(at)
        if stamp is None:
            if kind not in unavailable:
                unavailable.append(kind)
            return
        row = {"kind": kind, "t": stamp, "summary": summary, "href": href}
        if detail:
            row["detail"] = detail
        items.append(row)

    if not isinstance(ledger, dict):
        unavailable.append("Account activity")
    else:
        if ledger.get("unavailable"):
            unavailable.append("Some account activity")
        for row in ledger.get("items", []):
            kind = row.get("kind")
            if kind in DESTINATIONS:
                add(kind, row.get("t"), row.get("summary", "Account activity"), DESTINATIONS[kind])

    if not isinstance(trades, dict) or trades.get("available") is not True:
        unavailable.append("Exchange sales")
    else:
        for row in trades.get("history", []):
            if row.get("completion_type") != 4:
                continue
            template = quote(str(row.get("template_id") or ""), safe="")
            add("sale", row.get("logged_at"), f"Sold {row.get('name') or 'an item'}",
                f"/exchange?tpl={template}",
                f"{row.get('qty', '')} sold · {row.get('total_display', '')} Solari")

    if not isinstance(deliveries, dict) or deliveries.get("available") is not True:
        unavailable.append("Deliveries")
    else:
        for package in deliveries.get("packages", []):
            label = package.get("label") or "Package"
            add("delivery", package.get("granted_at"), f"{label} sent", "/mailbox#deliveries",
                "Open delivery details to see what has landed and what is still waiting.")
            for leg in package.get("legs", []):
                if leg.get("state") == "delivered" and leg.get("at"):
                    add("delivery", leg["at"], f"{label}: {leg.get('label') or 'delivery'} landed",
                        "/mailbox#deliveries", leg.get("note"))

    if not isinstance(alerts, list):
        unavailable.append("Price alerts")
    else:
        for row in alerts:
            template = quote(str(row.get("template_id") or ""), safe="")
            add("alert", row.get("created_at"), f"Price alert: {row.get('name') or 'item'}",
                f"/exchange?tpl={template}",
                f"Matched {row.get('match_display', '')} Solari · target {row.get('threshold_display', '')} Solari")

    items.sort(key=lambda row: row["t"], reverse=True)
    return {"items": items[:limit], "unavailable": unavailable,
            "has_more": len(items) >= limit}
