from datetime import datetime, timezone

from app.api_v1.game import GAME_TZ, _next_cutoff


def test_cutoff_before_23_is_same_day_23():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=GAME_TZ)
    cutoff = _next_cutoff(now)
    assert cutoff.year == 2026
    assert cutoff.month == 10
    assert cutoff.day == 11
    assert cutoff.hour == 23
    assert cutoff.minute == 0


def test_cutoff_after_23_is_next_day_23():
    now = datetime(2026, 10, 11, 23, 5, tzinfo=GAME_TZ)
    cutoff = _next_cutoff(now)
    assert cutoff.year == 2026
    assert cutoff.month == 10
    assert cutoff.day == 12
    assert cutoff.hour == 23
    assert cutoff.minute == 0


def test_cutoff_never_exceeds_24_real_hours_at_dst_fallback():
    # Europe/Amsterdam changes from UTC+2 to UTC+1 on 2026-10-25.
    now = datetime(2026, 10, 25, 0, 30, tzinfo=GAME_TZ)
    cutoff = _next_cutoff(now)
    elapsed = cutoff.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    assert elapsed.total_seconds() <= 24 * 60 * 60


def test_cutoff_uses_23_when_that_is_earlier_than_24_hours():
    now = datetime(2026, 10, 25, 10, 0, tzinfo=GAME_TZ)
    cutoff = _next_cutoff(now)
    assert cutoff.hour == 23
    assert cutoff.day == 25
