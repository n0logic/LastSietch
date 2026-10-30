from datetime import UTC, datetime
from types import SimpleNamespace

from cielago.cogs.events_sync import edit_kwargs, parse_time, status_steps


def test_steps_walk_discords_order():
    assert status_steps("scheduled", "active") == ["start"]
    assert status_steps("scheduled", "completed") == ["start", "end"]
    assert status_steps("active", "completed") == ["end"]
    assert status_steps("scheduled", "canceled") == ["cancel"]


def test_an_active_event_is_ended_not_cancelled():
    assert status_steps("active", "canceled") == ["end"]


def test_final_and_unchanged_need_nothing():
    for desired in ("scheduled", "active", "completed", "canceled"):
        assert status_steps("completed", desired) == []
        assert status_steps("canceled", desired) == []
    assert status_steps("active", "active") == []
    assert status_steps("active", "scheduled") == []


def test_parse_time_is_aware():
    assert parse_time("2026-11-06T04:00:00+00:00") == datetime(2026, 11, 6, 4, tzinfo=UTC)
    assert parse_time("2026-11-06T04:00:00").tzinfo == UTC


ITEM = {"name": "XP weekend", "description": "Bonus\n\nhttps://portal.lastsietch.com/events/7",
        "location": "The Last Sietch", "starts_utc": "2026-11-06T04:00:00+00:00",
        "ends_utc": "2026-11-09T05:00:00+00:00"}


def _event(**kw):
    base = dict(name=ITEM["name"], description=ITEM["description"], location=ITEM["location"],
                start_time=parse_time(ITEM["starts_utc"]), end_time=parse_time(ITEM["ends_utc"]))
    base.update(kw)
    return SimpleNamespace(**base)


def test_only_changed_fields_are_edited():
    assert edit_kwargs(ITEM, _event(), "scheduled") == {}
    assert edit_kwargs(ITEM, _event(name="Old"), "scheduled") == {"name": "XP weekend"}


def test_start_time_only_moves_while_scheduled():
    moved = _event(start_time=datetime(2026, 11, 6, 5, tzinfo=UTC))
    assert "start_time" in edit_kwargs(ITEM, moved, "scheduled")
    assert "start_time" not in edit_kwargs(ITEM, moved, "active")
