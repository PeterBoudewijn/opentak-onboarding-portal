from datetime import datetime, timezone

import app.api_v1.game as game
from app.api_v1.game import GAME_TEAMS, GAME_TZ, _next_cutoff


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



def test_game_missions_match_reserved_team_groups():
    assert GAME_TEAMS["red"]["mission"] == "GameRed"
    assert GAME_TEAMS["blue"]["mission"] == "GameBlue"
    assert GAME_TEAMS["red"]["mission"] == GAME_TEAMS["red"]["group"]
    assert GAME_TEAMS["blue"]["mission"] == GAME_TEAMS["blue"]["group"]


def test_reset_team_mission_deletes_old_before_creating_new(monkeypatch):
    calls = []

    monkeypatch.setattr(
        game,
        "_team_mission",
        lambda team_key: {"name": GAME_TEAMS[team_key]["mission"], "guid": "old-guid"},
    )
    monkeypatch.setattr(
        game.otsClient,
        "delete_mission",
        lambda name: calls.append(("delete", name)),
    )
    monkeypatch.setattr(
        game,
        "_create_team_mission",
        lambda team_key: calls.append(("create", team_key)) or {"guid": "new-guid"},
    )

    result = game._reset_team_mission("red")

    assert calls == [("delete", "GameRed"), ("create", "red")]
    assert result["oldGuid"] == "old-guid"
    assert result["newGuid"] == "new-guid"
    assert result["deleted"] is True
    assert result["created"] is True



def test_co_display_usernames_are_stable():
    assert GAME_TEAMS["red"]["co_display_username"] == "CORood"
    assert GAME_TEAMS["blue"]["co_display_username"] == "COBlauw"


def test_sync_co_hq_profiles_sets_team_role_and_callsign(monkeypatch):
    calls = []

    monkeypatch.setattr(
        game.otsClient,
        "create_device_profile",
        lambda **kwargs: calls.append(kwargs) or {"status_code": 200},
    )

    result = game._sync_co_hq_profiles(
        "blue",
        username="COBLauw",
        euds=[{"uid": "device-1"}],
    )

    assert result == {"synced": 1, "errors": []}
    assert [
        (call["preference_key"], call["preference_value"])
        for call in calls
    ] == [
        ("locationTeam", "Blue"),
        ("atakRoleType", "HQ"),
        ("locationCallsign", "COBlauw"),
    ]
    assert all(call["eud_uid"] == "device-1" for call in calls)
    assert all(call["connection"] is True for call in calls)



def test_co_atak_url_is_rewritten_to_public_host(app):
    with app.test_request_context("/game/red"):
        app.config["GAME_PUBLIC_TAK_HOST"] = "atak.3division.eu"
        value = game._normalize_atak_enrollment_url(
            "tak://com.atakmap.app/enroll?host=127.0.0.1&username=CORood&token=abc.def",
            username="CORood",
        )

    assert "host=atak.3division.eu" in value
    assert "username=CORood" in value
    assert "token=abc.def" in value
    assert "127.0.0.1" not in value


def test_itak_quick_connect_uses_public_host_and_stream_port(app):
    with app.test_request_context("/game/red"):
        app.config["GAME_PUBLIC_TAK_HOST"] = "atak.3division.eu"
        app.config["GAME_TAK_STREAM_PORT"] = 8089
        value = game._itak_quick_connect()

    assert value == "OpenTAKServer_atak.3division.eu,atak.3division.eu,8089,ssl"



def test_blue_co_uses_historical_ots_identity_when_exact_display_name_is_absent(app, monkeypatch):
    monkeypatch.setattr(game, "_find_ots_username_exact", lambda username: None)
    monkeypatch.setattr(
        game,
        "_find_ots_username_casefold",
        lambda username: "COBLauw" if str(username).casefold() == "coblauw" else None,
    )
    monkeypatch.setattr(game, "_ensure_team_isolation", lambda team, username: None)
    monkeypatch.setattr(game.otsClient, "activate_user", lambda username: {"status_code": 200})

    with app.app_context():
        assert game._ensure_co_ots_user("blue") == "COBLauw"



def test_revoke_co_tak_access_deletes_token_and_deactivates_account(app, monkeypatch):
    calls = []

    monkeypatch.setattr(game, "_co_ots_username", lambda team_key: "CORood")
    monkeypatch.setattr(
        game,
        "_get_user_euds",
        lambda username: [{"uid": "device-1"}, {"uid": "device-2"}],
    )
    monkeypatch.setattr(
        game.otsClient,
        "delete_atak_qr_string",
        lambda username: calls.append(("delete_token", username)),
    )
    monkeypatch.setattr(
        game.otsClient,
        "deactivate_user",
        lambda username: calls.append(("deactivate", username)),
    )

    with app.app_context():
        result = game._revoke_co_tak_access("red", "23:00_cutoff")

    assert calls == [
        ("delete_token", "CORood"),
        ("deactivate", "CORood"),
    ]
    assert result["username"] == "CORood"
    assert result["eudCount"] == 2
    assert result["enrollmentTokenRevoked"] is True
    assert result["accountDeactivated"] is True
    assert result["reason"] == "23:00_cutoff"



def test_co_devices_admin_payload_marks_connected_and_revoked(app, monkeypatch):
    monkeypatch.setattr(
        game,
        "_co_ots_username",
        lambda team_key: "CORood" if team_key == "red" else "COBLauw",
    )
    monkeypatch.setattr(
        game,
        "_get_user_euds",
        lambda username: (
            [{
                "uid": "device-red",
                "callsign": "CORood",
                "device": "Tablet",
                "platform": "ATAK",
                "version": "5.0",
                "last_status": "Connected",
                "last_event_time": "2026-10-13T18:00:00+00:00",
            }]
            if username == "CORood"
            else [{
                "uid": "device-blue",
                "callsign": "COBlauw",
                "device": "iPad",
                "platform": "iTAK",
                "version": "2.0",
                "last_status": "Disconnected",
                "last_event_time": "2026-10-13T17:00:00+00:00",
            }]
        ),
    )
    monkeypatch.setattr(
        game,
        "_co_device_profile_map",
        lambda: {
            "device-red": {
                "airsoftCoCertificateSerial": "12345",
                "airsoftCoValidUntil": "4102444800",
            },
            "device-blue": {
                "airsoftCoCertificateSerial": "67890",
                "airsoftCoValidUntil": "0",
            },
        },
    )

    with app.app_context():
        payload = game._co_devices_admin_payload()

    assert payload["teams"]["red"]["connectedCount"] == 1
    assert payload["teams"]["red"]["deviceCount"] == 1
    assert payload["teams"]["red"]["devices"][0]["connected"] is True
    assert payload["teams"]["red"]["devices"][0]["certificateWindowBound"] is True
    assert payload["teams"]["blue"]["devices"][0]["revoked"] is True
    assert payload["teams"]["blue"]["devices"][0]["validUntil"] is None


def test_co_device_profile_map_reads_internal_window_profiles(app, monkeypatch):
    def fake_profiles(**kwargs):
        key = kwargs["preference_key"]
        if key == "airsoftCoCertificateSerial":
            return {
                "response": {
                    "results": [{
                        "eud_uid": "device-1",
                        "preference_key": key,
                        "preference_value": "123",
                    }]
                }
            }
        return {
            "response": {
                "results": [{
                    "eud_uid": "device-1",
                    "preference_key": key,
                    "preference_value": "4102444800",
                }]
            }
        }

    monkeypatch.setattr(game.otsClient, "get_device_profiles", fake_profiles)

    with app.app_context():
        values = game._co_device_profile_map()

    assert values["device-1"]["airsoftCoCertificateSerial"] == "123"
    assert values["device-1"]["airsoftCoValidUntil"] == "4102444800"
