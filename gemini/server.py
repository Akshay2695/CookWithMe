"""
CookWithMe — FastAPI Web Server
--------------------------------
Serves the split-screen dashboard and wires it to the existing
ChatSession / CoreLoop / MultiPlatformRunner backend.

Run:
    cd <project-root>
    venv/bin/python -m gemini.server

Then open:  http://localhost:8000
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, AsyncGenerator

import structlog
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── path setup ────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from gemini.core.chat_session import ChatSession, ChatState
from gemini.core.loop import CoreLoop
from gemini.core.multi_platform import MultiPlatformRunner
from gemini.core.models import MultiPlatformComparison
from gemini.config.settings import PLATFORM_URLS, PLATFORM_DISPLAY_NAMES, settings

log = structlog.get_logger()

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="CookWithMe", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Token auth middleware ─────────────────────────────────────────────────────
# Skipped when DEMO_TOKEN is not set (local dev).  When set, every HTTP request
# and WebSocket upgrade must carry ?token=<secret>.  The dashboard URL becomes:
#   http://localhost:8000/?token=<secret>
# and the JS automatically appends the token to SSE/WS connections.

@app.middleware("http")
async def _token_auth(request: Request, call_next):
    _token = settings.demo_token
    if not _token:
        # Auth disabled — pass through
        return await call_next(request)
    # Frontend assets never need auth
    if request.url.path.startswith("/frontend") or request.url.path.startswith("/static"):
        return await call_next(request)
    provided = request.query_params.get("token", "")
    if provided != _token:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)

# ── In-memory session store ───────────────────────────────────────────────────
# One active session at a time for the MVP/demo.
# Keyed by session_id so it's trivial to extend to multi-user later.

class SessionState:
    """All mutable state for one connected user."""
    def __init__(self) -> None:
        self.session_id: str = str(uuid.uuid4())[:8]
        self.chat: ChatSession = ChatSession(platform="blinkit")
        self.events: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.last_screenshot: Optional[str] = None          # base64 PNG
        self.execution_task: Optional[asyncio.Task] = None
        self.runner: Optional[MultiPlatformRunner] = None
        self.core: Optional[CoreLoop] = None
        self.api_key: Optional[str] = None                  # per-session Gemini key
        # Login relay — used when guiding a new user through platform authentication
        self.login_input_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.login_flow_active: bool = False
        self.login_flow_platform: Optional[str] = None
        # Replay buffer: last cart_summary per platform + comparison (survives SSE reconnects)
        self.cart_summaries: dict[str, dict] = {}           # platform → event payload
        self.last_comparison: Optional[dict] = None         # latest comparison event
        self.summary_seq: int = 0                           # monotonically increasing summary id

_sessions: dict[str, SessionState] = {}
_default_session_id = "demo"


def _get_or_create_session(sid: str = _default_session_id) -> SessionState:
    if sid not in _sessions:
        s = SessionState()
        s.session_id = sid
        _sessions[sid] = s
    return _sessions[sid]


# ── Screenshot broadcaster ────────────────────────────────────────────────────
# Monkey-patch BrowserManager.screenshot to also push to the event queue.

_original_browser_screenshot = None

def _patch_browser_for_session(browser, state: SessionState):
    """Wrap browser.screenshot so every capture is pushed to the SSE stream."""
    import types

    orig = browser.screenshot

    async def _patched(label: str = "") -> str:
        b64 = await orig(label)
        state.last_screenshot = b64
        await _push_event(state, "screenshot", {"data": b64, "label": label})
        return b64

    browser.screenshot = _patched


# ── Event helpers ──────────────────────────────────────────────────────────────

async def _push_event(state: SessionState, event_type: str, payload: dict) -> None:
    """Non-blocking push; drops oldest if queue is full."""
    item = {"type": event_type, "ts": time.time(), **payload}
    try:
        state.events.put_nowait(item)
    except asyncio.QueueFull:
        try:
            state.events.get_nowait()
            state.events.put_nowait(item)
        except Exception:
            pass


async def _push_chat(state: SessionState, role: str, message: str,
                     items=None, chat_state: str = "") -> None:
    payload: dict = {"role": role, "message": message, "chat_state": chat_state}
    if items:
        payload["items"] = [
            {"name": i.name, "quantity": i.quantity,
             "source_recipe": i.source_recipe, "notes": i.notes}
            for i in items
        ]
    await _push_event(state, "chat", payload)


async def _push_status(state: SessionState, item_name: str, status: str,
                       detail: str = "") -> None:
    await _push_event(state, "item_status",
                      {"item": item_name, "status": status, "detail": detail})


async def _push_comparison(state: SessionState,
                            comparison: MultiPlatformComparison) -> None:
    """Serialize comparison into a JSON-safe dict for the frontend."""
    platforms = comparison.platforms_run

    def _result_dict(r):
        if not r:
            return {"error": "no result"}
        out = {
            "platform": r.platform,
            "error": r.error,
            "duration": round(r.duration_seconds, 1),
            "grand_total": "",
            "delivery": "",
            "savings": "",
            "coverage": "",
            "items": [],
        }
        if r.summary:
            s = r.summary
            out["grand_total"] = s.grand_total or s.estimated_total or ""
            out["delivery"] = s.delivery_charge or ""
            out["savings"] = s.total_savings or ""
            out["coverage"] = f"{s.total_items_added}/{s.total_items_requested}"
            out["items"] = [
                {
                    "name": i.item_name,
                    "status": i.status,
                    "product": i.product_selected or "",
                    "price": i.total_price or i.unit_price or "",
                    "qty": i.pack_description or i.quantity_added or "",
                    "alt": i.alternative_used,
                    "alt_reason": i.alternative_reason,
                    "qty_note": i.quantity_note or "",
                    "from_prev": i.from_previous_session,
                }
                for i in s.items
            ]
        return out

    payload = {
        "type": "comparison",
        "platforms": platforms,
        "recommended": comparison.recommended_platform or "",
        "reason": comparison.recommendation_reason,
        "results": {p: _result_dict(comparison.results.get(p)) for p in platforms},
    }
    state.last_comparison = payload   # keep for SSE replay on reconnect
    await _push_event(state, "comparison", {
        "platforms": platforms,
        "recommended": comparison.recommended_platform or "",
        "reason": comparison.recommendation_reason,
        "results": {p: _result_dict(comparison.results.get(p)) for p in platforms},
    })


async def _push_cart_summary(state: SessionState, summary, platform: str) -> None:
    if not summary:
        return
    state.summary_seq += 1
    items_out = [
        {
            "name": i.item_name,
            "status": i.status,
            "product": i.product_selected or "",
            "price": i.total_price or i.unit_price or "",
            "unit_price": i.unit_price or "",
            "mrp": i.mrp or "",
            "qty": i.pack_description or i.quantity_added or "",
            "qty_added": i.quantity_added or "",
            "pack_description": i.pack_description or "",
            "qty_requested": i.quantity_requested or "",
            "qty_note": i.quantity_note or "",
            "alt": i.alternative_used,
            "alt_reason": i.alternative_reason,
            "from_prev": i.from_previous_session,
            "savings": i.savings or "",
        }
        for i in summary.items
    ]
    payload = {
        "summary_id": state.summary_seq,
        "platform": platform,
        "items": items_out,
        "grand_total": summary.grand_total or summary.estimated_total or "",
        "estimated_total": summary.estimated_total or "",
        "delivery": summary.delivery_charge or "",
        "handling": summary.handling_charge or "",
        "platform_fee": summary.platform_fee or "",
        "savings": summary.total_savings or "",
        "subtotal": summary.items_subtotal or "",
        "duration": summary.duration_seconds,
        "requested": summary.total_items_requested,
        "added": summary.total_items_added,
        "skipped": summary.total_items_skipped,
        "failed": summary.total_items_failed,
        "items_this_session": summary.items_this_session,
        # Serviceability + delivery
        "is_serviceable": summary.is_serviceable,
        "delivery_time": summary.delivery_time or "",
        "late_night_fee": summary.late_night_fee or "",
        "surge_charge": summary.surge_charge or "",
    }
    # Keep in replay buffer so SSE reconnects can re-deliver it
    state.cart_summaries[platform] = payload
    await _push_event(state, "cart_summary", payload)


# ── Platform connection status ───────────────────────────────────────────────────

@app.get("/platform-status")
async def platform_status_endpoint(sid: str = _default_session_id):
    """
    Returns whether each platform has a saved session file.
    Used by the UI to show connection status on load.
    """
    state = _get_or_create_session(sid)
    result: dict = {}
    for p in ["blinkit", "zepto"]:
        sf = settings.session_dir / f"{p}_session.json"
        if sf.exists():
            age_h = round((time.time() - sf.stat().st_mtime) / 3600, 1)
            result[p] = {"connected": True, "age_hours": age_h}
        else:
            result[p] = {"connected": False, "age_hours": None}
    return result


# ── New-user platform login (browser-watch flow) ────────────────────────────────


async def _run_connect(state: SessionState, platform: str) -> None:
    """
    Open a browser, wait for the user to log in manually, then save the
    session when they type 'done'.  No auto-detection — the browser stays
    open until the user explicitly confirms.
    """
    from playwright.async_api import async_playwright

    plat_label = PLATFORM_DISPLAY_NAMES.get(platform, platform.title())
    url        = PLATFORM_URLS.get(platform, f"https://{platform}.com")
    sf         = settings.session_dir / f"{platform}_session.json"

    state.login_flow_active   = True
    state.login_flow_platform = platform

    pw = None
    browser_obj = None
    try:
        pw      = await async_playwright().start()
        browser_obj = await pw.chromium.launch(
            headless=settings.browser_headless,
            slow_mo=settings.browser_slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        ctx = await browser_obj.new_context(
            viewport={"width": settings.browser_viewport_width,
                      "height": settings.browser_viewport_height},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        async def _shot(label: str = "login") -> None:
            path = settings.screenshot_dir / f"{label}_{int(time.time())}.png"
            await page.screenshot(path=str(path))
            b64 = base64.b64encode(path.read_bytes()).decode()
            state.last_screenshot = b64
            await _push_event(state, "screenshot", {"data": b64, "label": label})

        # Navigate to platform
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)
        await _shot(f"{platform}_opened")

        await _push_chat(
            state, "assistant",
            f"I've opened **{plat_label}** in the browser on the left.\n\n"
            f"**Please log in** there using any method you prefer "
            f"(phone, Google, etc.).\n\n"
            f"Once you're on the homepage, type **done** here and I'll save your session.",
            chat_state="login_waiting",
        )

        # ── Wait indefinitely for the user to type "done" ───────────────────
        # 10-minute hard timeout to avoid hanging forever if user forgets.
        user_input = await asyncio.wait_for(
            state.login_input_queue.get(), timeout=600
        )
        log.info("login_confirmed_by_user", platform=platform,
                 input=user_input[:40])

        # ── Take final screenshot then save session ─────────────────────────
        await asyncio.sleep(1)
        await _shot(f"{platform}_post_login")
        await ctx.storage_state(path=str(sf))
        log.info("session_saved_browser_login", platform=platform, path=str(sf))

        await _push_event(state, "platform_status",
                          {"platform": platform, "connected": True})
        await _push_chat(
            state, "assistant",
            f"\u2705 **{plat_label} connected!** Your session is saved.\n\n"
            "You can now tell me what to shop for.",
            chat_state="idle",
        )

    except asyncio.TimeoutError:
        await _push_chat(state, "assistant",
                         "Login timed out (10 minutes). Please try again.",
                         chat_state="idle")
    except asyncio.CancelledError:
        await _push_chat(state, "assistant", "Login cancelled.", chat_state="idle")
    except Exception as exc:
        log.error("connect_error", platform=platform, error=str(exc))
        await _push_chat(state, "assistant",
                         f"Login failed: {exc}. Please try again.", chat_state="error")
    finally:
        state.login_flow_active   = False
        state.login_flow_platform = None
        state.execution_task      = None
        try:
            if browser_obj: await browser_obj.close()
            if pw:          await pw.stop()
        except Exception:
            pass


# ── Execution helpers ───────────────────────────────────────────────────────────

async def _run_single(state: SessionState, platform: str) -> None:
    """Execute shopping on a single platform."""
    start_url = PLATFORM_URLS.get(platform, f"https://{platform}.com")
    plat_label = PLATFORM_DISPLAY_NAMES.get(platform, platform.title())

    await _push_chat(state, "assistant",
                     f"Opening {plat_label}… watch the browser on the left 👀",
                     chat_state="shopping")

    core = CoreLoop()
    state.core = core

    # Inject API key if user provided one
    if state.api_key:
        os.environ["GOOGLE_API_KEY"] = state.api_key

    try:
        context = await core.start(platform=platform, start_url=start_url)
        _patch_browser_for_session(core.browser, state)

        # Re-take a screenshot now that patching is active
        await core.browser.screenshot(f"patched_start_{platform}")

        extras = state.chat.build_agent_context_extras()
        for key, value in extras.items():
            setattr(context, key, value)

        if context.existing_cart_items:
            await _push_chat(state, "assistant",
                             f"Found {len(context.existing_cart_items)} items already in your cart — skipping those.",
                             chat_state="shopping")
            state.chat.existing_cart_items = context.existing_cart_items

        steps = state.chat.build_task_steps(platform=platform)

        # Wire up per-step status updates
        original_execute = core._execute_step

        async def _tracked_execute(step, ctx):
            await _push_status(state, step.item_name, "searching")
            result = await original_execute(step, ctx)
            status = step.status
            if status == "done":
                await _push_status(state, step.item_name, "added",
                                   step.product_selected or "")
            elif status == "skipped":
                await _push_status(state, step.item_name, "skipped")
            else:
                await _push_status(state, step.item_name, "failed")
            return result

        core._execute_step = _tracked_execute

        results = await core.run_task(steps, context)
        summary = results.get("summary")

        added = results["completed"]
        total = results["total"]
        cart  = results["cart_count"]

        total_str = f" · {summary.grand_total}" if summary and summary.grand_total else ""
        skipped_str = (
            f" ({results.get('skipped', 0)} skipped)"
            if results.get("skipped", 0) else ""
        )
        detail_lines: list[str] = []
        if summary and summary.items:
            for it in summary.items:
                status_icon = {"added": "✅", "skipped": "⏭", "failed": "❌"}.get(it.status, "•")
                qty_part = f" · {it.pack_description or it.quantity_added}" if (it.pack_description or it.quantity_added) else ""
                product_part = f" ({it.product_selected})" if it.product_selected else ""
                price_part = f" — {it.total_price or it.unit_price}" if (it.total_price or it.unit_price) else ""
                detail_lines.append(f"{status_icon} {it.item_name}{product_part}{qty_part}{price_part}")

        bill_lines: list[str] = []
        if summary:
            if summary.items_subtotal:
                bill_lines.append(f"Subtotal: {summary.items_subtotal}")
            if summary.delivery_charge:
                bill_lines.append(f"Delivery: {summary.delivery_charge}")
            if summary.handling_charge:
                bill_lines.append(f"Handling: {summary.handling_charge}")
            if summary.platform_fee:
                bill_lines.append(f"Platform fee: {summary.platform_fee}")
            if summary.total_savings:
                bill_lines.append(f"Savings: {summary.total_savings}")
            if summary.grand_total or summary.estimated_total:
                bill_lines.append(f"Grand total: {summary.grand_total or summary.estimated_total}")

        details_block = "\n".join(detail_lines[:12])
        bill_block = "\n".join(bill_lines)
        msg_parts = [
            f"✅ Added **{added}/{total}** items to {plat_label}{skipped_str}{total_str}.",
            "**Detailed summary:**",
        ]
        if details_block:
            msg_parts.append(details_block)
        else:
            msg_parts.append("No item-level details were extracted.")
        if bill_block:
            msg_parts.extend(["", "**Bill details:**", bill_block])
        msg_parts.append("")
        msg_parts.append("The visual cart summary card is below. Want to **add more items** or are you done?")
        msg = "\n".join(msg_parts)

        # Build a plain-text summary the LLM can use for post-shopping Q&A
        summary_lines = [f"Shopping on {plat_label}: {added}/{total} items added, cart has {cart} item(s)."]
        if summary and summary.grand_total:
            summary_lines.append(f"Grand total: {summary.grand_total}")
        if summary and summary.items:
            for it in summary.items:
                status_icon = {"added": "✅", "skipped": "⏭", "failed": "❌"}.get(it.status, "?")
                price_str = f" — {it.total_price or it.unit_price}" if (it.total_price or it.unit_price) else ""
                product_str = f" ({it.product_selected})" if it.product_selected else ""
                summary_lines.append(f"  {status_icon} {it.item_name}{product_str}{price_str}")
        state.chat.set_shopping_complete("\n".join(summary_lines))

        # Push done message FIRST, then cart summary — this way the cart card
        # is the last thing appended and stays visible at the bottom.
        await _push_chat(state, "assistant", msg, chat_state="done")
        await _push_cart_summary(state, summary, platform)

    except asyncio.CancelledError:
        await _push_chat(state, "assistant",
                         "Shopping was cancelled.", chat_state="idle")
    except Exception as exc:
        log.error("execute_single_error", error=str(exc))
        await _push_chat(state, "assistant",
                         f"Something went wrong: {exc}", chat_state="error")
    finally:
        state.core = None
        state.execution_task = None


async def _run_multi(state: SessionState, platforms: list[str]) -> None:
    """Execute shopping on multiple platforms, then push comparison."""
    plat_labels = " + ".join(PLATFORM_DISPLAY_NAMES.get(p, p.title()) for p in platforms)

    await _push_chat(state, "assistant",
                     f"Starting recon on **{plat_labels}**… this takes about 90 seconds.",
                     chat_state="shopping")

    if state.api_key:
        os.environ["GOOGLE_API_KEY"] = state.api_key

    async def _progress(platform: str, message: str) -> None:
        pname = PLATFORM_DISPLAY_NAMES.get(platform, platform.title())
        await _push_event(state, "platform_progress",
                          {"platform": platform, "message": f"[{pname}] {message}"})

    runner = MultiPlatformRunner()
    state.runner = runner

    try:
        # Patch each platform's browser so screenshots stream to the UI in real time
        def _patch_cb(browser, platform: str) -> None:
            _patch_browser_for_session(browser, state)

        comparison = await runner.run(
            platforms=platforms,
            session=state.chat,
            extras=state.chat.build_agent_context_extras(),
            progress_cb=_progress,
            screenshot_cb=_patch_cb,
        )

        rec = comparison.recommended_platform
        rec_name = PLATFORM_DISPLAY_NAMES.get(rec, rec.title()) if rec else "—"
        detail_blocks: list[str] = [
            f"✅ **All platforms scanned. Recommendation: Order on {rec_name}.**",
            f"Reason: {comparison.recommendation_reason}",
            "",
            "**Detailed multi-platform summary:**",
        ]

        for p in platforms:
            r = comparison.results.get(p)
            pn = PLATFORM_DISPLAY_NAMES.get(p, p.title())
            if not r:
                detail_blocks.append(f"\n**{pn}:** no result")
                continue
            if r.error:
                detail_blocks.append(f"\n**{pn}:** ❌ {r.error}")
                continue
            s = r.summary
            if not s:
                detail_blocks.append(f"\n**{pn}:** no summary available")
                continue

            top_line = f"\n**{pn}:** {s.total_items_added}/{s.total_items_requested} added"
            if s.grand_total or s.estimated_total:
                top_line += f" · total {s.grand_total or s.estimated_total}"
            detail_blocks.append(top_line)

            # Keep chat readable: include at most 8 item lines per platform.
            for it in s.items[:8]:
                status_icon = {"added": "✅", "skipped": "⏭", "failed": "❌"}.get(it.status, "•")
                qty_part = f" · {it.pack_description or it.quantity_added}" if (it.pack_description or it.quantity_added) else ""
                product_part = f" ({it.product_selected})" if it.product_selected else ""
                price_part = f" — {it.total_price or it.unit_price}" if (it.total_price or it.unit_price) else ""
                detail_blocks.append(f"{status_icon} {it.item_name}{product_part}{qty_part}{price_part}")
            if len(s.items) > 8:
                detail_blocks.append(f"… and {len(s.items) - 8} more item(s)")

            bill_bits: list[str] = []
            if s.items_subtotal:
                bill_bits.append(f"Subtotal {s.items_subtotal}")
            if s.delivery_charge:
                bill_bits.append(f"Delivery {s.delivery_charge}")
            if s.handling_charge:
                bill_bits.append(f"Handling {s.handling_charge}")
            if s.platform_fee:
                bill_bits.append(f"Platform fee {s.platform_fee}")
            if s.total_savings:
                bill_bits.append(f"Savings {s.total_savings}")
            if bill_bits:
                detail_blocks.append("Bill: " + " · ".join(bill_bits))

        detail_blocks.extend([
            "",
            "Want to **add more items** or are you done?",
        ])
        msg = "\n".join(detail_blocks)

        # Build summary text for post-shopping Q&A
        summary_lines = [f"Multi-platform shopping complete. Recommended: {rec_name}. {comparison.recommendation_reason}"]
        for p, r in comparison.results.items():
            if r and r.summary:
                pn = PLATFORM_DISPLAY_NAMES.get(p, p.title())
                s = r.summary
                summary_lines.append(f"  {pn}: {s.total_items_added}/{s.total_items_requested} items, total {s.grand_total or s.estimated_total}")
        state.chat.set_shopping_complete("\n".join(summary_lines))

        # Done message first, then per-platform summaries, then comparison card
        # — this ensures the comparison (recommended platform) is the last thing
        # visible and naturally at the bottom of the chat.
        await _push_chat(state, "assistant", msg, chat_state="done")
        for p in platforms:
            result = comparison.results.get(p)
            if result and result.summary:
                await _push_cart_summary(state, result.summary, p)
        await _push_comparison(state, comparison)

    except asyncio.CancelledError:
        await _push_chat(state, "assistant",
                         "Multi-platform run was cancelled.", chat_state="idle")
    except Exception as exc:
        log.error("execute_multi_error", error=str(exc))
        await _push_chat(state, "assistant",
                         f"Something went wrong: {exc}", chat_state="error")
    finally:
        state.runner = None


# ── SSE endpoint ──────────────────────────────────────────────────────────────

@app.get("/events")
async def sse_events(sid: str = _default_session_id):
    """
    Server-Sent Events stream — pushes JSON lines to the browser dashboard.
    Event types: chat | screenshot | item_status | comparison | cart_summary |
                 platform_progress | ping
    """
    state = _get_or_create_session(sid)

    async def _stream() -> AsyncGenerator[str, None]:
        # Immediate ping confirms the connection is alive to the browser
        yield f"data: {json.dumps({'type': 'ping', 'ts': time.time()})}\n\n"

        # Re-send any stored cart summaries first (survives reconnects)
        for platform, payload in list(state.cart_summaries.items()):
            item = {"type": "cart_summary", "ts": time.time(), **payload}
            yield f"data: {json.dumps(item)}\n\n"

        # Re-send last comparison (survives reconnects)
        if state.last_comparison:
            item = {**state.last_comparison, "ts": time.time()}
            yield f"data: {json.dumps(item)}\n\n"

        # Send any buffered events next (e.g. reconnect after brief disconnect)
        while not state.events.empty():
            try:
                item = state.events.get_nowait()
                yield f"data: {json.dumps(item)}\n\n"
            except asyncio.QueueEmpty:
                break

        # Then stream live events
        while True:
            try:
                item = await asyncio.wait_for(state.events.get(), timeout=20.0)
                yield f"data: {json.dumps(item)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping
                yield f"data: {json.dumps({'type': 'ping', 'ts': time.time()})}\n\n"
            except asyncio.CancelledError:
                break
            except Exception:
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ── WebSocket chat endpoint ────────────────────────────────────────────────────

@app.websocket("/chat")
async def ws_chat(websocket: WebSocket, sid: str = _default_session_id):
    """
    Bidirectional WebSocket for the chat interface.

    Incoming message types (from browser):
      { "type": "message",  "text": "..." }
      { "type": "platform", "platforms": ["blinkit", "zepto"] }
      { "type": "key",      "key": "AIza..." }
      { "type": "restart" }
      { "type": "cancel" }

    All agent responses are pushed via SSE, not WS, to decouple
    the slow execution path from the fast chat path.
    """
    await websocket.accept()
    state = _get_or_create_session(sid)

    # Push current platform connection status immediately on connect
    for p in ["blinkit", "zepto"]:
        sf = settings.session_dir / f"{p}_session.json"
        await _push_event(state, "platform_status",
                          {"platform": p, "connected": sf.exists()})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"type": "message", "text": raw}

            mtype = msg.get("type", "message")

            # ── API key ────────────────────────────────────────────────────
            if mtype == "key":
                state.api_key = msg.get("key", "").strip()
                await websocket.send_text(json.dumps({"ok": True, "type": "key_set"}))
                continue

            # ── Cancel running task ────────────────────────────────────────
            if mtype == "cancel":
                if state.execution_task and not state.execution_task.done():
                    state.execution_task.cancel()
                    await _push_chat(state, "assistant",
                                     "Cancelling… browser will close shortly.",
                                     chat_state="idle")
                continue

            # ── Restart session ────────────────────────────────────────────
            if mtype == "restart":
                if state.execution_task and not state.execution_task.done():
                    state.execution_task.cancel()
                state.chat = ChatSession(platform="blinkit")
                state.login_flow_active = False
                state.cart_summaries = {}          # clear replay buffers on restart
                state.last_comparison = None
                await _push_chat(state, "assistant",
                                 "Session restarted! What would you like to cook or buy?",
                                 chat_state="idle")
                continue

            # ── Add more items (keep cart context, reset to IDLE for new input)
            if mtype == "add_more":
                # Keep only the pre-session existing_cart_items (items that were
                # in the physical cart BEFORE this conversation started).
                # Do NOT carry forward confirmed_items — those may have failed to
                # add and would block the user from requesting the same items again.
                # The DOM read in CoreLoop.start() will detect what's actually in
                # the cart and set context.existing_cart_items correctly at runtime.
                existing = list(state.chat.existing_cart_items or [])
                platform = state.chat.platform
                new_chat = ChatSession(platform=platform)
                new_chat.existing_cart_items = existing
                # Skip the per-session pref review — user already saw it
                new_chat._pref_review_done = True
                state.chat = new_chat
                await _push_chat(state, "assistant",
                                 "What else would you like to add?",
                                 chat_state="idle")
                continue

            # ── Connect a platform (new-user login flow) ───────────────────
            if mtype == "connect_platform":
                platform = msg.get("platform", "blinkit")
                if platform not in ("blinkit", "zepto"):
                    await websocket.send_text(json.dumps(
                        {"ok": False, "error": "Unsupported platform. Use blinkit or zepto."}
                    ))
                    continue
                if state.execution_task and not state.execution_task.done():
                    await websocket.send_text(json.dumps(
                        {"ok": False, "error": "Finish or cancel the current task first"}
                    ))
                    continue
                state.execution_task = asyncio.create_task(
                    _run_connect(state, platform)
                )
                await websocket.send_text(json.dumps(
                    {"ok": True, "type": "connect_started", "platform": platform}
                ))
                continue

            # ── Platform selection (user clicked a platform button) ─────────
            if mtype == "platform":
                selected: list[str] = msg.get("platforms", ["blinkit"])
                if state.execution_task and not state.execution_task.done():
                    await websocket.send_text(json.dumps({
                        "ok": False, "error": "Already running"}))
                    continue
                if not state.chat.confirmed_items:
                    await _push_chat(state, "assistant",
                                     "No items confirmed yet. Please tell me what to buy first.",
                                     chat_state="idle")
                    continue
                # Keep multi-platform scope intentionally narrow for reliability.
                if len(selected) > 1:
                    allowed_multi = [p for p in selected if p in ("blinkit", "zepto")]
                    # Preserve order while removing duplicates
                    allowed_multi = list(dict.fromkeys(allowed_multi))
                    if len(allowed_multi) < 2:
                        allowed_multi = ["blinkit", "zepto"]
                    selected = allowed_multi[:2]
                if len(selected) == 1:
                    state.execution_task = asyncio.create_task(
                        _run_single(state, selected[0])
                    )
                else:
                    state.execution_task = asyncio.create_task(
                        _run_multi(state, selected)
                    )
                await websocket.send_text(json.dumps({"ok": True, "type": "execution_started"}))
                continue

            # ── Chat message ───────────────────────────────────────────────
            text = msg.get("text", "").strip()
            if not text:
                continue

            # If a login flow is waiting for input, relay text to it
            if state.login_flow_active:
                try:
                    state.login_input_queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass  # previous input still pending
                await _push_chat(state, "user", text)
                continue

            # Echo user message to SSE stream so the dashboard renders it
            await _push_chat(state, "user", text)

            # Parse through ChatSession
            response = await state.chat.handle_message(text)

            await _push_chat(
                state, "assistant", response.message,
                items=response.items if response.state == ChatState.CONFIRMING else None,
                chat_state=response.state.value,
            )

    except WebSocketDisconnect:
        log.info("ws_disconnected", sid=sid)
    except Exception as exc:
        log.error("ws_error", sid=sid, error=str(exc))


# ── Screenshot endpoint (polling fallback) ────────────────────────────────────

@app.get("/screenshot")
async def get_screenshot(sid: str = _default_session_id):
    """Latest browser screenshot as base64 JSON — for SSE-less clients."""
    state = _get_or_create_session(sid)
    return {"data": state.last_screenshot or "", "ts": time.time()}


# ── HTML dashboard ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "frontend" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>index.html not found — place it in gemini/frontend/</h1>")


# ── Frontend files (UI assets) ────────────────────────────────────────────────

_frontend_dir = Path(__file__).parent / "frontend"
_frontend_dir.mkdir(exist_ok=True)

try:
    # Primary route after static -> frontend rename.
    app.mount("/frontend", StaticFiles(directory=str(_frontend_dir)), name="frontend")
    # Backward-compatible alias so older links still work.
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
except Exception:
    pass  # empty frontend dir is fine


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CookWithMe web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n🍳  CookWithMe server starting on http://{args.host}:{args.port}\n")
    uvicorn.run(
        "gemini.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
