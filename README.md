# NVR Browser

A purpose-built Home Assistant sidebar panel for browsing the home-grown NVR
motion clips in `/config/nvr` — a flat, newest-first thumbnail gallery with
camera and object filters, replacing the painful Media-browser folder drill-down.

It is **read-only and additive**: it never touches the recording automations or
any file under `/config/nvr`. Thumbnails are cached to a separate dir
(`/config/nvr_thumbs`).

## What it does

- **Flat newest-first feed** of every clip across all cameras, infinite-scroll.
- **Filter dropdowns** for camera and detected object (person, cat, …), both
  derived from the existing folder layout (one entry per camera/object folder).
- **Date controls** — a Day dropdown for jumping to a single day, plus a
  range-picker calendar popup that dots the days with clips and filters to an
  inclusive date range.
- **Thumbnails** generated on demand by Home Assistant's bundled `ffmpeg`
  (seeks ~10s in, scaled to 320px), cached to disk, throttled to 3 concurrent grabs.
- **Click to play** inline in a lightbox, with a download link.

## How it reads the tree

The recording automations produce two shapes per hour folder:

```
<date>/<hour>/<camera>/HH:MM:SS.mp4           # the canonical clip
<date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  # hard link tagging that clip
```

The integration joins them on `(time, camera)`: a folder whose files match
`HH:MM:SS.mp4` is a camera; a folder whose files match `HH:MM:SS-<camera>.mp4`
is an object label. Playback always uses the canonical clip, streamed via the
authed `/api/nvr_browser/clip` endpoint (clips live outside `www/`, so they are
not exposed by HA's unauthenticated `/local/` route).

## Endpoints

- `GET /api/nvr_browser/events?offset=&limit=&camera=&object=&start=&end=` — authed
  JSON list (`start`/`end` are inclusive `YYYY-MM-DD` bounds).
- `GET /api/nvr_browser/days` — authed JSON `{days: [...]}`, the available
  `YYYY-MM-DD` folders newest-first; powers the Day dropdown and the calendar.
- `GET /api/nvr_browser/thumb?path=<rel>` — JPEG thumbnail (authed; the events
  view hands the frontend short-lived signed URLs so a plain `<img src>` still
  works only for the logged-in user; path is sanitised against traversal).
- `GET /api/nvr_browser/clip?path=<rel>` — original clip stream (authed, signed
  the same way; supports HTTP range requests for `<video>` seeking). Replaces the
  old public `/local/nvr/...` route.
- `GET /api/nvr_browser/clip_proxy?path=<rel>` — authed, a Roku-playable transcode
  of the clip (≤1080p H.264 + faststart), generated on demand and cached under
  `/config/nvr_proxies`. The web panel uses the original `clip`; the Roku app uses
  this. See [Pairing a TV](#pairing-a-tv-roku-app).
- `GET /api/nvr_browser/cameras` — authed JSON list of live-capable cameras (from
  the `live_cameras` map); powers the Roku app's live-view picker.
- `GET /api/nvr_browser/live?camera=<name>` — authed JSON `{camera, url,
  streamFormat:"hls"}`; returns Home Assistant's own same-origin HLS URL for that
  camera's live stream (no transcode). See
  [Live camera streaming](#live-camera-streaming-roku-app).
- `POST /api/nvr_browser/pair/new`, `GET /api/nvr_browser/pair/claim?secret=`,
  `POST /api/nvr_browser/pair/approve` — TV-pairing flow (see below).

## Pairing a TV (Roku app)

The companion Roku app [`nvr-roku`](https://github.com/skidank/nvr-roku) shows the
same gallery on a TV. Because a TV can't comfortably type a ~250-character
long-lived token, it authenticates with a short pairing code instead:

1. Open the NVR app on the Roku — it displays a 6-character code.
2. In the **NVR** panel, click **Pair TV** and enter that code.
3. The TV signs in automatically and stays signed in.

Approving a code mints a Home Assistant **long-lived access token** bound to your
account and hands it to that TV. The token has full HA scope (HA has no
per-integration scoping), so **only pair TVs you trust**, and only ever approve a
code you can see on your own screen. Revoke a TV anytime under HA → Profile →
Security → *Long-lived access tokens* (named `NVR Roku (…)`).

## Live camera streaming (Roku app)

The Roku app can also show a **live** camera view, not just recorded clips. This
is opt-in via a `live_cameras` map in `configuration.yaml` that points each NVR
camera name at a Home Assistant camera entity:

```yaml
nvr_browser:
  live_cameras:
    backyard: camera.backyard_sub    # MUST be a <=1080p H.264 entity
    porch:    camera.porch_sub
```

The integration returns Home Assistant's own HLS stream URL for that entity
(served by the built-in `stream:` integration). It does **not** transcode. The
URL is a **root-relative, self-tokenized path** (e.g.
`/api/hls/<token>/master_playlist.m3u8`), which the app resolves against its own
base URL — that's *why* live works remotely with no extra host config.

> **The mapped entity must be ≤1080p H.264.** Roku's decoder caps H.264 at 1080p
> — the same limit that makes recorded clips need a transcoded proxy. Home
> Assistant *remuxes* the live stream without transcoding, so pointing
> `live_cameras` at a full ~5 MP main stream produces a URL that loads but fails
> on the TV with a decoder error. Point it at a **low-resolution substream** (most
> IP cameras expose one). Requires the `stream:` integration (enabled by default)
> and a stream-capable camera entity.

Leaving `live_cameras` out (or empty) disables live entirely. This is a
Python-side feature, so **restart Home Assistant** after adding it. The web
sidebar panel is unchanged — live is a Roku-app feature.

## Requirements

- A Home Assistant install whose config directory is `/config` — HAOS,
  Supervised, or the official Container image. Paths are hardcoded to
  `/config/nvr` and `/config/nvr_thumbs`, so a Core/venv install with a
  different config path won't work as-is.
- `ffmpeg` available to Home Assistant (bundled on HAOS / Supervised / Container).
- Live view (optional) additionally needs the `stream:` integration (enabled by
  default) and a stream-capable, ≤1080p H.264 camera entity — see
  [Live camera streaming](#live-camera-streaming-roku-app).
- Your motion clips already under `/config/nvr/` in the layout above (your
  recording automations must write there, **not** under `www/`).

## Installation

### HACS

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add this repository's URL with category **Integration**, then download
   **NVR Browser**.
3. Add `nvr_browser:` to `configuration.yaml` (see [Configuration](#configuration)) —
   the integration won't load until you do.
4. Restart Home Assistant.

### Manual

Copy `custom_components/nvr_browser/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Configuration

Enable the integration with one line in `configuration.yaml`:

```yaml
nvr_browser:
```

Restart Home Assistant. An **NVR** item (cctv icon) then appears in the sidebar.

To also stream **live** cameras to the Roku app, add the optional `live_cameras`
map — see [Live camera streaming](#live-camera-streaming-roku-app).

## Deep-linking (URL parameters)

The panel reads its filters from the page URL, so you can open it pre-filtered
from anywhere — a dashboard button, a live-camera card's tap action, a Markdown
link, etc. The supported query params match the event API:

| Param    | Meaning                          | Example                |
| -------- | -------------------------------- | ---------------------- |
| `camera` | only this camera                 | `camera=front_door`    |
| `object` | only clips tagged this object    | `object=person`        |
| `start`  | inclusive lower date bound       | `start=2026-06-01`     |
| `end`    | inclusive upper date bound       | `end=2026-06-08`       |

Combine freely, e.g. `/nvr-browser?camera=front_door&object=person`. From a
Lovelace card, point a navigation at that URL:

```yaml
tap_action:
  action: navigate
  navigation_path: /nvr-browser?camera=front_door
```

The URL also stays in sync as you change filters in the panel, so the address
bar always holds a shareable, bookmarkable link to the current view.

## Removal

Remove the `nvr_browser:` line from `configuration.yaml`, uninstall via HACS (or
delete `custom_components/nvr_browser`), and restart Home Assistant. The cache
dirs `/config/nvr_thumbs` (thumbnails) and `/config/nvr_proxies` (Roku transcodes)
are safe to delete anytime.

## Thumbnail cache cleanup

Thumbnails are cached under `/config/nvr_thumbs`, keyed by a hash of the clip
path. Because clips rotate out as the recording retention deletes old days, the
integration **prunes orphaned thumbnails** (those whose source clip no longer
exists) once at startup and every `PRUNE_INTERVAL` (default 24h). It also clears
stale `.part.jpg` temp files from any crashed grab. This tracks your retention
automatically — no manual cleanup needed. The whole cache is still safe to
`rm -rf` anytime; it just regenerates on next view.

## Tunables (in `__init__.py`)

- `_THUMB_SEM = asyncio.Semaphore(3)` — concurrent ffmpeg frame-grabs.
- `scale=320:-1` in `_generate_thumb` — thumbnail width.
- seek list in `_generate_thumb` (`00:00:10`, then fallbacks) — frame offset.
- `PRUNE_INTERVAL` — how often orphaned thumbnails are swept.
- `THUMB_DIR` — cache location.
