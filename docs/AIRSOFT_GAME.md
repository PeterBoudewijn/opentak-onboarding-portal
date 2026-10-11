# Airsoft Game Mode

This fork adds an Airsoft Game page on top of the existing OpenTAK Onboarding Portal.

## Fixed game accounts

The game mode expects two existing OpenTAKServer accounts:

- `GameRed`
- `GameBlue`

The portal does not delete these accounts.

## Isolation

Before generating access, the portal enforces exclusive OTS group membership:

- `GameRed` -> `GameRed` group, IN + OUT only
- `GameBlue` -> `GameBlue` group, IN + OUT only

All other group memberships for the relevant game account are removed. This is the security boundary that keeps the red and blue data streams separated.

## Portal roles

Two local, non-admin portal roles are seeded:

- `game_red_leader`
- `game_blue_leader`

Assign these roles to the OTS-backed portal accounts used by the game leaders.

A red leader can only request/status the red game account. A blue leader can only request/status the blue game account. The existing `administrator` role can see both and can run cleanup actions.

## Generating game access

The **Nieuwe toegang genereren** action:

1. verifies the OTS group isolation;
2. activates the selected shared game account;
3. generates a new random password and sets it in OTS;
4. invalidates the previous ATAK enrollment token;
5. creates a new ATAK QR enrollment token;
6. obtains the iTAK QR connection string;
7. returns the temporary password once to the browser.

The ATAK token expires at the earliest of:

- the next 23:00 in `Europe/Amsterdam`; or
- exactly 24 real hours after generation.

The 24-hour cap is calculated on the UTC timeline so it remains correct across DST transitions.

The default maximum ATAK token usage count is 250. It can be changed with Flask config key `GAME_QR_MAX_USES`.

## Team colors

For discovered EUDs associated with the shared account, the portal writes OTS Device Profiles:

- `GameRed`: `locationTeam = Red`
- `GameBlue`: `locationTeam = Blue`
- both: `atakRoleType = Team Member`

The game page refreshes status every 30 seconds so newly discovered EUDs receive the appropriate per-EUD connection profile.

Important: an EUD has to exist in OTS before a per-EUD Device Profile can be attached. The OTS group separation is therefore the hard isolation control; color is a configuration/display setting.

## 23:00 cutoff

At 23:00 `Europe/Amsterdam`, the portal scheduler performs a credential cutoff for both teams:

- invalidate the ATAK QR token;
- rotate the shared account password;
- deactivate `GameRed` / `GameBlue`.

The accounts and groups remain present for the next game.

A client that is already connected may remain connected until its current OTS socket is closed. Deactivation prevents a new authenticated connection. Hard socket termination requires an OTS-side helper because OTS does not expose a supported public API for disconnecting an individual EUD.

## Manual administrator cleanup

Administrators get:

- **Wis Team Rood**
- **Wis Team Blauw**
- **Wis alle game-toegang**

The current implementation revokes credentials and deactivates the relevant shared account(s).

### EUD deletion limitation

Current OpenTAKServer exposes `GET /api/eud`, but no supported `DELETE /api/eud` endpoint. For that reason this fork deliberately does **not** directly edit the OTS database.

The admin page clearly reports this limitation. Physical deletion of historic EUD rows/certificates should be implemented as a small OTS-side helper after it is verified against the exact OTS version installed on the production server.

## Recommended first deployment

1. Install the upstream onboarding portal/fork normally.
2. Confirm the portal can log in to OTS with an administrator API account.
3. Create/confirm `GameRed` and `GameBlue` in OTS.
4. Log in to the portal once as the intended game leader accounts so they exist locally.
5. Assign `game_red_leader` and `game_blue_leader` using the portal Roles page.
6. Test group isolation with two test devices before production use.
7. Test the 23:00 cutoff and reconnect behaviour.
8. Only after that add the optional OTS-side hard EUD cleanup helper.
