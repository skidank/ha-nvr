# Implementation plan — live camera streaming (ha-nvr side)

Status: **implemented in 0.9.0** (server side). The Roku client work is tracked in
[nvr-roku#3](https://github.com/skidank/nvr-roku/issues/3). Kept as the design
record — rationale, the deferred Design B, and the remaining out-of-scope items.

Goal: let the companion Roku app (`nvr-roku`) play a **live** view of an HA
camera, in addition to the recorded clips it already browses. This is the
integration (server) half; the client half is tracked in nvr-roku.

> This plan was reviewed by three independent agents (HA-API correctness, the
> Roku client seam, and cross-repo contract). The load-bearing HA API was
> verified against `home-assistant/core` `dev` (see the Verification box).
> Findings are folded in below.

## Decision: return HA's own camera HLS URL (thin, proxy-free design)

Roku has no WebRTC; the only viable live transport is **HLS** (`.m3u8`). HA's
`stream:` integration (already enabled in this install) produces a tokenized,
same-origin HLS URL for any camera entity. The integration runs *inside* HA, so
it can ask the camera component for that URL directly and hand it to Roku.

So the design is: **two new authed JSON endpoints that return HA's native HLS
URL.** The integration does *not* proxy the playlist or segments, does *not*
transcode, and does *not* touch go2rtc. HA owns the rolling-window HLS muxing
and the idle teardown.

Why this over proxying go2rtc or transcoding ourselves:

- **Least code, no new lifecycle.** No segment proxying, no live ffmpeg process
  to supervise. HA's `stream` component already does it and is battle-tested.
- **Remote-capable for free.** The HLS URL is same-origin (HA's `:8123` /
  external URL), so it reaches the TV remotely through the existing reverse
  proxy / Nabu Casa — exactly how `/events` already reaches it. go2rtc's `:1984`
  endpoint is LAN-only and unauthed, so handing that to Roku would break remote
  viewing and the auth model.
- **Auth model already understood by the client.** The HLS URL carries its own
  stream token in the path (HA's stream views are `requires_auth = False`,
  guarded by that token). Same shape as the signed clip URLs Roku already plays
  without a bearer header — see "Auth".
- **Stays read-only / additive.** No new write paths; nothing under
  `/config/nvr` is touched; the existing contract is unchanged.

A heavier **Design B** (reverse-proxy go2rtc HLS behind a short-lived
live-session token, optionally pointing at an ffmpeg-transcoded variant) is the
documented fallback if we ever need ha-nvr to control resolution/transcode
*independently* of HA's camera config. **Out of scope** for 0.9.0.

## The one real constraint: source resolution (an HA-config decision, not code)

HA's `stream` component **remuxes (copy codec), it does not transcode.** So the
live HLS carries whatever resolution the chosen camera entity's stream source
is. The cameras here are ~5 MP H.264 — which Roku's decoder **cannot** play
(the same `-5 malformed data` failure that drove the clip-proxy work). For live
to play on Roku, the camera entity we stream **must already be ≤1080p H.264.**

The cameras already expose a low-res `_ext` substream (the one Frigate uses for
detection). The fix is a configuration choice on the HA side, **made by the
operator, not by this integration** (the repo stays look-don't-touch w.r.t. the
live HA config):

- point the live camera entity at the `_ext` substream (e.g. a `generic`/go2rtc
  camera entity bound to `…/channel0_ext.bcs`, or Frigate's `live -> stream`
  set to the sub stream), **or**
- accept a continuous transcode (out of scope — Design B / future).

The `live_cameras` config map (below) exists precisely so the operator points
each NVR camera name at whichever entity is Roku-playable, without code changes.

> [!WARNING]
> **This constraint is enforced only by configuration + documentation.** If the
> operator maps a 5 MP entity, `/cameras` reports it `available: true`, `/live`
> returns a valid URL, and Roku fails with an opaque `-5` decoder error and *no*
> server-side signal. The negative case ("a 5 MP entity is what fails on Roku")
> is therefore a **required** acceptance test, not an optional one. Surfacing the
> source resolution in `/cameras` so the client could warn is a noted **future**
> enhancement, not part of 0.9.0 (HA doesn't cheaply expose it pre-stream).

## New config (optional, backwards-compatible)

`CONFIG_SCHEMA` currently accepts an empty `nvr_browser:` block. Extend it with
an optional `live_cameras` map: **NVR camera folder name → HA camera entity_id.**

```python
import homeassistant.helpers.config_validation as cv

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema(
        {vol.Optional("live_cameras"): {cv.string: cv.entity_id}},
        extra=vol.ALLOW_EXTRA,
    )},
    extra=vol.ALLOW_EXTRA,
)
```

```yaml
nvr_browser:
  live_cameras:
    backyard: camera.backyard_sub      # entity must be <=1080p H.264 (Roku)
    porch:    camera.porch_sub
```

- Use `cv.entity_id` for values so HA rejects malformed entity ids at config
  load (don't hand-roll a `camera.*` check in `async_setup`).
- Read the map in `async_setup` and stash it in
  `hass.data[DOMAIN]["live_cameras"]` (next to `"pairings"`).
- **Default (absent/empty): live is disabled** — `/cameras` returns `[]`, `/live`
  returns `404`. Conservative on purpose: no live until the operator opts in by
  listing Roku-playable entities, so we never advertise a 5 MP entity that just
  errors.

## New endpoint: `GET /api/nvr_browser/cameras` (authed)

Authoritative list of live-capable cameras (independent of whether a camera has
recent clips). Mirrors `NvrDaysView` in style.

```json
{
  "cameras": [
    {"name": "backyard", "entity_id": "camera.backyard_sub", "title": "Backyard", "available": true}
  ]
}
```

- `name` — NVR camera folder name (the `live_cameras` key); lets the client line
  live up with its existing camera facets.
- `entity_id` — the entity that will be streamed.
- `title` — `state.attributes["friendly_name"]` or `name` titlecased.
- `available` — **a liveness hint, not a guarantee.** True when the entity
  exists, is not `unavailable`, **and** advertises
  `CameraEntityFeature.STREAM` (`supported_features`). It does *not* prove the
  underlying RTSP source is healthy, so `/live` can still fail on an
  `available: true` camera — the client must handle that (see error table).

Implementation: iterate the configured map, read `hass.states.get(entity_id)`
(pure in-memory; no executor).

## New endpoint: `GET /api/nvr_browser/live?camera=<nvr-name>` (authed)

Returns the HLS URL for the camera's live stream.

```json
{ "camera": "backyard", "url": "/api/hls/<token>/master_playlist.m3u8", "streamFormat": "hls" }
```

Logic:

1. `name = request.query.get("camera")`. Empty/missing/unknown (not a key in
   `live_cameras`) → `404` `json_message("unknown live camera", HTTPStatus.NOT_FOUND)`.
   **The entity_id is taken only from the operator-configured map, never from
   the request** — that (not a `_safe_rel`-style sanitizer) is the abuse/SSRF
   defense; keep it that way.
2. Resolve `entity_id`. If `hass.states.get(entity_id)` is missing/`unavailable`
   → `503` `json_message("camera unavailable", HTTPStatus.SERVICE_UNAVAILABLE)`.
3. Request the stream from HA's camera component, **bounded by a timeout** so a
   dead RTSP source fails fast instead of hanging the request (HA's worker
   startup is bounded by `OUTPUT_STARTUP_TIMEOUT`, ~60 s — too long to hold a
   JSON view):

   ```python
   from homeassistant.components import camera
   from homeassistant.exceptions import HomeAssistantError

   try:
       url = await asyncio.wait_for(
           camera.async_request_stream(self.hass, entity_id, fmt="hls"),
           timeout=15,
       )
   except (HomeAssistantError, asyncio.TimeoutError, Exception) as err:  # noqa: BLE001
       _LOGGER.warning("nvr_browser: live stream start failed for %s: %s", entity_id, err)
       return self.json_message("camera stream unavailable", HTTPStatus.SERVICE_UNAVAILABLE)
   ```

   - `async_request_stream` creates/reuses the camera's `Stream`, adds the HLS
     provider, starts the worker, and returns the **relative** endpoint URL.
     Re-calling it for the same camera reuses the running stream and bumps
     keepalive (so N TVs on one camera = one HA stream; no `_PROXY_SEM`-style
     throttle is needed).
   - Catch broadly (a bad `fmt` would raise `KeyError`, a dead source can raise
     or time out) and **collapse every start failure to `503`** — a single
     status the client already handles. *Do not emit `502`*; keep the
     server→client error set to exactly `{404 unknown/disabled, 503 unavailable}`.
4. Return `{camera: name, url, streamFormat: "hls"}`. Do **not** sign the URL —
   it already carries the stream's access token. Do **not** prepend the host;
   the client prepends `baseUrl` exactly as it does for signed clip URLs.

Note `/live` is *not* as cheap as `/cameras`: it does worker-startup I/O (hence
the `wait_for`), whereas `/cameras` is an instant state read.

> [!IMPORTANT]
> **Verification box — the load-bearing API.** Verified against
> `home-assistant/core` `dev`: `async_request_stream(hass, entity_id, fmt) ->
> str` calls `async_create_stream()` → `add_provider(fmt)` → `await
> stream.start()` → returns `stream.endpoint_url(fmt)`; the HLS endpoint is
> registered as `/api/hls/{}/master_playlist.m3u8`; `HLS_PROVIDER = "hls"`;
> stream views are `requires_auth = False` and look the stream up by the
> path token; no stream → `HomeAssistantError`. **Still confirm against the
> pinned container version before merge** (`dev` ≠ the running 2026.6 image) and
> **capture the actual returned URL string in this doc** so nvr-roku pins the
> same concrete shape. If the helper moved, replicate the WS `camera/stream`
> handler: `stream = await entity.async_create_stream(); stream.add_provider("hls");
> await stream.start(); url = stream.endpoint_url("hls")` (apply the same
> exception/timeout handling).

## Auth: why the HLS URL needs no signing

The existing `_sign_urls` mints a per-path signed URL because the thumb/clip
views are authed and `<img>/<video>` can't send a bearer token. For live we
don't reuse that mechanism:

1. **HLS is many rolling paths**, not one. `async_sign_path` signs a single
   path; the master playlist references a variant playlist which references a
   rolling window of `.ts` segments. Signing each would be wrong and racy.
2. **HA's stream URL already self-authorizes.** The stream views
   (`requires_auth = False`) validate the `<token>` in the path against the live
   `Stream`'s access token, covering the playlist *and* its segments. So the live
   URL is auth-free-but-tokenized — the same property that lets the Roku Video
   node fetch it with no header, like the signed clip URLs.

The `/cameras` and `/live` JSON endpoints themselves stay **authed**: the bearer
gates *getting* a stream URL, the stream token gates *fetching* it. Same
two-tier model as events→signed-clip.

## Lifecycle & the ephemeral-URL contract (important)

HA's `stream` component owns the HLS lifecycle — **there is no new cache dir, no
pruner, no ffmpeg process in this integration.** But "no cleanup" does **not**
mean "the URL is durable." The `/live` URL is **ephemeral and re-fetchable**:

- HA's `OUTPUT_IDLE_TIMEOUT` is ~30 s. While Roku is actively fetching the
  playlist the stream stays alive; once it stops for >30 s (Back, TV sleep, a
  network blip, app backgrounded) the stream idles out, **the access token is
  cleared, and the previously handed-out URL `404`s permanently.**
- An HA **restart** invalidates all stream tokens (streams are in-memory).

**Contract for the client:** treat the `/live` URL as valid only while the
stream is non-idle. On *any* playback error/404, **re-`GET /api/nvr_browser/live`
to mint a fresh URL** rather than retrying the dead one. Do not persist it. This
is captured in the nvr-roku tracking issue, not just an overlay hint.

`/live` does **not** block until the playlist is fully warm (that would risk the
~60 s worker-startup hang and fights the `wait_for` fast-fail). The first segment
takes ~1–2 s, so the variant playlist can briefly `404` at startup; the client
shows a "Connecting…" overlay and does a bounded re-fetch-and-retry (see the
nvr-roku plan). Server-side we keep `/live` fast and let the client own warm-up.

## Touch list

- `__init__.py`
  - `CONFIG_SCHEMA`: add `vol.Optional("live_cameras"): {cv.string: cv.entity_id}`
    (import `config_validation as cv`).
  - `async_setup`: stash `live_cameras` in `hass.data[DOMAIN]`; register two new
    views.
  - New `NvrCamerasView` + `NvrLiveView` (model on `NvrDaysView`/`NvrClipView`;
    use `HTTPStatus.*` qualified, never bare).
  - `VERSION = "0.9.0"`.
- `manifest.json`: `version` → `0.9.0` (lockstep with `VERSION`).
- `README.md`:
  - add `cameras` and `live` to the **Endpoints** bullet list (the published
    contract surface — not just the config section);
  - document the `live_cameras` config + the **≤1080p H.264 source requirement**
    in user-facing terms;
  - note live needs **0.9.0 → restart HA** (Python change; the same restart that
    picks up the new config). No panel JS change, so no `?v=` cache-bust here.
- `CLAUDE.md`: add `cameras` + `live` to the endpoint list **and** the
  API-contract list; bump "Current dev version" to 0.9.0; note the
  remux-not-transcode constraint, the `live_cameras` map, and the ephemeral-URL
  contract.
- nvr-roku: tracking issue (additive, but the client must build the live UI) +
  update its CLAUDE.md "Compatibility" note to require ha-nvr ≥ 0.9.0 for live
  (clips still work on ≥ 0.8.0).

## Error semantics (client-facing contract)

| Situation | Status | Body message |
|---|---|---|
| `live_cameras` absent / camera not a configured key / endpoint on 0.8.0 | `404` | `unknown live camera` (or no such route on old servers) |
| entity missing / `unavailable` / stream failed to start / timed out | `503` | `camera unavailable` / `camera stream unavailable` |
| success | `200` | `{camera, url, streamFormat:"hls"}` |

(No `502` — every stream-start failure is `503`.)

## Testing without deploying

- `python3 -m py_compile custom_components/nvr_browser/__init__.py` — syntax.
- In a real HA: set `live_cameras`, hit `/api/nvr_browser/cameras` and
  `/api/nvr_browser/live?camera=<name>` with a bearer token; confirm the returned
  `url` plays in an HLS player (`ffplay "$BASE$url"`).
- **Required negative test:** a ≤1080p entity plays on Roku; a 5 MP entity
  returns `200` from `/live` but fails on Roku with `-5` (justifies the config
  requirement and the "operator must pick a Roku-playable entity" docs).

## Out of scope for 0.9.0 (call out, don't build)

- **Transcoding the main 5 MP stream to 1080p.** A *continuous* per-viewer
  ffmpeg/VAAPI process (unlike the one-shot, cached clip-proxy) contending with
  clip transcodes on the single iGPU. Defer; ship the substream path first.
- **Surfacing source resolution in `/cameras`** so the client can warn before
  playback. Nice, but HA doesn't cheaply expose it pre-stream.
- **Design B (proxy go2rtc behind a live-session token).**
- **Multi-camera mosaic / PTZ / two-way audio.**
- **Lower latency.** HLS here is ~5–10 s glass-to-glass — driven by the
  *operator-owned* `stream:` settings (`segment_duration: 2`, `ll_hls: false`;
  the integration can't enforce them). WebRTC would fix it but Roku can't;
  LL-HLS is shaky on Roku and disabled here.

## Rough effort

~180–230 lines in `__init__.py` (two small views + config parsing + the
timeout/exception handling), plus docs/version bumps. The risk is concentrated
in one verifiable spot: the exact `async_request_stream` behavior (verified vs
`dev`; re-confirm vs the pinned image) and that the configured entity is genuinely
Roku-playable. Both are checkable before any Roku work starts — **gate the Roku
work on capturing the real `/live` URL shape from a running 0.9.0.**
