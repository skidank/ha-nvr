"""NVR Browser — a purpose-built gallery for the home-grown NVR clips in /config/nvr.

This integration is intentionally read-only and additive. It does not touch the
recording automations or any file under /config/nvr. It exposes:

  * GET /api/nvr_browser/events  (authed) — newest-first event list, paginated
  * GET /api/nvr_browser/thumb   (authed) — ffmpeg frame-grab, cached to disk
  * GET /api/nvr_browser/clip    (authed) — original clip stream (range-capable)
  * GET /api/nvr_browser/cameras (authed) — live-capable cameras (live_cameras map)
  * GET /api/nvr_browser/live    (authed) — HA HLS URL for a camera's live stream
  * a custom sidebar panel ("NVR") rendering the gallery

Clips live under /config/nvr — OUTSIDE www/, so they are not exposed by HA's
unauthenticated /local/ route. They're streamed via the authed clip view, whose
URLs the events list signs so a plain <video src> still works for the user.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from http import HTTPStatus
from urllib.parse import quote

from aiohttp import web
import voluptuous as vol

from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components import camera, frontend
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "nvr_browser"

# Paths are under HA's config dir. Clips live OUTSIDE www/ so they are NOT served
# by HA's unauthenticated /local/ route; they're streamed via the authed
# /api/nvr_browser/clip endpoint instead.
NVR_DIR = "/config/nvr"
THUMB_DIR = "/config/nvr_thumbs"
# Transcoded, Roku-playable renditions (see the clip-proxy section). Separate
# cache dir; the originals under NVR_DIR are never touched.
PROXY_DIR = "/config/nvr_proxies"

STATIC_JS_URL = "/nvr_browser_static/nvr-browser-panel.js"
PANEL_URL_PATH = "nvr-browser"
PANEL_TITLE = "NVR"
PANEL_ICON = "mdi:filmstrip-box-multiple"
WEBCOMPONENT_NAME = "nvr-browser-panel"
VERSION = "0.9.2"

# --- TV pairing (device-authorization flow for the Roku app) ---------------
# A TV has no HA credentials, so it can't just call the authed events API. The
# pairing flow mints a long-lived token WITHOUT typing it on the TV: the TV gets
# a short human code (pair/new), shows it, and polls (pair/claim); a logged-in
# user approves that code from the panel (pair/approve), which mints a long-lived
# token bound to *their* account and hands it back through the next poll. No
# token is ever issued without an authenticated user approving a code.
_PAIR_TTL = timedelta(minutes=5)        # a pending pairing expires this fast
_PAIR_MAX_PENDING = 20                  # bound the in-memory pending set
_PAIR_CODE_LEN = 6
# Unambiguous alphabet for the displayed code (no 0/O/1/I to mis-read on a TV).
_PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Folder/file shapes produced by the recording automations:
#   <date>/<hour>/<camera>/HH:MM:SS.mp4           -> the canonical clip
#   <date>/<hour>/<object>/HH:MM:SS-<camera>.mp4  -> hard link tagging the clip
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}:00$")
BASE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\.mp4$")
LINK_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})-(.+)\.mp4$")

# Keep ffmpeg from storming the box: only a few frame-grabs at a time.
_THUMB_SEM = asyncio.Semaphore(3)

# --- Clip proxy (Roku-playable transcode) ----------------------------------
# Roku's H.264 decoder maxes out at 1080p, but the source clips are ~5 MP H.264,
# so the TV reports "-5 malformed data". The clip-proxy endpoint transcodes a
# clip down to <=1080p H.264 + faststart on first request, caches it, and serves
# that. Tries the iGPU (VAAPI) and falls back to software libx264.
_PROXY_SEM = asyncio.Semaphore(1)        # one transcode at a time (CPU/GPU bound)
_PROXY_MAX_HEIGHT = 1080
_VAAPI_DEVICE = "/dev/dri/renderD128"
# A long-ish clip on the software fallback can take ~40s; give it room.
_PROXY_TIMEOUT = 300

# --- Live streaming (Roku live view) ----------------------------------------
# Roku has no WebRTC; the only live transport it plays is HLS. HA's stream
# integration already serves a tokenized, same-origin HLS URL per camera entity,
# so /live just hands that URL to the client (no proxy, no transcode). The
# streamed entity MUST be <=1080p H.264 (Roku's decoder limit — the same reason
# clips need a proxy); the operator points each camera at a Roku-playable
# substream via the `live_cameras` config map. The URL is EPHEMERAL: HA idles a
# stream out ~30s after playback stops (and on restart), invalidating its token,
# so the client must re-request /live on a playback error, not reuse a stale URL.
_LIVE_STREAM_TIMEOUT = 15        # backstop cap (HA bounds the source fetch internally, ~10s)
# Camera states a live stream can't start from (HA raises "Camera is off" for
# `off`); /cameras won't advertise these and /live rejects them up front.
_LIVE_UNAVAILABLE_STATES = ("unavailable", "unknown", "off")

# How often to sweep orphaned thumbnails (whose source clip has rotated out).
PRUNE_INTERVAL = timedelta(hours=24)
# Don't delete an in-progress ".part.jpg" younger than this (avoids racing a
# concurrent frame-grab); older ones are leftovers from a crashed run.
_PART_STALE_SECONDS = 3600

# The thumb and clip views are authed, but a plain <img>/<video> can't send a
# bearer token. So the (authenticated) event list hands the frontend short-lived
# *signed* URLs (HA's async_sign_path): each is time-limited and bound to the
# caller's refresh token, so media loads only for the logged-in user who fetched
# the list.
_SIGNED_URL_TTL = timedelta(hours=12)


def _thumb_name(rel: str) -> str:
    """Cache filename for a clip's relative path (shared by the view and pruner)."""
    return f"{hashlib.sha1(rel.encode()).hexdigest()}.jpg"


def _proxy_name(rel: str) -> str:
    """Cache filename for a clip's transcoded Roku-playable proxy."""
    return f"{hashlib.sha1(rel.encode()).hexdigest()}.mp4"

# `nvr_browser:` is commonly an empty (None) block; vol.Any(None, ...) keeps that
# valid. `live_cameras` (optional) maps an NVR camera name -> the HA camera
# entity to stream live for the Roku app; cv.entity_domain enforces a camera
# entity. Absent/empty => live is disabled (/cameras returns [], /live 404s).
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Any(
            None,
            vol.Schema(
                {vol.Optional("live_cameras"): {cv.string: cv.entity_domain("camera")}},
                extra=vol.ALLOW_EXTRA,
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _safe_rel(rel: str) -> str | None:
    """Normalise an incoming relative path and reject anything escaping NVR_DIR."""
    if not rel:
        return None
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm.startswith(".."):
        return None
    full = os.path.normpath(os.path.join(NVR_DIR, norm))
    if full != NVR_DIR and not full.startswith(NVR_DIR + os.sep):
        return None
    return norm


def _build_hour(date: str, hour: str) -> dict[tuple[str, str], dict]:
    """Collapse one hour folder into events keyed by (time, camera)."""
    base = os.path.join(NVR_DIR, date, hour)
    events: dict[tuple[str, str], dict] = {}
    try:
        folders = os.listdir(base)
    except OSError:
        return events

    for folder in folders:
        fdir = os.path.join(base, folder)
        if not os.path.isdir(fdir):
            continue
        try:
            files = os.listdir(fdir)
        except OSError:
            continue
        for fn in files:
            m_base = BASE_RE.match(fn)
            if m_base:
                # folder name *is* the camera; this is the canonical clip.
                t = m_base.group(1)
                cam = folder
                ev = events.setdefault((t, cam), {"time": t, "camera": cam, "objects": set()})
                ev["camera"] = cam
                ev["time"] = t
                continue
            m_link = LINK_RE.match(fn)
            if m_link:
                # folder name is an object label (person/cat/...); camera is in the name.
                t = m_link.group(1)
                cam = m_link.group(2)
                ev = events.setdefault((t, cam), {"time": t, "camera": cam, "objects": set()})
                ev["objects"].add(folder)
    return events


def _list_days() -> list[str]:
    """Return available YYYY-MM-DD folders, newest first."""
    try:
        return sorted((d for d in os.listdir(NVR_DIR) if DATE_RE.match(d)), reverse=True)
    except OSError:
        return []


def _scan(
    offset: int,
    limit: int,
    camera: str | None,
    obj: str | None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Walk the tree newest-first, returning a paginated slice of events.

    `start`/`end` are inclusive YYYY-MM-DD bounds. Date folders sort lexically,
    so plain string comparison gives correct calendar ordering.
    """
    results: list[dict] = []
    need = offset + limit
    dates = _list_days()
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    for date in dates:
        ddir = os.path.join(NVR_DIR, date)
        try:
            hours = sorted((h for h in os.listdir(ddir) if HOUR_RE.match(h)), reverse=True)
        except OSError:
            continue
        for hour in hours:
            hour_events = _build_hour(date, hour)
            for ev in sorted(hour_events.values(), key=lambda e: e["time"], reverse=True):
                if camera and ev["camera"] != camera:
                    continue
                if obj and obj not in ev["objects"]:
                    continue
                rel = f"{date}/{hour}/{ev['camera']}/{ev['time']}.mp4"
                if not os.path.isfile(os.path.join(NVR_DIR, rel)):
                    continue
                results.append(
                    {
                        "id": rel,
                        "date": date,
                        "time": ev["time"],
                        "datetime": f"{date} {ev['time']}",
                        "camera": ev["camera"],
                        "objects": sorted(ev["objects"]),
                        "url": f"/api/nvr_browser/clip?path={quote(rel)}",
                        "proxy": f"/api/nvr_browser/clip_proxy?path={quote(rel)}",
                        "thumb": f"/api/nvr_browser/thumb?path={quote(rel)}",
                    }
                )
            if len(results) >= need:
                break
        if len(results) >= need:
            break

    return results[offset:need]


async def _generate_thumb(src: str, dst: str) -> bool:
    """Grab a single frame ~10s into the clip with the container's bundled ffmpeg.

    Seeks ~10s in to maximise the chance of catching something interesting, with
    fallbacks to earlier offsets for short clips. Writes atomically.
    """
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: os.makedirs(THUMB_DIR, exist_ok=True)
    )
    # The temp file MUST keep a .jpg suffix: ffmpeg infers the output format from
    # the extension, and a bare ".tmp" makes it fail format detection. We also
    # pass "-f image2" so the format is never left to guesswork. The name MUST be
    # unique per grab: HA is single-process, so two concurrent requests for the
    # same uncached clip would otherwise share one temp path — both ffmpegs write
    # it at once (corrupt JPEG) and the second os.replace races the first. A
    # random token makes each attempt's temp file its own; os.replace is atomic,
    # so concurrent grabs of the same clip just both produce a valid thumb.
    tmp = f"{dst}.{os.getpid()}.{os.urandom(4).hex()}.part.jpg"
    last_err = b""
    async with _THUMB_SEM:
        for seek in ("00:00:10", "00:00:03", "00:00:00"):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-ss", seek, "-i", src,
                "-frames:v", "1", "-f", "image2", "-vf", "scale=320:-1", tmp,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, last_err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                continue
            if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dst)
                return True
    if os.path.isfile(tmp):
        os.remove(tmp)
    _LOGGER.warning(
        "nvr_browser: thumbnail generation failed for %s: %s",
        src, last_err.decode(errors="replace")[-300:],
    )
    return False


def _valid_clip_rels() -> set[str]:
    """Relative paths of every canonical clip currently on disk (the keep-set
    both the thumbnail and proxy pruners derive their cache names from)."""
    rels: set[str] = set()
    for date in _list_days():
        ddir = os.path.join(NVR_DIR, date)
        try:
            hours = [h for h in os.listdir(ddir) if HOUR_RE.match(h)]
        except OSError:
            continue
        for hour in hours:
            try:
                folders = os.listdir(os.path.join(ddir, hour))
            except OSError:
                # The hour folder may rotate out mid-sweep — exactly the race the
                # pruner must tolerate. Skip it rather than abort the whole prune.
                continue
            for folder in folders:
                fdir = os.path.join(ddir, hour, folder)
                if not os.path.isdir(fdir):
                    continue
                try:
                    files = os.listdir(fdir)
                except OSError:
                    continue
                for fn in files:
                    # Only canonical clips (camera folders) back a thumb/proxy.
                    if BASE_RE.match(fn):
                        rels.add(f"{date}/{hour}/{folder}/{fn}")
    return rels


def _valid_thumb_names() -> set[str]:
    """Cache filenames for every clip currently on disk (the keep-set)."""
    return {_thumb_name(rel) for rel in _valid_clip_rels()}


def _prune_thumbs() -> int:
    """Delete cached thumbnails whose source clip has rotated out, plus any
    stale temp files. Returns the number removed."""
    try:
        entries = os.listdir(THUMB_DIR)
    except OSError:
        return 0

    keep = _valid_thumb_names()
    now = time.time()
    removed = 0
    for name in entries:
        path = os.path.join(THUMB_DIR, name)
        if name.endswith(".part.jpg"):
            # Leftover temp from a crashed grab — but don't race a live one.
            try:
                if now - os.path.getmtime(path) > _PART_STALE_SECONDS:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
            continue
        if name.endswith(".jpg") and name not in keep:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def _proxy_cmds(src: str, tmp: str) -> list[list[str]]:
    """ffmpeg argv variants for the Roku proxy, fastest-first.

    Both downscale to <=1080p H.264 + AAC with the moov atom up front
    (`+faststart`) so Roku can start playback immediately. The source cameras are
    ~5MP and roughly 4:3, so capping *height* to 1080 keeps width well under
    Roku's 1920 limit; for any clip already <=1080p, `min` leaves it untouched
    (no upscaling). First entry uses the iGPU (VAAPI); second is the CPU fallback.
    """
    scale = f"scale=-2:'min({_PROXY_MAX_HEIGHT},ih)'"
    return [
        # Hardware: CPU decode+scale, upload to the iGPU, hardware H.264 encode.
        [
            "ffmpeg", "-nostdin", "-y", "-vaapi_device", _VAAPI_DEVICE, "-i", src,
            "-vf", f"{scale},format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", "24",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4", tmp,
        ],
        # Software fallback (libx264). Always works given ffmpeg; just slower.
        [
            "ffmpeg", "-nostdin", "-y", "-i", src,
            "-vf", scale,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-f", "mp4", tmp,
        ],
    ]


async def _generate_proxy(src: str, dst: str) -> bool:
    """Transcode a clip to a cached <=1080p Roku-playable proxy. Writes atomically.

    Tries the VAAPI (iGPU) command first, falls back to software libx264. Like the
    thumbnail grabber, the temp file is uniquely named so concurrent requests for
    the same clip can't corrupt each other, and os.replace is atomic.
    """
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: os.makedirs(PROXY_DIR, exist_ok=True)
    )
    tmp = f"{dst}.{os.getpid()}.{os.urandom(4).hex()}.part.mp4"
    last_err = b""
    async with _PROXY_SEM:
        for cmd in _proxy_cmds(src, tmp):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, last_err = await asyncio.wait_for(proc.communicate(), timeout=_PROXY_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                continue
            if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dst)
                return True
            # Hardware path failed (e.g. no/!busy iGPU) — clean up and try software.
            if os.path.isfile(tmp):
                os.remove(tmp)
    _LOGGER.warning(
        "nvr_browser: proxy transcode failed for %s: %s",
        src, last_err.decode(errors="replace")[-300:],
    )
    return False


def _prune_proxies() -> int:
    """Delete cached proxies whose source clip has rotated out, plus stale temps.
    Mirrors _prune_thumbs (same keep-set, .mp4/.part.mp4 instead of .jpg)."""
    try:
        entries = os.listdir(PROXY_DIR)
    except OSError:
        return 0

    keep = {_proxy_name(rel) for rel in _valid_clip_rels()}
    now = time.time()
    removed = 0
    for name in entries:
        path = os.path.join(PROXY_DIR, name)
        if name.endswith(".part.mp4"):
            try:
                if now - os.path.getmtime(path) > _PART_STALE_SECONDS:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
            continue
        if name.endswith(".mp4") and name not in keep:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def _get_signer(hass: HomeAssistant):
    """Return a `(path, ttl) -> signed_url` callable, or None if unavailable.

    HA's signed-path helper lets an authenticated request mint a time-limited,
    refresh-token-bound URL so a plain `<img>`/`<video>`/Roku `Poster` (which
    can't send a bearer token) still loads only for the user who fetched the
    list. The canonical API is the module function; we also probe
    `hass.http.async_sign_path` first in case a build/fork exposes it there.
    Shared by the events view (clip thumb/clip/proxy URLs) and the cameras view
    (camera snapshot URLs)."""
    signer = getattr(hass.http, "async_sign_path", None)
    if signer is not None:
        return signer
    try:
        from homeassistant.components.http.auth import async_sign_path
    except ImportError:
        _LOGGER.warning("nvr_browser: async_sign_path unavailable; media won't load")
        return None
    return lambda path, exp: async_sign_path(hass, path, exp)  # noqa: E731


class NvrEventsView(HomeAssistantView):
    """Authed JSON list of events, newest first."""

    url = "/api/nvr_browser/events"
    name = "api:nvr_browser:events"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            offset = max(0, int(q.get("offset", 0)))
            limit = min(200, max(1, int(q.get("limit", 60))))
        except ValueError:
            return self.json_message("invalid paging", HTTPStatus.BAD_REQUEST)
        camera = q.get("camera") or None
        obj = q.get("object") or None
        start = q.get("start") if DATE_RE.match(q.get("start", "")) else None
        end = q.get("end") if DATE_RE.match(q.get("end", "")) else None
        events = await self.hass.async_add_executor_job(
            _scan, offset, limit, camera, obj, start, end
        )
        self._sign_urls(events)
        return self.json(
            {"events": events, "offset": offset, "limit": limit, "count": len(events)}
        )

    def _sign_urls(self, events: list[dict]) -> None:
        """Replace each event's thumb + clip path with a short-lived signed URL.

        The thumb and clip views require auth, but a plain <img>/<video> can't
        carry a bearer token. HA's signed-path mechanism bridges that: this
        (authenticated) request mints a time-limited, refresh-token-bound URL per
        asset, so media loads for this user without exposing an unauthed endpoint.
        """
        signer = _get_signer(self.hass)
        if signer is None:
            return
        for ev in events:
            try:
                ev["thumb"] = signer(ev["thumb"], _SIGNED_URL_TTL)
                ev["url"] = signer(ev["url"], _SIGNED_URL_TTL)
                # The Roku app plays the transcoded proxy (the original is too
                # high-res for its decoder); sign that URL the same way.
                ev["proxy"] = signer(ev["proxy"], _SIGNED_URL_TTL)
            except Exception as err:  # noqa: BLE001 — never let signing 500 the list
                _LOGGER.warning("nvr_browser: media URL signing failed: %s", err)
                return


class NvrDaysView(HomeAssistantView):
    """Authed list of available YYYY-MM-DD folders, newest first."""

    url = "/api/nvr_browser/days"
    name = "api:nvr_browser:days"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        days = await self.hass.async_add_executor_job(_list_days)
        return self.json({"days": days})


class NvrThumbView(HomeAssistantView):
    """Cached JPEG thumbnail. Authed (the default): the event list hands the
    frontend signed URLs so a plain <img src> still loads only for the
    logged-in user who fetched the list."""

    url = "/api/nvr_browser/thumb"
    name = "api:nvr_browser:thumb"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        rel = _safe_rel(request.query.get("path", ""))
        if not rel:
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        src = os.path.join(NVR_DIR, rel)
        if not os.path.isfile(src):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        dst = os.path.join(THUMB_DIR, _thumb_name(rel))
        if not os.path.isfile(dst):
            if not await _generate_thumb(src, dst):
                return web.Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return web.FileResponse(dst, headers={"Cache-Control": "max-age=86400"})


class NvrClipView(HomeAssistantView):
    """Authed clip stream. Clips live outside www/, so HA's public /local/ route
    no longer serves them — this serves them by absolute path, with auth. aiohttp's
    FileResponse honours HTTP range requests, so <video> seeking works; the event
    list hands out signed URLs (see NvrEventsView._sign_urls)."""

    url = "/api/nvr_browser/clip"
    name = "api:nvr_browser:clip"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        rel = _safe_rel(request.query.get("path", ""))
        if not rel:
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        src = os.path.join(NVR_DIR, rel)
        if not os.path.isfile(src):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.FileResponse(src)


class NvrClipProxyView(HomeAssistantView):
    """Authed, Roku-playable transcode of a clip (<=1080p H.264 + faststart).

    The originals are ~5MP H.264, which Roku's decoder can't handle. On first
    request this transcodes + caches a downscaled rendition (VAAPI, else libx264)
    and serves it range-capably; repeat plays hit the cache. Signed like /clip."""

    url = "/api/nvr_browser/clip_proxy"
    name = "api:nvr_browser:clip_proxy"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        rel = _safe_rel(request.query.get("path", ""))
        if not rel:
            return web.Response(status=HTTPStatus.BAD_REQUEST)
        src = os.path.join(NVR_DIR, rel)
        if not os.path.isfile(src):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        dst = os.path.join(PROXY_DIR, _proxy_name(rel))
        if not os.path.isfile(dst):
            if not await _generate_proxy(src, dst):
                return web.Response(status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return web.FileResponse(dst)


class NvrCamerasView(HomeAssistantView):
    """Authed list of live-capable cameras (from the `live_cameras` config map).

    Authoritative list for the Roku app's "Watch live" picker — independent of
    whether a camera has recorded clips. `available` is a liveness *hint* (entity
    present, state not in `_LIVE_UNAVAILABLE_STATES`, advertises STREAM), not a
    guarantee: a camera can still fail /live if its underlying source is dead, so
    the client must cope. Available cameras also carry a signed `thumb` (a current
    still via HA's camera_proxy) for the picker tile.
    """

    url = "/api/nvr_browser/cameras"
    name = "api:nvr_browser:cameras"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        live_cameras = self.hass.data[DOMAIN].get("live_cameras", {})
        signer = _get_signer(self.hass)
        cameras = []
        for name in sorted(live_cameras):
            entity_id = live_cameras[name]
            state = self.hass.states.get(entity_id)
            features = state.attributes.get("supported_features", 0) if state else 0
            available = (
                state is not None
                and state.state not in _LIVE_UNAVAILABLE_STATES
                and bool(features & CameraEntityFeature.STREAM)
            )
            title = (
                (state and state.attributes.get("friendly_name"))
                or name.replace("_", " ").title()
            )
            cam = {
                "name": name,
                "entity_id": entity_id,
                "title": title,
                "available": available,
            }
            # A signed snapshot URL the Roku Poster can load without a bearer (same
            # async_sign_path mechanism as clip thumbs). Gated on `available` — the
            # *live* gate, reused as a best-effort still gate: STREAM capability
            # doesn't strictly imply still support, but either way a camera that
            # can't produce a frame just makes camera_proxy error and the client
            # falls back to a placeholder tile. Stills are served on demand by HA's
            # camera component (no caching/pruning/ffmpeg of our own).
            if available and signer is not None:
                try:
                    cam["thumb"] = signer(
                        f"/api/camera_proxy/{entity_id}", _SIGNED_URL_TTL
                    )
                except Exception as err:  # noqa: BLE001 — never let signing 500 the list
                    _LOGGER.warning(
                        "nvr_browser: camera thumb signing failed for %s: %s",
                        entity_id, err,
                    )
            cameras.append(cam)
        return self.json({"cameras": cameras})


class NvrLiveView(HomeAssistantView):
    """Authed: returns HA's native HLS URL for a camera's live stream.

    Roku has no WebRTC, so live uses HLS. HA's stream integration already serves a
    tokenized, same-origin HLS URL per camera entity; we just hand that URL to the
    client (no proxy, no transcode). The URL self-authorizes via the token in its
    path (HA's stream views are unauthed-but-tokenized), so the Roku <video> needs
    no bearer — same model as the signed clip URLs. The URL is EPHEMERAL (see
    _LIVE_STREAM_TIMEOUT note): the client must re-request /live on a playback
    error, not reuse a stale one.
    """

    url = "/api/nvr_browser/live"
    name = "api:nvr_browser:live"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        name = request.query.get("camera", "")
        live_cameras = self.hass.data[DOMAIN].get("live_cameras", {})
        # The entity_id is taken ONLY from the operator-configured map, never from
        # the request — that (not a path sanitiser) is the abuse defence.
        entity_id = live_cameras.get(name)
        if not entity_id:
            return self.json_message("unknown live camera", HTTPStatus.NOT_FOUND)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _LIVE_UNAVAILABLE_STATES:
            return self.json_message(
                "camera unavailable", HTTPStatus.SERVICE_UNAVAILABLE
            )
        try:
            # Backstop timeout: async_request_stream fetches the source + spawns the
            # HLS worker (HA bounds the source fetch internally, ~10s); cap it here
            # too so a wedged start can't hold the request open. A cancelled start
            # may leave a cached Stream on the entity — harmless: HA reuses it on the
            # next request and idles it out.
            url = await asyncio.wait_for(
                camera.async_request_stream(self.hass, entity_id, "hls"),
                timeout=_LIVE_STREAM_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001 — any start failure => 503, never 500
            _LOGGER.warning(
                "nvr_browser: live stream start failed for %s: %s", entity_id, err
            )
            return self.json_message(
                "camera stream unavailable", HTTPStatus.SERVICE_UNAVAILABLE
            )
        return self.json({"camera": name, "url": url, "streamFormat": "hls"})


def _purge_pairings(pairings: dict) -> None:
    """Drop pending pairings older than the TTL (monotonic clock; in-place)."""
    now = time.monotonic()
    ttl = _PAIR_TTL.total_seconds()
    for code in [c for c, s in pairings.items() if now - s["created"] > ttl]:
        pairings.pop(code, None)


class NvrPairNewView(HomeAssistantView):
    """Unauthed: a TV starts pairing and gets a short display code + poll secret.

    Safe to leave unauthed because it only creates a *pending* request — no token
    is issued here. Bounded + TTL'd so it can't be used to exhaust memory.
    """

    url = "/api/nvr_browser/pair/new"
    name = "api:nvr_browser:pair:new"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        pairings = self.hass.data[DOMAIN]["pairings"]
        _purge_pairings(pairings)
        if len(pairings) >= _PAIR_MAX_PENDING:
            return self.json_message(
                "too many pending pairings; try again shortly",
                HTTPStatus.TOO_MANY_REQUESTS,
            )
        code = ""
        for _ in range(10):
            code = "".join(secrets.choice(_PAIR_CODE_ALPHABET) for _ in range(_PAIR_CODE_LEN))
            if code not in pairings:
                break
        secret = secrets.token_urlsafe(32)
        pairings[code] = {"secret": secret, "created": time.monotonic(), "token": None}
        return self.json({"code": code, "secret": secret})


class NvrPairClaimView(HomeAssistantView):
    """Unauthed: the TV polls with its secret; returns the token once approved.

    The secret is a 32-byte unguessable value only the requesting TV holds, so an
    approved token is delivered only to that TV. Single-use: the session is
    dropped as soon as the token is handed over.
    """

    url = "/api/nvr_browser/pair/claim"
    name = "api:nvr_browser:pair:claim"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        secret = request.query.get("secret", "")
        pairings = self.hass.data[DOMAIN]["pairings"]
        _purge_pairings(pairings)
        if not secret:
            return self.json({"status": "expired"})
        for code, session in pairings.items():
            if secrets.compare_digest(session["secret"], secret):
                if session["token"]:
                    token = session["token"]
                    pairings.pop(code, None)   # single-use
                    return self.json({"status": "approved", "token": token})
                return self.json({"status": "pending"})
        return self.json({"status": "expired"})


class NvrPairApproveView(HomeAssistantView):
    """Authed: a logged-in user approves a code, minting a long-lived token.

    This is the ONLY place a token is created, and it's bound to the approving
    user's account. The token has the same full scope as any HA long-lived token
    (HA has no per-integration token scoping), so only pair TVs you trust.
    """

    url = "/api/nvr_browser/pair/approve"
    name = "api:nvr_browser:pair:approve"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return self.json_message("invalid body", HTTPStatus.BAD_REQUEST)
        code = (data.get("code") or "").strip().upper()
        pairings = self.hass.data[DOMAIN]["pairings"]
        _purge_pairings(pairings)
        session = pairings.get(code)
        if session is None:
            return self.json_message("invalid or expired code", HTTPStatus.BAD_REQUEST)
        if session["token"]:
            return self.json_message("code already used", HTTPStatus.BAD_REQUEST)
        user = request["hass_user"]
        try:
            refresh_token = await self.hass.auth.async_create_refresh_token(
                user,
                client_name=f"NVR Roku ({code}·{session['secret'][:6]})",
                token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                # Without this the minted access token inherits the 30-min default
                # and the "long-lived" token would expire almost immediately. HA's
                # own UI uses a 10-year lifespan for long-lived tokens.
                access_token_expiration=timedelta(days=3650),
            )
        except ValueError as err:
            return self.json_message(f"could not create token: {err}", HTTPStatus.BAD_REQUEST)
        session["token"] = self.hass.auth.async_create_access_token(refresh_token)
        return self.json({"status": "approved"})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the API views and the custom sidebar panel."""
    js_path = os.path.join(os.path.dirname(__file__), "nvr-browser-panel.js")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_JS_URL, js_path, False)]
    )

    # Pending TV pairings live in memory (ephemeral, 5-min TTL); a restart just
    # means re-pairing, which is fine.
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data["pairings"] = {}
    # Live-stream map: NVR camera name -> HA camera entity_id (empty => live off).
    domain_data["live_cameras"] = (config.get(DOMAIN) or {}).get("live_cameras", {})

    hass.http.register_view(NvrEventsView(hass))
    hass.http.register_view(NvrDaysView(hass))
    hass.http.register_view(NvrThumbView(hass))
    hass.http.register_view(NvrClipView(hass))
    hass.http.register_view(NvrClipProxyView(hass))
    hass.http.register_view(NvrCamerasView(hass))
    hass.http.register_view(NvrLiveView(hass))
    hass.http.register_view(NvrPairNewView(hass))
    hass.http.register_view(NvrPairClaimView(hass))
    hass.http.register_view(NvrPairApproveView(hass))

    await hass.async_add_executor_job(
        lambda: (os.makedirs(THUMB_DIR, exist_ok=True), os.makedirs(PROXY_DIR, exist_ok=True))
    )

    async def _prune_job(_now=None) -> None:
        removed = await hass.async_add_executor_job(_prune_thumbs)
        if removed:
            _LOGGER.info("nvr_browser: pruned %d orphaned thumbnail(s)", removed)
        removed = await hass.async_add_executor_job(_prune_proxies)
        if removed:
            _LOGGER.info("nvr_browser: pruned %d orphaned proxy clip(s)", removed)

    # Sweep once at startup, then daily. Tracks the recording retention: when a
    # day's clips are deleted, the next sweep drops their thumbnails.
    async_track_time_interval(hass, _prune_job, PRUNE_INTERVAL)
    hass.async_create_task(_prune_job())

    frontend.async_register_built_in_panel(
        hass,
        "custom",
        PANEL_TITLE,
        PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": WEBCOMPONENT_NAME,
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{STATIC_JS_URL}?v={VERSION}",
            }
        },
        require_admin=False,
    )

    _LOGGER.info("NVR Browser ready — sidebar panel at /%s", PANEL_URL_PATH)
    return True
