"""
Multi-Platform Runner — Gemini implementation
----------------------------------------------
Runs CoreLoop instances for multiple grocery platforms in parallel and
produces a structured comparison with fee estimates and a recommendation.

"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import structlog

from gemini.config.settings import PLATFORM_URLS
from gemini.core.loop import CoreLoop
from gemini.core.models import MultiPlatformComparison, PlatformResult
from gemini.core.chat_session import ChatSession

log = structlog.get_logger()


class MultiPlatformRunner:
    """Orchestrates parallel shopping across one or more platforms."""

    def __init__(self) -> None:
        self._running_cores: list[CoreLoop] = []

    async def stop_all(self) -> None:
        """Close all open browser instances (called after user reviews carts)."""
        for core in self._running_cores:
            try:
                await core.stop()
            except Exception:
                pass
        self._running_cores.clear()

    async def run(
        self,
        platforms: list[str],
        session: ChatSession,
        extras: dict,
        progress_cb=None,   # optional async callable(platform, message)
        screenshot_cb=None, # optional callable(browser, platform) called after browser starts
    ) -> MultiPlatformComparison:
        """
        Run all requested platforms concurrently.

        Each platform gets its own CoreLoop (and therefore its own Playwright
        browser instance). TaskStep objects are freshly built per platform so
        their mutable state (selected_product, combination_plan, …) never
        bleeds across runs.
        """
        tasks = [
            self._run_one(platform, session, extras, progress_cb, screenshot_cb)
            for platform in platforms
        ]
        platform_results: list[PlatformResult] = await asyncio.gather(
            *tasks, return_exceptions=False
        )

        results_dict = {r.platform: r for r in platform_results}
        recommended, reason = self._pick_recommendation(results_dict)

        return MultiPlatformComparison(
            platforms_run=platforms,
            results=results_dict,
            recommended_platform=recommended,
            recommendation_reason=reason,
        )

    # ── internal ──────────────────────────────────────────────────────────────

    async def _run_one(
        self,
        platform: str,
        session: ChatSession,
        extras: dict,
        progress_cb,
        screenshot_cb=None,
    ) -> PlatformResult:
        """Run CoreLoop for a single platform; always returns a PlatformResult."""
        start = time.time()
        start_url = PLATFORM_URLS.get(platform, f"https://{platform}.com")

        if progress_cb:
            await progress_cb(platform, f"Opening {platform}\u2026")

        core = CoreLoop()
        self._running_cores.append(core)
        try:
            context = await core.start(platform=platform, start_url=start_url)

            # Let the server patch the browser for live screenshot streaming
            if screenshot_cb:
                screenshot_cb(core.browser, platform)
            # Take an initial screenshot so the UI shows something immediately
            await core.browser.screenshot(f"start_{platform}")

            context.task_goal = f"Add {len(session.confirmed_items)} items to {platform} cart"

            for key, value in extras.items():
                setattr(context, key, value)

            steps = session.build_task_steps(platform=platform)

            if progress_cb:
                await progress_cb(platform, f"Adding {len(steps)} items…")

            results = await core.run_task(steps, context)
            duration = time.time() - start

            summary = results.get("summary")

            # ── Fees: prefer vision-read values, fall back to estimates ─────
            import re as _re
            def _parse_inr(s: str) -> int:
                if not s:
                    return 0
                m = _re.search(r"(\d+)", s)
                return int(m.group(1)) if m else 0

            if summary:
                delivery_val  = _parse_inr(summary.delivery_charge)
                handling_val  = _parse_inr(summary.handling_charge)
                platform_val  = _parse_inr(summary.platform_fee)
                subtotal_val  = _parse_inr(summary.items_subtotal)
                grand_val     = _parse_inr(summary.grand_total)
                savings_val   = _parse_inr(summary.total_savings)
                cart_value    = subtotal_val or context.estimated_spend
                # If grand total was read by vision, use it; else estimate
                if grand_val:
                    effective_total = grand_val
                else:
                    from gemini.config.settings import estimate_platform_fees as _epf
                    est_fees = _epf(platform, cart_value)
                    effective_total = cart_value + est_fees.total_extra
            else:
                cart_value = context.estimated_spend
                from gemini.config.settings import estimate_platform_fees as _epf
                est_fees = _epf(platform, cart_value)
                delivery_val = est_fees.delivery_fee
                handling_val = est_fees.handling_fee
                platform_val = est_fees.platform_fee
                effective_total = cart_value + est_fees.total_extra

            from gemini.core.models import PlatformFees as _PF
            fees = _PF(
                delivery_fee=delivery_val,
                handling_fee=handling_val,
                platform_fee=platform_val,
            )

            # ── Item coverage map ────────────────────────────────────────────
            item_coverage: dict[str, str] = {}
            if summary:
                for it in summary.items:
                    if it.alternative_used:
                        item_coverage[it.item_name] = "substituted"
                    elif it.from_previous_session:
                        item_coverage[it.item_name] = "previous_session"
                    else:
                        item_coverage[it.item_name] = it.status

            if progress_cb:
                await progress_cb(
                    platform,
                    f"Done — {results['completed']}/{results['total']} added, "
                    f"cart ₹{cart_value}, total ₹{effective_total}",
                )

            return PlatformResult(
                platform=platform,
                summary=summary,
                fees=fees,
                cart_value=cart_value,
                effective_total=effective_total,
                duration_seconds=round(duration, 1),
                item_coverage=item_coverage,
            )

        except Exception as exc:
            log.error("platform_run_failed", platform=platform, error=str(exc))
            if progress_cb:
                await progress_cb(platform, f"Failed: {exc}")
            return PlatformResult(
                platform=platform,
                error=str(exc),
                duration_seconds=round(time.time() - start, 1),
            )
        # NOTE: do NOT call core.stop() here — browsers stay open for user review.
        # stop_all() is called by the test_chat CLI after the user presses ENTER.

    def _pick_recommendation(
        self, results: dict[str, PlatformResult]
    ) -> tuple[Optional[str], str]:
        """
        Pick the best platform to order from.

        Scoring (lower is better):
          primary   — items NOT added (missing_count): coverage first
          secondary — grand total (vision-read or estimated)
          tertiary  — substitution count (original items always better)
        """
        if not results:
            return None, ""

        ok_results = {p: r for p, r in results.items() if r.error is None and r.summary}
        if not ok_results:
            return None, "All platforms encountered errors."

        def score(r: PlatformResult) -> tuple:
            s = r.summary
            if not s:
                return (999, 999, 999)
            missing  = s.total_items_requested - s.total_items_added
            total    = r.effective_total or 9999
            subs     = sum(1 for it in s.items if it.alternative_used)
            return (missing, total, subs)

        best_platform = min(ok_results, key=lambda p: score(ok_results[p]))
        best = ok_results[best_platform]
        s = best.summary

        parts: list[str] = []
        if s:
            missing = s.total_items_requested - s.total_items_added
            if missing == 0:
                parts.append(f"All {s.total_items_requested} item(s) found")
            else:
                parts.append(f"{s.total_items_added}/{s.total_items_requested} items found")

        total_str = best.summary.grand_total or (
            f"~₹{best.effective_total}" if best.effective_total else None
        )
        if total_str:
            parts.append(f"total {total_str}")

        if s:
            nsub = sum(1 for it in s.items if it.alternative_used)
            if nsub:
                parts.append(f"{nsub} substitution(s)")

        # Show if any competitor has better pricing
        other_totals = {}
        for p, r in ok_results.items():
            if p == best_platform and r.summary:
                other_totals[p] = r.effective_total

        reason = " | ".join(parts) if parts else "best coverage and price"
        return best_platform, reason
