# Plan: Spotify browser in the web app

**Status:** proposal, nothing built yet · **Branch:** `feature/spotify-browser` · **Supersedes the approach in** #72 · builds on #70/#71 for the second Duo account

**Decided (10 Oct):** Spotify is a **5th tab**. It must work with **both Duo accounts**. A personal Spotify developer app is needed; the reasons are in *Setup* below.

## Recommendation

Build it. It's worth doing, and it's a lot smaller than #72 suggested, because of something we've found since #72 was written:

> **The speakers can already play any Spotify playlist on their own.** In October we stored a playlist as a preset on the Dining Room ST20. From standby, it played on the speaker's own linked Spotify account (`turnerben37`) through a single local `/select` call. No phone and no Spotify Web API were involved.

So playback is solved. What's left is the **browsing**: search, your playlists and your library. That needs the Spotify Web API, but only read-only access.

#72 planned to play via the Web API's transfer-playback, plus a ZeroConf credential push for a second account. Neither is needed for the main use case.

## How it works

```
 Phone (web app)                 Controller (Python)                   Cloud / LAN
 ───────────────                 ───────────────────                   ───────────
 Spotify tab ── search / list ─▶ /api/spotify/* proxy ── read-only ──▶ Spotify Web API
                                  (holds the token,                     (api.spotify.com)
                                   browser never sees it)
 tap ▶ Play ─────────────────▶  /api/spotify/play ─── /select ──────▶ ST speaker :8090
                                  builds the ContentItem:               plays it on its own
                                  SPOTIFY / tracklisturl /              linked account
                                  /playback/container/<b64 uri>
```

- **Browse:** the Web API, authorised once with OAuth PKCE using read-only scopes: `playlist-read-private`, `user-library-read`, `user-read-recently-played`. Since it's PKCE, there's no client secret to store.
- **Play:** the same local ContentItem format as presets. For example, `spotify:playlist:0NF0Mryk…` is base64-encoded into `/playback/container/…`, with `sourceAccount="turnerben37"`.
- **Speakers:** only speakers with that account linked and `READY` get Spotify as an option. That's every speaker except the Living Room soundbar, which has no account linked. The same rule (`preset_target_ok()`) already drives "Save what's playing → All speakers".

## Two Duo accounts

Browsing is easy: each account links separately, and a switcher at the top of the tab picks whose library you see (screen 1).

**Playing is the hard part.** A speaker plays Spotify as **whichever account is logged into it**, and all six are logged in as `turnerben37`. So for the second account:

| Second account plays… | Through Ben's account on the speaker | After switching the speaker to the second account |
|---|---|---|
| Public playlists, albums, artists, songs | ✅ works | ✅ works |
| Its own **private playlists** and **Liked Songs** | ❌ Ben's account can't see them | ✅ works |
| Its own listening history / recommendations | counts as Ben's | ✅ its own |

To do this properly, the controller has to **switch the speaker's logged-in account** before playing, and switch back when Ben plays. The speakers support this in principle via the Spotify Connect ZeroConf endpoint on port 8200. That's the hand-off the phone app does when you pick a speaker. It takes a short-lived access token (`tokenType: accesstoken`) via `addUser`, which needs the extra `streaming` scope on that account's login. #71 got as far as reading `getInfo` but never tested `addUser` on these speakers.

**So spike 5 (below) decides the design:**
- **If it works:** playing as either account switches the speaker as needed. The speaker sheet shows "Playing as …", and the Player tab shows whose account is on each speaker.
- **If it doesn't:** the second account still gets full browsing and plays anything public. Its private playlists show a "Make this playlist public to play it on the speakers" hint. Still useful, just not complete.

## Spotify's rules that shape the design

| Rule (checked October 2026) | What it means here |
|---|---|
| Redirect URIs must be HTTPS, except the loopback IPs `http://127.0.0.1` / `[::1]`. `localhost` and LAN IPs are not allowed (enforced for new apps from April 2025). | The controller's `http://10.10.10.111:8888` can't be the redirect. So the one-off login is a **paste-back**: Spotify redirects the phone to `http://127.0.0.1:8888/…`, the page fails to load (expected), and you paste that address into the app. See screen 5. |
| Since November 2024, new apps can't read **Spotify-owned / algorithmic playlists** (Discover Weekly, Daily Mix, editorial), recommendations or featured/category playlists. | These can't be browsed or searched into, but they can still be **played**. The speaker doesn't use the Web API, as the Discover Weekly preset already shows. They also still work through "Save what's playing". |
| Development-mode apps are limited to a small allow-list of named users. | Fine for one household. Both Duo accounts' emails go on the allow-list (see *Setup*). |
| Brand guidelines. | Show "Content from Spotify", don't crop or alter artwork, and don't use the Spotify logo as our icon. The mockups use a generic music-note tab icon. |

## Screens

All mockups are at phone width, in the app's real styling (`docs/plans/spotify-browser/mockup.html`). Covers are placeholders: the real build shows Spotify's artwork.

| 1 · Spotify tab (home) | 2 · Search | 3 · Playlist |
|---|---|---|
| ![Home](spotify-browser/1-home.png) | ![Search](spotify-browser/2-search.png) | ![Playlist](spotify-browser/3-playlist.png) |
| **Jump back in** (recently played contexts) and **Your library** (Liked Songs, your playlists, saved albums). The speaker/source pickers stay at the top, so the active speaker is the default target. | One box searches playlists, albums, artists and songs, with filter chips. ▶ plays straight away on the active speaker. Artists open a page with top albums. | Cover, details and tracks. The primary **Play on Dining Room ▾** button goes to the active speaker, and ▾ opens the speaker sheet. **☆ Preset** saves it to a slot, reusing the existing save-preset code. |

| 4 · Speaker sheet | 5 · Link account (Settings) |
|---|---|
| ![Speakers](spotify-browser/4-speaker-sheet.png) | ![Link](spotify-browser/5-link-account.png) |
| Pick one speaker, or several to play in sync (creates a group via the existing zone API, then plays on the master). Speakers that can't play it are greyed out with the reason. | One-off setup with the paste-back flow, explained in plain terms, plus an up-front note that anyone on the LAN can browse the account read-only. |

## Setup: why a Spotify developer app is needed

Spotify's Web API only answers programs that identify themselves with an app **Client ID**, even for a plain search, logged in or not. There's no anonymous access, and the speakers' own Spotify integration can't be borrowed for browsing.

- **Who:** it has to be Ben. An app is created under a Spotify login, and a development-mode app only works for accounts its owner adds to its allow-list, which here means the second Duo account.
- **Cost:** free, no review, about 5 minutes.
- **Steps:**
  1. Log in at developer.spotify.com and create an app (Web API).
  2. Add the redirect URI `http://127.0.0.1:8888/api/spotify/callback`.
  3. Under *User Management*, add the second Duo account's email.
  4. Copy the **Client ID** into the controller (Settings → Spotify). With PKCE there's no client secret.
- **Alternatives considered:** using another app's Client ID breaks Spotify's terms and can be blocked at any time. Not building the browser leaves the Spotify phone app plus "Save what's playing", which already covers most of the need.

## Backend

New `SpotifyClient` class and `/api/spotify/*` endpoints (GET, matching the existing API style):

| Endpoint | Does |
|---|---|
| `/api/spotify/accounts` | linked accounts (name, user id, Premium?), never tokens |
| `/api/spotify/login?account=new` | returns the authorise URL (PKCE `code_challenge`, `state`) |
| `/api/spotify/link?url=` | takes the pasted callback URL, checks `state`, exchanges the code, resolves the user via `/v1/me`, stores the tokens |
| `/api/spotify/unlink?account=` | forgets that account |
| `/api/spotify/home?account=` | recently played contexts, playlists, saved albums, Liked Songs count (cached ~60 s) |
| `/api/spotify/search?q=&type=&account=` | `/v1/search` |
| `/api/spotify/item?uri=&account=` | playlist / album / artist detail and tracks |
| `/api/spotify/play?uri=&hosts=&account=` | switches each speaker to `account` if needed (spike 5), builds the ContentItem, `/select`s it. With several hosts it makes a zone first |

- **Tokens:** one file per account, `data/spotify/<user_id>.json`, with mode `0600`. That's #70's `SpotifyAccountStore`, minus the client secret thanks to PKCE. Refreshed automatically about 60 s before expiry, never logged or sent to the browser. `data/` is already gitignored.
- **Scopes:** read-only browsing (`playlist-read-private`, `user-library-read`, `user-read-recently-played`, `user-read-private`), plus `streaming` if spike 5 passes, to hand the speaker a login.
- **Client ID:** from `$SPOTIFY_CLIENT_ID` or `data/spotify/app.json`. It isn't secret with PKCE, but it's kept out of the repo anyway.
- **Rate limits:** honour `429 Retry-After`, and cache responses briefly so scrolling doesn't hammer the API.
- **Front end:** a separate `web/spotify.js` module rather than growing `app.js`, which is already ~1,800 lines.

## Phases

**Phase 0: spikes (do first; each one can change the plan)**

1. **Other content types:** does `/select` play **albums**, **artists**, **Liked Songs** (`spotify:user:<id>:collection`) and **single tracks** the same way playlists do? Playlists are proven. If tracks don't work, tapping a song plays its album instead, or see 2.
2. **Starting at a track:** can playback start at a chosen track? Probably not via `/select`. The fallback is Web API `PUT /me/player/play` with `offset`. The speaker is already in `turnerben37`'s Connect device list, but this needs the extra `user-modify-playback-state` scope.
3. **Login flow:** does the paste-back flow work on iPhone, and how long do refresh tokens last for a dev-mode app?
4. **Spotify-made playlists:** does `/v1/me/playlists` still *list* Spotify-made playlists you follow, with name, art and URI, even though their tracks are blocked? If so, Discover Weekly can still appear as a tile.
5. **Account switching (decides the two-account design):** can the controller log a speaker into the second Duo account via ZeroConf `addUser` (port 8200, access token with `streaming`)? Then: does `/sources` show that account `READY`, does `/select` with its `sourceAccount` play one of its private playlists, and can we switch back to `turnerben37` cleanly? Test on one ST10 first. If it fails, the fallback is described under *Two Duo accounts*.

**Phase 1: backend.** `SpotifyClient`, the token store, the endpoints above, and unit tests with mocked Web API responses. Done when `/api/spotify/home` returns real data and `/api/spotify/play` starts a playlist on an ST20.

**Phase 2: UI.** Spotify tab (home, search, detail), the speaker sheet, and the Settings link card. Album art feeds the existing full-bleed background. Done when you can browse and play from the phone.

**Phase 3: extras that come almost free**
- ☆ Preset from any playlist or album, without having to play it first. Generalise `/api/presets/save-current` to accept a URI.
- **Alarms that play a Spotify playlist directly** rather than a preset slot. Alarms keep working when presets change.
- "Play on all speakers" via the existing group/party API.

**Rough size:** Phase 0 is one short session. Phases 1 and 2 are a couple of sessions each. Phase 3 is one session.

## Risks

- **Spotify could end the speakers' built-in support** (it's a Bose integration, and Bose has exited). It works today. If it stopped, presets would break too, so the browser adds no new exposure.
- **Spotify Web API policy keeps tightening.** We use only plain read-only library and search endpoints, which have been stable.
- **The controller has no login.** Anyone on the Wi-Fi could browse the linked account, but only read-only: the scopes can't modify playlists or see billing. Spike 2 would add `user-modify-playback-state`, and that's the point to revisit this.
- **Account switching (spike 5) is unproven on Bose.** If it fails, the second account loses only its private playlists and Liked Songs. If it works but is slow, switching may add a few seconds before playback.
- **Switching fights:** if one person casts from the Spotify phone app while the controller switches that speaker to the other account, the last one wins. The Player tab should show whose account is on each speaker, so it isn't a mystery.

## Decisions

1. ~~Tab or Source picker?~~ **5th tab.**
2. ~~One account or both?~~ **Both Duo accounts.** The design depends on spike 5.
3. ~~Developer app?~~ **Needed.** See *Setup*. Ben creates it when Phase 0 starts, adding the second account's email.
