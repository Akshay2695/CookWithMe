import os
from pathlib import Path
from pydantic_settings import BaseSettings


class GeminiSettings(BaseSettings):
    # ── Google Gemini ──────────────────────────────────────────────────────
    google_api_key: str = ""

    # Model — override via GEMINI_MODEL env var
    gemini_model: str = "gemini-3.1-flash-lite-preview"

    # Rate-limit budget (free tier: 15 RPM / 1 M TPM)
    gemini_rpm: int = 15        # requests per minute
    gemini_tpm: int = 1_000_000 # tokens per minute

    # ── Browser ────────────────────────────────────────────────────────────
    browser_headless: bool = False
    browser_slow_mo: int = 100
    browser_viewport_width: int = 1280
    browser_viewport_height: int = 800

    # ── Action timing (ms) ─────────────────────────────────────────────────
    action_delay_min: int = 300
    action_delay_max: int = 800

    # ── Agent knobs ────────────────────────────────────────────────────────
    confidence_threshold: float = 0.6
    max_retries: int = 3
    max_sub_actions: int = 8   # per item; fail-fast
    max_step_seconds: int = 120  # hard timeout per item step

    # ── Auth ──────────────────────────────────────────────────────────────
    # Set DEMO_TOKEN=<secret> to require ?token=<secret> on every request.
    # Leave empty to disable auth (default for local dev).
    demo_token: str = ""  # CHANGE THIS before deployment!

    # ── Paths ─────────────────────────────────────────────────────────────
    session_dir: Path = Path("sessions")
    screenshot_dir: Path = Path("screenshots")

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = GeminiSettings()

settings.session_dir.mkdir(parents=True, exist_ok=True)
settings.screenshot_dir.mkdir(parents=True, exist_ok=True)


# ── Platform catalogue ──────────────────────────────────────────────────────

PLATFORM_URLS: dict[str, str] = {
    "blinkit": "https://blinkit.com",
    "zepto": "https://www.zeptonow.com",
}

PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "blinkit": "Blinkit",
    "zepto": "Zepto",
}

PLATFORM_FEE_BRACKETS: dict[str, list[tuple[int, int, int, int]]] = {
    "blinkit":   [(499, 0, 9, 0), (299, 25, 9, 0), (0, 49, 9, 0)],
    "zepto":     [(499, 0, 7, 0), (249, 25, 7, 0), (0, 39, 7, 0)],
}


def estimate_platform_fees(platform: str, cart_value: int):
    """Return a PlatformFees estimate for a given platform and cart value."""
    from gemini.core.models import PlatformFees
    brackets = PLATFORM_FEE_BRACKETS.get(platform, [])
    for min_cart, delivery, handling, plat_fee in brackets:
        if cart_value >= min_cart:
            return PlatformFees(delivery_fee=delivery, handling_fee=handling, platform_fee=plat_fee)
    return PlatformFees()
