"""
Gemini API Client with rate limiting.

Limits: 15 RPM / 250k TPM.
Design principles:
  - Async wrapper around the synchronous google-genai SDK
  - Token-bucket rate limiter enforces RPM and TPM budgets
  - Optional screenshot compression (disabled by default; available for bandwidth saving)
    - Structured JSON output via response_schema (Pydantic-native)
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from typing import Any, Optional, Type, TypeVar

import structlog
from pydantic import BaseModel

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


def _sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Gemini response_schema does not accept JSON Schema `default` fields.
    Remove unsupported keys recursively while preserving structure.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k == "default":
                continue
            out[k] = _sanitize_schema_for_gemini(v)
        return out
    if isinstance(schema, list):
        return [_sanitize_schema_for_gemini(x) for x in schema]
    return schema


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Token-bucket rate limiter for the Gemini API.

    Enforces two constraints simultaneously:
      1. RPM  — minimum (60 / rpm) seconds between consecutive calls
      2. TPM  — stop if estimated token spend for this minute would exceed budget;
                wait for the minute window to reset before proceeding.
    """

    def __init__(self, rpm: int = 15, tpm: int = 250_000):
        self.rpm = rpm
        self.tpm = tpm
        self._min_interval: float = 60.0 / rpm
        self._last_call: float = 0.0
        self._minute_start: float = time.monotonic()
        self._tokens_this_minute: int = 0
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_tokens: int = 80) -> None:
        async with self._lock:
            now = time.monotonic()

            # Reset per-minute token counter
            if now - self._minute_start >= 60.0:
                self._tokens_this_minute = 0
                self._minute_start = now

            # Wait if token budget is exhausted
            if self._tokens_this_minute + estimated_tokens > self.tpm:
                wait = 60.0 - (now - self._minute_start) + 0.2
                log.info("rate_limit_tpm_wait", wait_s=round(wait, 1),
                         tokens_used=self._tokens_this_minute)
                await asyncio.sleep(wait)
                self._tokens_this_minute = 0
                self._minute_start = time.monotonic()

            # Enforce minimum inter-call interval (RPM)
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                log.debug("rate_limit_rpm_wait", wait_s=round(wait, 2))
                await asyncio.sleep(wait)

            self._tokens_this_minute += estimated_tokens
            self._last_call = time.monotonic()


# ── Screenshot compressor ─────────────────────────────────────────────────────

def compress_screenshot(b64_png: str, max_width: int = 1280, quality: int = 85) -> str:
    """
    JPEG-compress a PNG screenshot to reduce upload size and latency.

    Defaults preserve full viewport width (1280 px) at high quality (85).
    Pass lower values only if you need to reduce bandwidth further.
    Falls back to the original bytes if Pillow is unavailable.
    """
    try:
        from PIL import Image  # type: ignore
        data = base64.b64decode(b64_png)
        img = Image.open(io.BytesIO(data))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64_png   # graceful degradation


# ── Gemini client ─────────────────────────────────────────────────────────────

class GeminiClient:
    """
    Thin async wrapper around the google-genai SDK.

    The SDK is synchronous; we dispatch to a thread executor so callers can
    use ``await`` without blocking the event loop.

    Usage::

        client = GeminiClient(api_key=settings.google_api_key)
        result: MyModel = await client.text(prompt, response_model=MyModel)
        result: MyModel = await client.vision(screenshot_b64, prompt, response_model=MyModel)
    """

    def __init__(self, api_key: str, model: str, rpm: int = 15, tpm: int = 250_000):
        from google import genai  # type: ignore
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self._limiter = RateLimiter(rpm=rpm, tpm=tpm)

    # ── text ─────────────────────────────────────────────────────────────────

    async def text(
        self,
        prompt: str,
        system: str = "",
        response_model: Optional[Type[T]] = None,
        estimated_tokens: int = 120,
    ) -> Any:
        """
        Text-only generation.
        Returns a validated Pydantic instance when ``response_model`` is given,
        otherwise a plain string.
        """
        from google.genai import types  # type: ignore

        await self._limiter.acquire(estimated_tokens)

        cfg: dict = {}
        if system:
            cfg["system_instruction"] = system
        if response_model is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = _sanitize_schema_for_gemini(
                response_model.model_json_schema()
            )

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg) if cfg else None,
            ),
        )

        raw = response.text or ""
        _log_usage(response, "text")

        if response_model is not None:
            return response_model.model_validate_json(raw)
        return raw

    # ── vision ────────────────────────────────────────────────────────────────

    async def vision(
        self,
        screenshot_b64: str,
        prompt: str,
        system: str = "",
        response_model: Optional[Type[T]] = None,
        estimated_tokens: int = 180,
        compress: bool = True,
    ) -> Any:
        """
        Vision generation (screenshot + text prompt).
        Screenshot is JPEG-compressed if ``compress=True`` to save image tokens.
        """
        from google.genai import types  # type: ignore

        await self._limiter.acquire(estimated_tokens)

        img_b64 = compress_screenshot(screenshot_b64) if compress else screenshot_b64
        img_bytes = base64.b64decode(img_b64)
        mime = "image/jpeg" if compress else "image/png"

        cfg: dict = {}
        if system:
            cfg["system_instruction"] = system
        if response_model is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = _sanitize_schema_for_gemini(
                response_model.model_json_schema()
            )

        contents = [
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            prompt,
        ]

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg) if cfg else None,
            ),
        )

        raw = response.text or ""
        _log_usage(response, "vision")

        if response_model is not None:
            return response_model.model_validate_json(raw)
        return raw

    # ── vision_pair ───────────────────────────────────────────────────────────

    async def vision_pair(
        self,
        screenshot_before_b64: str,
        screenshot_after_b64: str,
        prompt: str,
        system: str = "",
        response_model: Optional[Type[T]] = None,
        estimated_tokens: int = 300,
        compress: bool = True,
    ) -> Any:
        """
        Vision call with TWO screenshots (before + after an action).
        Used for visual verification: did the ADD button click succeed?
        """
        from google.genai import types  # type: ignore

        await self._limiter.acquire(estimated_tokens)

        def _prepare(b64: str) -> tuple[bytes, str]:
            compressed = compress_screenshot(b64) if compress else b64
            return base64.b64decode(compressed), ("image/jpeg" if compress else "image/png")

        before_bytes, mime = _prepare(screenshot_before_b64)
        after_bytes, _ = _prepare(screenshot_after_b64)

        cfg: dict = {}
        if system:
            cfg["system_instruction"] = system
        if response_model is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = _sanitize_schema_for_gemini(
                response_model.model_json_schema()
            )

        contents = [
            types.Part.from_bytes(data=before_bytes, mime_type=mime),
            types.Part.from_bytes(data=after_bytes, mime_type=mime),
            prompt,
        ]

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**cfg) if cfg else None,
            ),
        )

        raw = response.text or ""
        _log_usage(response, "vision_pair")

        if response_model is not None:
            return response_model.model_validate_json(raw)
        return raw


# ── helpers ───────────────────────────────────────────────────────────────────

def _log_usage(response: Any, call_type: str) -> None:
    try:
        meta = response.usage_metadata
        if meta:
            log.debug(
                "gemini_usage",
                call_type=call_type,
                input_tokens=meta.prompt_token_count,
                output_tokens=meta.candidates_token_count,
                total=meta.total_token_count,
            )
    except Exception:
        pass


# ── singleton factory ─────────────────────────────────────────────────────────

_client: Optional[GeminiClient] = None


def get_client() -> GeminiClient:
    """Return the module-level singleton GeminiClient, creating it on first call."""
    global _client
    if _client is None:
        from gemini.config.settings import settings
        if not settings.google_api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Add it to your .env file."
            )
        _client = GeminiClient(
            api_key=settings.google_api_key,
            model=settings.gemini_model,
            rpm=settings.gemini_rpm,
            tpm=settings.gemini_tpm,
        )
    return _client
