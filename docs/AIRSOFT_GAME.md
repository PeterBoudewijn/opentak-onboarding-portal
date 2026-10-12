# Airsoft Game Mode

This fork adds an Airsoft Game page on top of the existing OpenTAK Onboarding Portal.

## Logical teams and per-session OTS accounts

The fixed game identities are:

- `GameRed` (logical label and OTS group)
- `GameBlue` (logical label and OTS group)

For certificate safety the portal does **not** reuse one permanent OTS login for each team. Each time **Nieuwe toegang genereren** is used, the portal creates a unique temporary OTS username such as:

- `GameRed_a3f91c2d`
- `GameBlue_54b8d910`

That temporary account is what ATAK/iTAK use for the game session.

This is intentional. OpenTAKServer 1.7.13 authenticates an SSL client certificate by certificate common name and does not enforce its generated CRL in the SSL EUD handler. Reusing the same username after reactivation could therefore make an old certificate useful again. A unique account per game session avoids that: once a session account is deactivated it is never reactivated.

## Isolation

Before access is returned, the temporary account is placed exclusively in its team group:

- red session account -> `GameRed` group, IN + OUT only
- blue session account -> `GameBlue` group, IN + OUT only

All other OTS group memberships for the session account are removed. This is the primary security boundary between teams.

## Portal roles

Two local, non-admin portal roles are seeded:

- `game_red_leader`
- `game_blue_leader`

A red game leader only sees/generates red access. A blue game leader only sees/generates blue access. The existing `administrator` role can see and clean up both teams.

## Generating game access

**Nieuwe toegang genereren**:

1. permanently closes the previous active session account for that team, if any;
2. creates a new unique OTS user;
3. gives it only the correct team group membership (IN + OUT);
4. generates a random password;
5. creates a new ATAK enrollment token;
6. returns the iTAK connection QR string;
7. returns the temporary username/password to the browser;
8. records the session account locally for later cutoff/cleanup.

The ATAK token expires at the earliest of:

- the next 23:00 in `Europe/Amsterdam`; or
- exactly 24 real hours after generation.

The 24-hour cap is calculated on the UTC timeline so DST fallback can never create a 25-hour credential.

## Team colors

For discovered EUDs belonging to a red or blue session account, the portal writes OTS Device Profiles:

- Red: `locationTeam = Red`
- Blue: `locationTeam = Blue`
- both: `atakRoleType = Team Member`

The game page refreshes status every 30 seconds so new EUDs can receive the appropriate per-EUD connection profile.

Group membership, not color, is the team security boundary.

## 23:00 cutoff

At 23:00 `Europe/Amsterdam`, the current session account for each team is:

- stripped of usable ATAK enrollment access;
- assigned a new random password;
- deactivated;
- marked closed in the portal state.

That account is never reactivated. The next game receives a newly generated username.

A client that was already connected at 23:00 may keep its existing socket until it disconnects or the OTS EUD service closes it. A deactivated session account cannot establish a new authenticated connection. Hard live-socket termination requires an OTS-side helper.

## Manual administrator cleanup

Administrators get:

- **Wis Team Rood**
- **Wis Team Blauw**
- **Wis alle game-toegang**

Cleanup permanently deactivates every tracked, still-open session account for the selected team(s).

## Historic EUD deletion

OpenTAKServer exposes `GET /api/eud`, but no supported `DELETE /api/eud` endpoint. This fork therefore does not directly modify the OTS database.

Historic EUD records can remain until an admin cleanup/helper removes them. Security does not depend on deleting them because their session OTS usernames are permanently deactivated and never reused.

## Recommended first deployment

1. Install this feature branch on a separate local portal port (127.0.0.1:5000).
2. Connect it to OTS with an OTS administrator API account.
3. Assign the local portal roles `game_red_leader` and `game_blue_leader` to the intended game leader portal users.
4. Generate one red and one blue test session.
5. Verify OTS groups contain only the expected IN/OUT membership.
6. Enroll two test devices and verify cross-team isolation.
7. Verify the per-EUD team colors.
8. Test deactivation/reconnect behaviour.
9. Only then add the optional OTS-side hard EUD/socket cleanup helper.
