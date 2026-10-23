"""Typed, bounded client for Carter's separately deployed log-analysis backend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp


MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_SVG_BYTES = 2 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
REPORT_ID = re.compile(r"^[A-Za-z0-9]{8,32}$")
DEATH_ID = re.compile(r"^[A-Za-z0-9:._-]{1,240}$")


class AnalysisClientError(RuntimeError):
    """A credential-safe backend failure suitable for logs and Discord replies."""

    def __init__(self, code: str, *, status: int | None = None, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class AnalysisClientSettings:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8765/api/v1"
    timeout: float = 15.0
    allow_insecure_remote: bool = False

    def validated(self) -> "AnalysisClientSettings":
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("analysis_api_url_invalid")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("analysis_api_url_invalid")
        if not parsed.path.rstrip("/").endswith("/api/v1"):
            raise ValueError("analysis_api_url_requires_api_v1")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.casefold() == "localhost"
        if parsed.scheme != "https" and not loopback and not self.allow_insecure_remote:
            raise ValueError("analysis_api_remote_http_rejected")
        if not 1 <= float(self.timeout) <= 120:
            raise ValueError("analysis_api_timeout_invalid")
        return self


@dataclass(frozen=True)
class AnalysisImage:
    data: bytes
    filename: str
    alt_text: str


class AnalysisClient:
    """Small async HTTP adapter; the backend remains the data/analysis owner."""

    def __init__(self, settings: AnalysisClientSettings, session=None):
        self.settings = settings.validated()
        self._session = session
        self._owns_session = session is None

    async def _http(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def close(self):
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *, params=None, payload=None):
        if not self.settings.enabled:
            raise AnalysisClientError("analysis_disabled")
        session = await self._http()
        url = self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            async with session.request(
                method, url, params=params, json=payload, allow_redirects=False,
            ) as response:
                length = response.content_length
                if length is not None and length > MAX_RESPONSE_BYTES:
                    raise AnalysisClientError("response_too_large", status=response.status)
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AnalysisClientError("response_too_large", status=response.status)
                    chunks.append(chunk)
                try:
                    data = json.loads(b"".join(chunks).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise AnalysisClientError("invalid_response", status=response.status) from None
                if not isinstance(data, dict):
                    raise AnalysisClientError("invalid_response", status=response.status)
                if response.status >= 400:
                    error = data.get("error") if isinstance(data.get("error"), dict) else {}
                    code = str(error.get("code") or f"http_{response.status}")
                    safe_details = {
                        key: error[key] for key in (
                            "candidates", "oldest_cursor", "report_id", "fight_id", "attempt",
                        ) if key in error
                    }
                    raise AnalysisClientError(code, status=response.status, details=safe_details)
                return data
        except AnalysisClientError:
            raise
        except asyncio.TimeoutError:
            raise AnalysisClientError("analysis_timeout") from None
        except aiohttp.ClientError:
            raise AnalysisClientError("analysis_unavailable") from None

    async def health(self):
        return await self._request("GET", "health")

    async def reports(self):
        return await self._request("GET", "reports")

    async def fights(self, report_id: str):
        return await self._request("GET", f"reports/{_report(report_id)}/fights")

    async def summary(self, report_id: str, fight_id: int):
        return await self._request("GET", f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/summary")

    async def ranking(self, report_id: str, fight_id: int, metric: str, limit: int = 10):
        metric = _metric(metric)
        return await self._request(
            "GET", f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/rankings/{metric}",
            params={"limit": _limit(limit)},
        )

    async def players(self, report_id: str, fight_id: int, query: str):
        return await self._request(
            "GET", f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/players",
            params={"query": _query(query)},
        )

    async def player(self, report_id: str, fight_id: int, player_id: str):
        player_id = quote(_identifier(player_id), safe="")
        return await self._request(
            "GET", f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/players/{player_id}",
        )

    async def deaths(self, report_id: str, fight_id: int, *, player_name: str | None = None):
        params = {"player_name": _query(player_name)} if player_name else None
        return await self._request(
            "GET", f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/deaths", params=params,
        )

    async def death_answer(self, report_id: str, fight_id: int, death_id: str,
                           seconds_before: float = 10):
        return await self._request(
            "GET",
            f"reports/{_report(report_id)}/fights/{_fight(fight_id)}/deaths/"
            f"{quote(_death(death_id), safe='')}/answer",
            params={"seconds_before": _seconds(seconds_before)},
        )

    async def live_status(self):
        return await self._request("GET", "live/status")

    async def live_summary(self):
        return await self._request("GET", "live/summary")

    async def live_ranking(self, metric: str, limit: int = 10):
        return await self._request(
            "GET", f"live/rankings/{_metric(metric)}", params={"limit": _limit(limit)},
        )

    async def live_players(self, query: str):
        return await self._request("GET", "live/players", params={"query": _query(query)})

    async def live_player(self, player_id: str):
        return await self._request("GET", f"live/players/{quote(_identifier(player_id), safe='')}")

    async def live_deaths(self, *, player_name: str | None = None):
        params = {"player_name": _query(player_name)} if player_name else None
        return await self._request("GET", "live/deaths", params=params)

    async def live_death_answer(self, death_id: str, seconds_before: float = 10):
        return await self._request(
            "GET", f"live/deaths/{quote(_death(death_id), safe='')}/answer",
            params={"seconds_before": _seconds(seconds_before)},
        )

    async def live_attempts(self):
        return await self._request("GET", "live/attempts")

    async def archived_summary(self, attempt: int):
        return await self._request("GET", f"live/attempts/{_fight(attempt)}/summary")

    async def archived_ranking(self, attempt: int, metric: str, limit: int = 10):
        return await self._request(
            "GET", f"live/attempts/{_fight(attempt)}/rankings/{_metric(metric)}",
            params={"limit": _limit(limit)},
        )

    async def archived_players(self, attempt: int, query: str):
        return await self._request(
            "GET", f"live/attempts/{_fight(attempt)}/players", params={"query": _query(query)},
        )

    async def archived_player(self, attempt: int, player_id: str):
        return await self._request(
            "GET", f"live/attempts/{_fight(attempt)}/players/{quote(_identifier(player_id), safe='')}",
        )

    async def archived_deaths(self, attempt: int, *, player_name: str | None = None):
        params = {"player_name": _query(player_name)} if player_name else None
        return await self._request("GET", f"live/attempts/{_fight(attempt)}/deaths", params=params)

    async def archived_death_answer(self, attempt: int, death_id: str,
                                    seconds_before: float = 10):
        return await self._request(
            "GET", f"live/attempts/{_fight(attempt)}/deaths/{quote(_death(death_id), safe='')}/answer",
            params={"seconds_before": _seconds(seconds_before)},
        )

    async def import_report(self, report_url: str):
        value = str(report_url).strip()
        if not 1 <= len(value) <= 500:
            raise AnalysisClientError("invalid_report_url")
        return await self._request("POST", "imports", payload={"report_url": value})

    async def import_job(self, job_id: str):
        return await self._request("GET", f"imports/{quote(_identifier(job_id), safe='')}")


def render_answer_image(payload: dict[str, Any]) -> AnalysisImage | None:
    """Convert one trusted backend SVG payload into a bounded Discord PNG."""
    image = payload.get("image")
    if not isinstance(image, dict) or image.get("status") != "available":
        return None
    if image.get("mime_type") != "image/svg+xml" or image.get("encoding") != "base64":
        raise AnalysisClientError("unsupported_image")
    encoded = image.get("data")
    if not isinstance(encoded, str) or len(encoded) > (MAX_SVG_BYTES * 4 // 3 + 16):
        raise AnalysisClientError("invalid_image")
    try:
        svg = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise AnalysisClientError("invalid_image") from None
    lowered = svg[:4096].lower()
    unsafe_fragments = (
        b"<!doctype", b"<!entity", b"<script", b"<foreignobject", b"<image",
        b"href=", b"url(", b"@import", b"javascript:", b"file:",
    )
    full_lowered = svg.lower()
    if (len(svg) > MAX_SVG_BYTES or b"<svg" not in lowered
            or any(fragment in full_lowered for fragment in unsafe_fragments)):
        raise AnalysisClientError("invalid_image")
    try:
        import cairosvg
    except ImportError:
        raise AnalysisClientError("image_renderer_unavailable") from None
    try:
        output = io.BytesIO()
        cairosvg.svg2png(bytestring=svg, write_to=output, output_width=1200, output_height=900)
        png = output.getvalue()
    except Exception:
        raise AnalysisClientError("image_render_failed") from None
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > MAX_PNG_BYTES:
        raise AnalysisClientError("image_render_failed")
    report = str(payload.get("report_id") or payload.get("session_id") or "raid")
    fight = str(payload.get("fight_id") or payload.get("attempt") or "fight")
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", f"{report}-{fight}")[:100]
    alt = str(image.get("alt_text") or "Recorded positions at the selected death")[:300]
    return AnalysisImage(png, f"carter-death-{safe}.png", alt)


def answer_without_image_bytes(payload: dict[str, Any]) -> dict[str, Any]:
    """Return concise tool context while intentionally excluding base64 image bytes."""
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    primary = _compact_evidence(analysis.get("primary_cause"), include_contributors=True)
    killing = _compact_evidence(analysis.get("killing_blow"))
    player = analysis.get("player") if isinstance(analysis.get("player"), dict) else {}
    return {
        "text": str(answer.get("text") or "No explanation was returned.")[:2000],
        "report_id": payload.get("report_id"),
        "fight_id": payload.get("fight_id"),
        "session_id": payload.get("session_id"),
        "attempt": payload.get("attempt"),
        "death_id": payload.get("death_id"),
        "player": {key: _bounded_scalar(player.get(key)) for key in ("id", "name", "class")},
        "time_seconds": analysis.get("time_seconds"),
        "killing_blow": killing,
        "primary_cause": primary,
        "image": {
            key: _bounded_scalar(image.get(key)) for key in (
                "status", "reason", "kind", "map_id", "time_seconds", "position_confidence",
                "sample_age_seconds", "alt_text", "width", "height",
            ) if key in image
        },
    }


def _bounded_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:300]


def _compact_evidence(value, *, include_contributors=False):
    if not isinstance(value, dict):
        return None
    keys = (
        "type", "source_guid", "source", "ability_id", "ability", "amount", "overkill",
        "provenance", "confidence", "reason", "time", "health_before", "health_after",
    )
    result = {key: _bounded_scalar(value.get(key)) for key in keys if key in value}
    contributors = value.get("contributing_events")
    if include_contributors and isinstance(contributors, list):
        result["contributing_events"] = [
            _compact_evidence(item) for item in contributors[:10] if isinstance(item, dict)
        ]
        result["contributing_events_truncated"] = len(contributors) > 10
    return result


def _report(value: str) -> str:
    value = str(value)
    if not REPORT_ID.fullmatch(value):
        raise AnalysisClientError("invalid_report_id")
    return value


def _fight(value: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise AnalysisClientError("invalid_fight_id") from None
    if value <= 0:
        raise AnalysisClientError("invalid_fight_id")
    return value


def _metric(value: str) -> str:
    value = str(value).casefold()
    if value not in {"damage", "healing", "dps", "hps"}:
        raise AnalysisClientError("invalid_metric")
    return value


def _limit(value: int) -> int:
    try:
        return min(max(1, int(value)), 50)
    except (TypeError, ValueError):
        raise AnalysisClientError("invalid_limit") from None


def _seconds(value: float) -> float:
    try:
        return min(max(0.0, float(value)), 30.0)
    except (TypeError, ValueError):
        raise AnalysisClientError("invalid_seconds") from None


def _query(value: str) -> str:
    value = str(value).strip()
    if not 1 <= len(value) <= 100 or any(ord(char) < 32 for char in value):
        raise AnalysisClientError("invalid_query")
    return value


def _identifier(value: str) -> str:
    value = str(value).strip()
    if not 1 <= len(value) <= 240 or "/" in value or any(ord(char) < 32 for char in value):
        raise AnalysisClientError("invalid_identifier")
    return value


def _death(value: str) -> str:
    value = str(value).strip()
    if not DEATH_ID.fullmatch(value):
        raise AnalysisClientError("invalid_death_id")
    return value
