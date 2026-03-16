"""
CoreLoop — Gemini implementation
----------------------------------
Vision-first orchestration: Gemini vision locates UI elements by looking at
screenshots and returns pixel coordinates the browser clicks directly.
DOM interaction is used only as a last-resort fallback.

Execution flow per item
-----------------------
1.  DOM : Idempotency check (session list + cart badge)          (0 LLM calls)
2.  VISION: screenshot → FusedVisionAgent.locate_search_bar()   (1 LLM call)
            → returns (x, y) of the search bar
3.  BROWSER: click at (x,y), type item name, press Enter         (0 LLM calls)
      └─ fallback: DOM selector approach if vision fails
4.  Wait for search results to load                              (0 LLM calls)
5.  VISION: screenshot → FusedVisionAgent.analyse()             (1 LLM call)
            → selected product + add_button (x,y) + combination plan
6.  BROWSER: click at vision-provided ADD button (x,y)           (0 LLM calls)
      └─ fallback: DOM selector ADD button search
7.  DOM : Verify cart badge incremented                          (0 LLM calls)
8.  If unavailable → SubstitutionAgent then retry               (~1 LLM call)

Total LLM calls per item: 2 (happy path) or ~3 (substitution needed)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

import structlog

from gemini.agents.fused_vision import FusedVisionAgent, VerifyAddResult, CartAnalysis
from gemini.agents.substitution import SubstitutionAgent
from gemini.config.settings import settings
from gemini.core.browser import BrowserManager
from gemini.core.models import (
    AgentContext,
    CartItemSummary,
    CartSummary,
    TaskStep,
)

log = structlog.get_logger()

# ── Category-mismatch guard ───────────────────────────────────────────────────
# Prevents the vision agent from selecting a packaged snack product when the
# user asked for fresh produce, spices, dairy, or other grocery staples.
# Keywords in product names that indicate it is a packaged snack/junk food:
_SNACK_PRODUCT_TERMS: frozenset[str] = frozenset({
    "chips", "crisps", "veggie stix", "veggie sticks", "stix",
    "namkeen", "popcorn", "kurkure", "lays", "bingo", "uncle chipps",
    "too yumm", "haldirams snack", "bikano", "diamond", "act ii",
    "puff", "puffs", "rings", "wafer", "wafers",
    "bhujia", "mixture", "chivda",  # savory snacks
    "biscuit", "biscuits", "cookie", "cookies", "cracker", "crackers",  # baked snacks
})
# Keywords in the REQUESTED item name that indicate a snack is expected:
_SNACK_REQUEST_TERMS: frozenset[str] = frozenset({
    "chips", "crisps", "namkeen", "snack", "snacks", "biscuit", "biscuits",
    "cookie", "cookies", "crackers", "popcorn", "kurkure", "wafer", "wafers",
    "puff", "puffs", "bhujia", "chivda", "mixture", "namkin",
})


def _is_snack_product(product_name: str) -> bool:
    pl = product_name.lower()
    return any(term in pl for term in _SNACK_PRODUCT_TERMS)


def _is_snack_request(item_name: str) -> bool:
    il = item_name.lower()
    return any(term in il for term in _SNACK_REQUEST_TERMS)


class CoreLoop:
    """
    DOM-first execution loop.  Calls the Gemini vision API only once per item
    to pick the right product — everything else uses Playwright DOM APIs.
    """

    def __init__(self) -> None:
        self.browser = BrowserManager()
        self._vision = FusedVisionAgent()
        self._substitution = SubstitutionAgent()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, platform: str, start_url: str) -> AgentContext:
        page = await self.browser.start(platform=platform)
        await page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
        # Extra settle time — SPAs keep rendering after domcontentloaded
        await asyncio.sleep(2.5)
        await self.browser.wait_for_stable(timeout_ms=5000)
        context = AgentContext(
            session_id=str(uuid.uuid4())[:8],
            platform=platform,
            current_url=start_url,
        )
        # Read what’s already in the cart from a previous session so we can skip
        # those items or intelligently increment them.
        existing = await self.browser.read_cart_items(platform)
        context.existing_cart_items = existing
        if existing:
            log.info("existing_cart_detected", count=len(existing), items=existing[:8])
        log.info("core_loop_started", platform=platform)
        return context

    async def stop(self) -> None:
        await self.browser.stop()

    async def _read_cart_count(self, platform: str) -> int:
        if platform == "zepto":
            return await self.browser.get_cart_count_strict(platform)
        return await self.browser.get_cart_count()

    def _to_viewport_px(
        self,
        value: Optional[float],
        axis_size: int,
    ) -> Optional[int]:
        """
        Convert model-provided coordinate to viewport pixels.

        Handles three observed formats:
        1) normalized (0..1) -> multiply by axis
        2) absolute px (1..axis_size) -> use as-is
        3) accidentally pre-scaled (e.g., 780*800=624000) -> divide once by axis
        """
        if value is None:
            return None
        v = float(value)
        if v <= 1.5:
            px = v * axis_size
        elif v <= axis_size:
            px = v
        elif v <= axis_size * axis_size:
            # Most likely absolute px multiplied one extra time by axis_size
            px = v / axis_size
        else:
            # Last-resort clamp for pathological values
            px = axis_size - 1
        # Keep clicks inside viewport bounds
        return max(1, min(axis_size - 1, int(px)))

    def _analysis_add_coords(self, analysis) -> tuple[Optional[int], Optional[int]]:
        x = self._to_viewport_px(analysis.add_button_x_norm, settings.browser_viewport_width)
        y = self._to_viewport_px(analysis.add_button_y_norm, settings.browser_viewport_height)
        return x, y

    def _verify_matches_target(self, observed: str, target_name: str) -> bool:
        """
        Guard against counting a different variant as success.

        Example failure to block:
        target="Onion" but observed says "Onion White".
        """
        import re as _re

        obs = (observed or "").strip()
        tgt = (target_name or "").strip()
        if not obs or not tgt:
            return True

        # Try extracting the concrete product mentioned by verifier.
        m = _re.search(r"for\s+'([^']+)'", obs, flags=_re.IGNORECASE)
        if not m:
            m = _re.search(r"for\s+([A-Za-z0-9()\-\s|,&]+?)\s+(?:changed|remains|is|was)", obs, flags=_re.IGNORECASE)
        observed_name = (m.group(1).strip() if m else "")
        if not observed_name:
            return True

        def _norm(s: str) -> str:
            s = s.lower()
            s = _re.sub(r"[^a-z0-9\s]", " ", s)
            s = _re.sub(r"\s+", " ", s).strip()
            return s

        on = _norm(observed_name)
        tn = _norm(tgt)
        if not on or not tn:
            return True
        if on == tn:
            return True

        t_words = [w for w in tn.split() if len(w) > 2]
        o_words = [w for w in on.split() if len(w) > 2]

        # If target is generic/single-word, require exact match to avoid
        # accepting sibling variants (e.g. onion vs onion white).
        if len(t_words) <= 1:
            return on == tn

        overlap = len(set(t_words) & set(o_words))
        return overlap >= min(2, len(t_words))

    # ── task execution ────────────────────────────────────────────────────────

    async def run_task(
        self,
        steps: list[TaskStep],
        context: AgentContext,
    ) -> dict:
        t0 = time.time()
        results = {
            "total": len(steps),
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        }

        for step in steps:
            try:
                await asyncio.wait_for(
                    self._execute_step(step, context),
                    timeout=settings.max_step_seconds,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "step_timeout",
                    item=step.item_name,
                    timeout_s=settings.max_step_seconds,
                    platform=context.platform,
                )
                step.status = "failed"
            if step.status == "done":
                results["completed"] += 1
            elif step.status == "skipped":
                results["skipped"] += 1
            else:
                results["failed"] += 1

        # Reconcile cart count from DOM
        dom_count = await self.browser.get_cart_count()
        if dom_count > 0:
            context.cart_count = dom_count

        results["cart_count"] = context.cart_count
        results["estimated_spend"] = context.estimated_spend
        results["substitutions"] = context.substitutions_made

        # Navigate to cart, then scroll through it entirely so every item is
        # captured before building the ground-truth summary.
        cart_vision: Optional[CartAnalysis] = None
        if results["completed"] > 0 or results["cart_count"] > 0:
            try:
                await self.browser.navigate_to_cart(context.platform)
                cart_wait_s = {
                    "blinkit": 3.5,
                    "zepto": 1.2,
                }.get(context.platform, 2.0)
                await asyncio.sleep(cart_wait_s)
                await self.browser.wait_for_stable(timeout_ms=2500)
                cart_vision = await self._capture_full_cart()
            except Exception as _cart_err:
                log.warning("cart_vision_skipped", error=str(_cart_err))

        results["summary"] = _build_summary(
            steps, context, time.time() - t0, cart_vision
        )

        await self.browser.save_session(context.platform)
        log.info("task_complete", **{k: v for k, v in results.items() if k != "summary"})
        return results

    # ── full-cart capture (scroll + merge) ───────────────────────────────────

    async def _capture_full_cart(self) -> CartAnalysis:
        """
        Vision-driven cart capture with gradual, overlapping scrolls.

        Scroll step = viewport_h // 2 (50 % overlap between consecutive
        screenshots) so no cart item can slip through the gap even when
        the DOM renders a beat late.

        Termination conditions (any one stops the loop early):
          • vision reports has_more_items_below = False
          • 2 consecutive rounds with 0 new items (truly stuck)
          • scroll_y exceeds the container's scrollHeight (hit bottom)
          • MAX_SCROLLS safety cap

        Bill details (subtotal, fees, grand total) are tracked from EVERY
        screenshot and the richest one wins — so we get fee data even if the
        "Bill Details" panel is only visible mid-scroll.
        """
        # Half-viewport step guarantees ~50 % overlap so no item is skipped
        viewport_h: int = settings.browser_viewport_height  # typically 800
        SCROLL_STEP  = viewport_h // 2                       # 400 px
        MAX_SCROLLS  = 20   # raised because steps are smaller now

        # ── Reset scroll position and wait for cart to fully paint ───────────
        await self.browser.scroll_cart_container(0)
        await asyncio.sleep(1.5)   # was 0.5 — lazy content needs room to render

        all_items: dict[str, CartLineItem] = {}
        # Best bill details seen so far across ALL screenshots
        best_bill: Optional[CartAnalysis] = None
        last_result: Optional[CartAnalysis] = None
        screenshot_index  = 0
        scroll_y          = 0
        zero_new_streak   = 0   # consecutive rounds with no new items

        for attempt in range(MAX_SCROLLS):
            shot = await self.browser.screenshot(f"cart_scroll_{screenshot_index}")
            screenshot_index += 1

            try:
                result = await self._vision.analyse_cart(shot)
            except Exception as exc:
                log.warning("cart_scroll_vision_failed",
                            attempt=attempt, error=str(exc))
                break

            # ── merge newly-visible items ─────────────────────────────────
            new_this_round = 0
            for item in result.items:
                key = item.product_name.strip().lower()
                if key and key not in all_items:
                    all_items[key] = item
                    new_this_round += 1

            last_result = result

            # ── keep the richest bill details seen so far ─────────────────
            # A screenshot closer to the bottom usually has more bill fields.
            # We score by how many fee-fields are non-empty.
            def _bill_score(r: CartAnalysis) -> int:
                return sum(bool(v) for v in [
                    r.cart_total, r.items_subtotal, r.delivery_charge,
                    r.handling_charge, r.platform_fee, r.total_savings,
                ])
            if best_bill is None or _bill_score(result) > _bill_score(best_bill):
                best_bill = result

            # ── track no-progress streak ──────────────────────────────────
            if new_this_round == 0:
                zero_new_streak += 1
            else:
                zero_new_streak = 0

            log.info(
                "cart_scroll_step",
                attempt=attempt,
                new_items=new_this_round,
                total_items=len(all_items),
                has_more=result.has_more_items_below,
                zero_streak=zero_new_streak,
                cart_total=result.cart_total or "",
            )

            # ── vision says no more content below — finalise ──────────────
            if not result.has_more_items_below:
                log.info("cart_capture_complete",
                         reason="vision_no_more_below",
                         screenshots=screenshot_index,
                         items=len(all_items))
                break

            # ── stuck: 2 consecutive rounds with nothing new ──────────────
            if zero_new_streak >= 2:
                log.info("cart_capture_complete",
                         reason="no_new_items_2_rounds",
                         screenshots=screenshot_index,
                         items=len(all_items))
                break

            # ── scroll gradually (half-viewport step for overlap) ─────────
            scroll_y += SCROLL_STEP
            info = await self.browser.scroll_cart_container(scroll_y)
            await asyncio.sleep(0.8)   # was 0.4 — give DOM time to render new rows
            log.info(
                "cart_container_scrolled",
                method=info.get("method"),
                scroll_y=scroll_y,
                container_h=info.get("scrollHeight", 0),
            )

            # Safety: stop once we've scrolled past the container's bottom
            container_h = info.get("scrollHeight", 0)
            if container_h > 0 and scroll_y >= container_h:
                log.info("cart_capture_complete",
                         reason="reached_container_bottom",
                         screenshots=screenshot_index)
                break

        # ── Scroll back to top ────────────────────────────────────────────────
        await self.browser.scroll_cart_container(0)

        if last_result is None:
            log.warning("cart_full_capture_fallback")
            shot = await self.browser.screenshot("cart_fallback")
            return await self._vision.analyse_cart(shot)

        # ── Build merged result ───────────────────────────────────────────────
        # Use best_bill (richest fee data seen across all screenshots) not just
        # last_result, since the Bill Details panel may have been visible earlier.
        bb = best_bill or last_result
        merged = CartAnalysis(
            items=list(all_items.values()),
            item_count=len(all_items),
            items_subtotal=bb.items_subtotal,
            delivery_charge=bb.delivery_charge,
            handling_charge=bb.handling_charge,
            platform_fee=bb.platform_fee,
            total_savings=bb.total_savings,
            cart_total=bb.cart_total,
            is_serviceable=bb.is_serviceable,
            delivery_time=bb.delivery_time,
            late_night_fee=bb.late_night_fee,
            surge_charge=bb.surge_charge,
            reasoning=f"Vision-driven gradual scroll: {screenshot_index} screenshots.",
        )
        log.info(
            "cart_full_capture_complete",
            screenshots=screenshot_index,
            items_found=len(all_items),
            cart_total=merged.cart_total,
        )
        return merged

    # ── step execution ────────────────────────────────────────────────────────

    async def _execute_step(
        self,
        step: TaskStep,
        context: AgentContext,
    ) -> bool:
        step.status = "in_progress"
        context.current_step_id = step.step_id

        # ── Idempotency check (DOM badge + session list) ───────────────────
        if step.item_name in context.items_in_cart_this_session:
            log.info("step_skip_already_in_session", item=step.item_name)
            step.status = "skipped"
            return True
        if any(
            step.item_name.lower() in e.lower() or e.lower() in step.item_name.lower()
            for e in context.existing_cart_items
        ):
            log.info("step_skip_pre_existing",
                     item=step.item_name,
                     matched_pre_existing=[
                         e for e in context.existing_cart_items
                         if step.item_name.lower() in e.lower() or e.lower() in step.item_name.lower()
                     ][:3])
            step.status = "skipped"
            return True

        cart_before = await self._read_cart_count(context.platform)
        context.cart_count = cart_before
        log.info("step_start",
                 item=step.item_name,
                 target_qty=step.item_quantity,
                 cart_count=cart_before,
                 existing_cart_items=context.existing_cart_items[:6])

        # ── Search entry strategy ───────────────────────────────────────────────
        # Zepto is more reliable with DOM-driven search activation than
        # vision coordinates (which can hit product cards in dense layouts).
        search_ok = False
        if context.platform == "zepto":
            search_ok = await self.browser.search_via_dom(step.item_name, context.platform)
            if not search_ok:
                log.info("zepto_dom_search_miss_trying_vision", item=step.item_name)
        if not search_ok:
            pre_shot = await self.browser.screenshot(f"pre_{step.step_id}")
            search_coords = await self._vision.locate_search_bar(pre_shot, context)
            if search_coords:
                sx, sy = search_coords
                log.info("vision_search_start", item=step.item_name, x=sx, y=sy)
                search_ok = await self.browser.click_and_type(sx, sy, step.item_name)
                if search_ok:
                    await self.browser.wait_for_stable(timeout_ms=4000)
                    await self.browser.wait_for_search_results(
                        context.platform, timeout_ms=8000
                    )
            if not search_ok:
                # DOM fallback when vision cannot locate the search bar
                log.info("search_bar_dom_fallback", item=step.item_name)
                search_ok = await self.browser.search_via_dom(
                    step.item_name, context.platform
                )

        if not search_ok:
            log.warning("search_failed_step", item=step.item_name)
            step.status = "failed"
            return False

        await asyncio.sleep(2.5)  # let results fully render (increased from 1.5s)

        # ── VISION: analyse results page ───────────────────────────────────
        screenshot = await self.browser.screenshot(f"search_{step.step_id}")
        analysis = await self._vision.analyse(
            screenshot_b64=screenshot,
            item_name=step.item_name,
            target_quantity=step.item_quantity,
            context=context,
        )
        log.info("vision_analysis",
                 item=step.item_name,
                 target_qty=step.item_quantity,
                 page_state=analysis.page_state,
                 no_relevant_product=analysis.no_relevant_product,
                 item_already_in_cart=analysis.item_already_in_cart,
                 cart_qty_for_item=analysis.cart_quantity_for_item,
                 selected=analysis.selected_product,
                 selected_pack=analysis.selected_pack_size,
                 needs_scroll=analysis.needs_scroll,
                 confidence=analysis.confidence,
                 visible_packs=[(p.name, p.pack_size, p.pack_size_ml_or_g)
                                for p in analysis.all_visible_packs[:6]],
                 reasoning=(analysis.reasoning or "")[:300])
        # ── Handle vision result ──────────────────────────────────────────
        # Guard: if vision chose a snack for a non-snack item, treat as unavailable
        if (
            analysis.selected_product
            and not analysis.no_relevant_product
            and _is_snack_product(analysis.selected_product)
            and not _is_snack_request(step.item_name)
        ):
            log.warning(
                "category_mismatch_blocked_initial",
                item=step.item_name,
                selected=analysis.selected_product,
            )
            analysis.no_relevant_product = True
            analysis.selected_product = None

        if analysis.no_relevant_product:
            log.warning("step_no_product_found",
                        item=step.item_name,
                        target_qty=step.item_quantity,
                        reasoning=(analysis.reasoning or "")[:300])
            return await self._handle_unavailable(step, context)

        if analysis.item_already_in_cart:
            log.info("step_skip_already_in_cart",
                     item=step.item_name,
                     target_qty=step.item_quantity,
                     cart_qty_for_item=analysis.cart_quantity_for_item,
                     reasoning=(analysis.reasoning or "")[:300])
            step.status = "skipped"
            step.item_added_to_cart = True
            return True

        if analysis.needs_scroll:
            log.info("step_scroll_for_more",
                     item=step.item_name,
                     visible_packs=len(analysis.all_visible_packs))
            await self.browser._page.evaluate("window.scrollBy(0, 400)")
            await asyncio.sleep(0.8)
            screenshot = await self.browser.screenshot(f"scroll_{step.step_id}")
            analysis = await self._vision.analyse(
                screenshot_b64=screenshot,
                item_name=step.item_name,
                target_quantity=step.item_quantity,
                context=context,
            )
            log.info("vision_analysis_after_scroll",
                     item=step.item_name,
                     selected=analysis.selected_product,
                     selected_pack=analysis.selected_pack_size,
                     no_relevant_product=analysis.no_relevant_product,
                     reasoning=(analysis.reasoning or "")[:200])

        # ── Category-mismatch guard ───────────────────────────────────────────
        # If vision picked a snack product for a non-snack search item, reject it
        # so we fall through to SubstitutionAgent rather than adding garbage.
        if (
            analysis.selected_product
            and not analysis.no_relevant_product
            and _is_snack_product(analysis.selected_product)
            and not _is_snack_request(step.item_name)
        ):
            log.warning(
                "category_mismatch_blocked",
                item=step.item_name,
                selected=analysis.selected_product,
            )
            analysis.no_relevant_product = True
            analysis.selected_product = None

        # ── Execute combination plan ──────────────────────────────────────
        product_hint = analysis.selected_product or step.item_name
        step.product_selected = product_hint
        step.selected_product_details = {
            "name": product_hint,
            "pack_size": analysis.selected_pack_size,
            "price": analysis.selected_price,
        }
        if analysis.quantity_mismatch_note:
            log.warning(
                "quantity_mismatch",
                item=step.item_name,
                note=analysis.quantity_mismatch_note,
                ratio=analysis.mismatch_ratio,
            )
            step.selected_product_details["quantity_note"] = analysis.quantity_mismatch_note

        # ── Execute combination plan or single add ──────────────────────────────
        # Use vision-provided ADD button coordinates when available.
        add_x, add_y = self._analysis_add_coords(analysis)

        # ── Programmatic units override (mirrors original product_selector.py) ─
        # Never trust the LLM's arithmetic alone. If we know the pack size from
        # pack_size_ml_or_g, recompute units_needed mathematically.
        # When some quantity is already in cart (cart_quantity_for_item > 0),
        # the override accounts for what's already there so we only add the rest.
        if analysis.combination_plan and step.item_quantity:
            for pack in analysis.combination_plan:
                computed = _compute_units_needed(
                    step.item_quantity,
                    pack.pack_size_ml_or_g,
                    llm_units=pack.units_needed,
                )
                # Subtract units already in cart (vision-reported) if the pack
                # size is known, so we don't double-add.
                if pack.pack_size_ml_or_g > 0 and analysis.cart_quantity_for_item > 0:
                    already_packs = analysis.cart_quantity_for_item // pack.pack_size_ml_or_g
                    computed = max(0, computed - already_packs)
                    if already_packs > 0:
                        log.info("units_adjusted_for_existing_cart",
                                 item=step.item_name,
                                 already_packs=already_packs,
                                 cart_qty=analysis.cart_quantity_for_item,
                                 computed_final=computed)
                if computed != pack.units_needed:
                    log.info(
                        "units_override",
                        item=step.item_name,
                        target=step.item_quantity,
                        pack_g_ml=pack.pack_size_ml_or_g,
                        llm_said=pack.units_needed,
                        computed=computed,
                        direction="down" if computed < pack.units_needed else "up",
                    )
                    pack.units_needed = computed

        if analysis.combination_plan:
            plan_summary = [
                f"{p.units_needed}×{p.pack_name}(pack={p.pack_size_ml_or_g})"
                for p in analysis.combination_plan
            ]
            log.info("executing_combination_plan",
                     item=step.item_name,
                     target_qty=step.item_quantity,
                     cart_qty_already=analysis.cart_quantity_for_item,
                     plan=plan_summary)
            success = await self._execute_combination(
                step, context, analysis.combination_plan, cart_before, add_x, add_y
            )
        else:
            log.info("executing_single_add",
                     item=step.item_name,
                     product=product_hint,
                     pack=analysis.selected_pack_size,
                     cart_qty_already=analysis.cart_quantity_for_item)
            success = await self._add_single(
                step, context, product_hint, cart_before, add_x, add_y
            )

        return success

    async def _add_single(
        self,
        step: TaskStep,
        context: AgentContext,
        product_hint: str,
        cart_before: int,
        add_x: Optional[int] = None,
        add_y: Optional[int] = None,
    ) -> bool:
        """
        Click ADD once. DOM text-matching is tried first (pixel-perfect).
        Vision coords are the fallback when DOM finds no card.
        """
        if context.platform == "zepto":
            return await self._add_single_zepto_vision(
                step=step,
                context=context,
                product_hint=product_hint,
                add_x=add_x,
                add_y=add_y,
            )

        shot_before = await self.browser.screenshot(f"pre_add_{step.step_id}")

        # DOM first — reliable text-based card targeting
        clicked = await self.browser.click_add_button(product_hint)
        if not clicked and add_x is not None and add_y is not None:
            # Vision coord fallback
            log.info("dom_add_miss_using_vision_coords",
                     item=step.item_name, x=add_x, y=add_y)
            await self.browser.move_and_click(add_x, add_y)
            clicked = True

        if not clicked:
            log.warning("add_click_failed", item=step.item_name)
            step.status = "failed"
            return False

        await asyncio.sleep(1.2)

        # Zepto: if ADD click navigated to PDP, use the wide 'Add To Cart' CTA
        # instead.  This is the primary source of false-positives: clicking the
        # search-results card body navigates to PDP, and subsequent cart-count
        # reads return stale values that falsely satisfy the fast-path below.
        if context.platform == "zepto" and self.browser.is_on_pdp("zepto"):
            log.info("zepto_single_add_hit_pdp", item=step.item_name)
            pdp_ok = await self.browser.click_pdp_add_to_cart_cta()
            if pdp_ok:
                await asyncio.sleep(1.5)
                cart_after_pdp = await self._read_cart_count("zepto")
                if cart_after_pdp > cart_before:
                    _mark_done(step, context, product_hint, 1)
                    return True
                log.warning("zepto_pdp_cta_no_increment", item=step.item_name,
                            cart_before=cart_before, cart_after=cart_after_pdp)
            else:
                log.warning("zepto_pdp_cta_not_found", item=step.item_name)
            await self.browser.go_back()
            await asyncio.sleep(1.0)
            step.status = "failed"
            return False

        # Fast-path for Zepto: cart badge delta is usually more stable than
        # visual before/after in dense card layouts.
        cart_after_fast = await self._read_cart_count(context.platform)
        if context.platform == "zepto" and cart_after_fast > cart_before:
            _mark_done(step, context, product_hint, 1)
            return True

        # ─── Self-evaluation: vision compares before/after ──────────────────────
        shot_after = await self.browser.screenshot(f"post_add_{step.step_id}")
        verify = await self._vision.verify_add(shot_before, shot_after, product_hint)
        log.info("add_verified", item=step.item_name, success=verify.success,
                 signal=verify.signal, observed=verify.observed)

        # On Zepto, PDP navigation is a hard failure for this click path.
        if context.platform == "zepto" and verify.signal == "navigated_to_product_page":
            corrected = await self._self_correct_add(
                step=step, context=context,
                product_hint=product_hint,
                cart_before=cart_before,
                verify=verify,
                add_x=add_x, add_y=add_y,
                attempt_label=f"single_navfix_{step.step_id}",
            )
            if corrected:
                _mark_done(step, context, product_hint, 1)
                return True
            step.status = "failed"
            return False

        if verify.success:
            _mark_done(step, context, product_hint, 1)
            return True

        # Fallback: DOM cart count
        cart_after = await self._read_cart_count(context.platform)
        if cart_after > cart_before:
            _mark_done(step, context, product_hint, 1)
            return True

        # ─── Self-correction: act on verify’s retry_instruction then retry once ───
        corrected = await self._self_correct_add(
            step=step, context=context,
            product_hint=product_hint,
            cart_before=cart_before,
            verify=verify,
            add_x=add_x, add_y=add_y,
            attempt_label=f"single_{step.step_id}",
        )
        if corrected:
            _mark_done(step, context, product_hint, 1)
            return True

        log.warning("add_not_confirmed_after_correction", item=step.item_name,
                    cart_before=cart_before, verify_signal=verify.signal)
        step.status = "failed"
        return False

    async def _add_single_zepto_vision(
        self,
        step: TaskStep,
        context: AgentContext,
        product_hint: str,
        add_x: Optional[int],
        add_y: Optional[int],
    ) -> bool:
        """
        Zepto single-add path: vision-only.

        Do not use DOM ADD/stepper/cart badge as source-of-truth because Zepto
        can navigate to PDP on card clicks and cart badge values can be stale.
        """
        vx, vy = add_x, add_y
        for attempt in range(3):
            shot_before = await self.browser.screenshot(
                f"pre_add_zepto_{step.step_id}_{attempt}"
            )

            dom_rank = attempt if len((product_hint or "").split()) > 1 else 0
            dom_clicked = await self.browser.click_zepto_add_button(product_hint, rank=dom_rank)
            if dom_clicked:
                await asyncio.sleep(1.0)
                if self.browser.is_on_pdp("zepto"):
                    log.warning("zepto_single_dom_hit_pdp", item=step.item_name, attempt=attempt + 1)
                    await self.browser.go_back()
                    await asyncio.sleep(1.0)
                else:
                    shot_after_dom = await self.browser.screenshot(
                        f"post_add_zepto_dom_{step.step_id}_{attempt}"
                    )
                    vdom = await self._vision.verify_add(shot_before, shot_after_dom, product_hint)
                    log.info(
                        "zepto_single_dom_verified",
                        item=step.item_name,
                        success=vdom.success,
                        signal=vdom.signal,
                        observed=vdom.observed,
                    )
                    if vdom.success and self._verify_matches_target(vdom.observed, product_hint):
                        _mark_done(step, context, product_hint, 1)
                        return True
                    if vdom.success:
                        log.warning(
                            "zepto_single_reject_mismatch",
                            item=step.item_name,
                            target=product_hint,
                            observed=vdom.observed,
                        )

            if vx is None or vy is None:
                fresh = await self._vision.analyse(
                    screenshot_b64=shot_before,
                    item_name=product_hint,
                    target_quantity=step.item_quantity,
                    context=context,
                )
                if (
                    fresh.add_button_x_norm is None
                    or fresh.add_button_y_norm is None
                ):
                    log.warning(
                        "zepto_single_no_add_coords",
                        item=step.item_name,
                        product=product_hint,
                    )
                    continue
                vx, vy = self._analysis_add_coords(fresh)
                log.info("zepto_single_fresh_coords", item=step.item_name, x=vx, y=vy)

            await self.browser.move_and_click(vx, vy)
            await asyncio.sleep(1.0)

            if self.browser.is_on_pdp("zepto"):
                log.warning("zepto_single_vision_hit_pdp", item=step.item_name)
                await self.browser.go_back()
                await asyncio.sleep(1.0)
                vx, vy = None, None
                continue

            shot_after = await self.browser.screenshot(
                f"post_add_zepto_{step.step_id}_{attempt}"
            )
            verify = await self._vision.verify_add(shot_before, shot_after, product_hint)
            log.info(
                "zepto_single_verified",
                item=step.item_name,
                success=verify.success,
                signal=verify.signal,
                observed=verify.observed,
            )

            if verify.success and self._verify_matches_target(verify.observed, product_hint):
                _mark_done(step, context, product_hint, 1)
                return True
            if verify.success:
                log.warning(
                    "zepto_single_reject_mismatch",
                    item=step.item_name,
                    target=product_hint,
                    observed=verify.observed,
                )

            vx, vy = None, None

        step.status = "failed"
        return False

    async def _attempt_zepto_unit_add(
        self,
        step: TaskStep,
        context: AgentContext,
        pack_name: str,
        unit_index: int,
        current_total: int,
        add_x: Optional[int],
        add_y: Optional[int],
    ) -> tuple[bool, Optional[int], Optional[int]]:
        """
        Try adding one Zepto unit using vision coordinates only.
        Returns (success, x, y) where x/y are last usable coords.
        """
        vx, vy = add_x, add_y
        for attempt in range(3):
            # Recovery for Zepto recommendation drift: reset to top of search list
            # and refresh search query before retrying ADD selection.
            if attempt > 0:
                await self.browser.scroll_to_top()
                await self.browser.search_via_dom(step.item_name, context.platform)
                await asyncio.sleep(0.8)

            shot_before = await self.browser.screenshot(
                f"pre_combo_zepto_{step.step_id}_{current_total}_{unit_index}_{attempt}"
            )

            dom_rank = attempt if len((pack_name or "").split()) > 1 else 0
            dom_clicked = await self.browser.click_zepto_add_button(pack_name, rank=dom_rank)
            if dom_clicked:
                await asyncio.sleep(1.0)
                if self.browser.is_on_pdp("zepto"):
                    log.warning("zepto_combo_dom_hit_pdp", pack=pack_name, unit=unit_index + 1, attempt=attempt + 1)
                    await self.browser.go_back()
                    await asyncio.sleep(1.0)
                else:
                    shot_after_dom = await self.browser.screenshot(
                        f"post_combo_zepto_dom_{step.step_id}_{current_total}_{unit_index}_{attempt}"
                    )
                    vdom = await self._vision.verify_add(shot_before, shot_after_dom, pack_name)
                    log.info(
                        "zepto_combo_dom_verified",
                        pack=pack_name,
                        unit=unit_index + 1,
                        success=vdom.success,
                        signal=vdom.signal,
                        observed=vdom.observed,
                    )
                    if vdom.success and self._verify_matches_target(vdom.observed, pack_name):
                        return True, vx, vy
                    if vdom.success:
                        log.warning(
                            "zepto_combo_reject_mismatch",
                            pack=pack_name,
                            unit=unit_index + 1,
                            observed=vdom.observed,
                        )

            if vx is None or vy is None:
                fresh = await self._vision.analyse(
                    screenshot_b64=shot_before,
                    item_name=pack_name,
                    target_quantity=step.item_quantity,
                    context=context,
                )
                if (
                    fresh.add_button_x_norm is None
                    or fresh.add_button_y_norm is None
                ):
                    log.warning("zepto_combo_no_add_coords", pack=pack_name, unit=unit_index + 1)
                    continue
                vx, vy = self._analysis_add_coords(fresh)
                log.info("zepto_combo_fresh_coords", pack=pack_name, unit=unit_index + 1, x=vx, y=vy)

            await self.browser.move_and_click(vx, vy)
            await asyncio.sleep(1.0)

            if self.browser.is_on_pdp("zepto"):
                log.warning("zepto_combo_vision_hit_pdp", pack=pack_name, unit=unit_index + 1)
                await self.browser.go_back()
                await asyncio.sleep(1.0)
                vx, vy = None, None
                continue

            shot_after = await self.browser.screenshot(
                f"post_combo_zepto_{step.step_id}_{current_total}_{unit_index}_{attempt}"
            )
            verify = await self._vision.verify_add(shot_before, shot_after, pack_name)
            log.info(
                "zepto_combo_verified",
                pack=pack_name,
                unit=unit_index + 1,
                success=verify.success,
                signal=verify.signal,
                observed=verify.observed,
            )
            if verify.success and self._verify_matches_target(verify.observed, pack_name):
                return True, vx, vy
            if verify.success:
                log.warning(
                    "zepto_combo_reject_mismatch",
                    pack=pack_name,
                    unit=unit_index + 1,
                    observed=verify.observed,
                )

            vx, vy = None, None

        return False, vx, vy

    async def _execute_combination_zepto_vision(
        self,
        step: TaskStep,
        context: AgentContext,
        plan: list,
        add_x: Optional[int],
        add_y: Optional[int],
    ) -> bool:
        """
        Zepto combination flow: add each required unit using vision-only clicks.
        """
        total_added = 0
        first_pack = True

        for pack in plan:
            pack_name = (
                pack.pack_name if hasattr(pack, "pack_name")
                else pack.get("pack_name", step.item_name)
            )
            units_needed = (
                pack.units_needed if hasattr(pack, "units_needed")
                else pack.get("units_needed", 1)
            )

            vx = add_x if first_pack else None
            vy = add_y if first_pack else None
            first_pack = False

            for unit_idx in range(units_needed):
                ok, vx, vy = await self._attempt_zepto_unit_add(
                    step=step,
                    context=context,
                    pack_name=pack_name,
                    unit_index=unit_idx,
                    current_total=total_added,
                    add_x=vx,
                    add_y=vy,
                )
                if ok:
                    total_added += 1
                    # Blinkit-style continuation: once first unit is in cart,
                    # prefer deterministic '+' stepper increments for remaining
                    # units of the same product.
                    remaining = units_needed - (unit_idx + 1)
                    if remaining > 0:
                        stepped = await self.browser.increment_quantity_stepper(
                            times=remaining,
                            product_hint=pack_name,
                            allow_unknown_qty=False,
                        )
                        if stepped:
                            total_added += remaining
                            log.info(
                                "zepto_stepper_incremented",
                                pack=pack_name,
                                extra=remaining,
                                total_added=total_added,
                            )
                            break
                        log.warning(
                            "zepto_stepper_increment_failed",
                            pack=pack_name,
                            wanted=remaining,
                        )
                        # Do not continue with broad ADD retries after a
                        # same-card stepper failure; that can add sibling
                        # variants and over-count requested quantity.
                        break
                else:
                    log.warning(
                        "zepto_combo_unit_failed",
                        pack=pack_name,
                        unit=unit_idx + 1,
                    )

        if total_added > 0:
            pack_desc = " + ".join(
                f"{(p.units_needed if hasattr(p,'units_needed') else p.get('units_needed',1))}"
                f"×{(p.pack_name if hasattr(p,'pack_name') else p.get('pack_name',''))}"
                for p in plan
            )
            _mark_done(step, context, step.item_name, total_added, pack_desc=pack_desc)
            log.info("combination_plan_done", item=step.item_name,
                     units=total_added, desc=pack_desc)
            return True

        step.status = "failed"
        return False

    async def _execute_combination(
        self,
        step: TaskStep,
        context: AgentContext,
        plan: list,
        cart_before: int,
        add_x: Optional[int] = None,
        add_y: Optional[int] = None,
    ) -> bool:
        """
        Add N packs per combination plan entry.

        DOM text-based ADD click is always attempted first (pixel-perfect).
        Vision coords are used as fallback when DOM finds no matching card.
        Handles the case where a stepper is already showing (item was added in
        a previous session): reads the current quantity and increments from there.
        """
        if context.platform == "zepto":
            return await self._execute_combination_zepto_vision(
                step=step,
                context=context,
                plan=plan,
                add_x=add_x,
                add_y=add_y,
            )

        total_added = 0
        first_pack = True

        for pack in plan:
            pack_name = (
                pack.pack_name if hasattr(pack, "pack_name")
                else pack.get("pack_name", step.item_name)
            )
            units_needed = (
                pack.units_needed if hasattr(pack, "units_needed")
                else pack.get("units_needed", 1)
            )

            shot_before = await self.browser.screenshot(
                f"pre_combo_{step.step_id}_{total_added}"
            )

            # ─── Step A: try to click ADD (DOM first) ────────────────
            dom_clicked = await self.browser.click_add_button(pack_name)

            # click_add_button returns True for two reasons:
            #   (a) found & clicked ADD                  → need to verify
            #   (b) stepper already present              → already_in_cart scenario
            # Distinguish by checking DOM stepper immediately:
            await asyncio.sleep(0.4)
            current_stepper_qty = await self.browser._get_stepper_qty(pack_name) or 0

            # Save coords BEFORE setting first_pack=False so they can be
            # forwarded to _self_correct_add.
            _sc_add_x = add_x if first_pack else None
            _sc_add_y = add_y if first_pack else None

            if not dom_clicked:
                # DOM found nothing — try vision coord fallback
                if first_pack and add_x is not None and add_y is not None:
                    log.info("dom_add_miss_using_vision_coords",
                             pack=pack_name, x=add_x, y=add_y)
                    await self.browser.move_and_click(add_x, add_y)
                    await asyncio.sleep(0.8)
                else:
                    log.warning("combination_add_failed", pack=pack_name)
                    first_pack = False
                    continue
            first_pack = False

            # Wait for UI to settle after click
            await asyncio.sleep(1.0)

            # Zepto: detect PDP navigation BEFORE relying on cart-badge or stepper
            # checks.  When the ADD click navigates to PDP, the stale cart-badge
            # fast-path below produces false positives (root cause confirmed via
            # logs: no combo_add_verified + qty=None strategy=B).
            if context.platform == "zepto" and dom_clicked and self.browser.is_on_pdp("zepto"):
                log.info("zepto_combo_add_hit_pdp", pack=pack_name)
                pdp_ok = await self.browser.click_pdp_add_to_cart_cta()
                if pdp_ok:
                    await asyncio.sleep(1.5)
                    cart_after_pdp = await self._read_cart_count("zepto")
                    if cart_after_pdp > cart_before + total_added:
                        total_added += 1
                        log.info("zepto_pdp_first_unit_confirmed", pack=pack_name,
                                 cart_now=cart_after_pdp, total_added=total_added)
                        extra_pdp = units_needed - 1
                        if extra_pdp > 0:
                            ok = await self.browser.increment_quantity_stepper(
                                extra_pdp, pack_name
                            )
                            if ok:
                                total_added += extra_pdp
                                log.info("zepto_pdp_stepper_done", pack=pack_name,
                                         extra=extra_pdp, total_added=total_added)
                    else:
                        log.warning("zepto_pdp_cta_no_increment", pack=pack_name,
                                    cart_now=cart_after_pdp,
                                    expected_min=cart_before + total_added + 1)
                else:
                    log.warning("zepto_pdp_cta_not_found", pack=pack_name)
                await self.browser.go_back()
                await asyncio.sleep(1.5)
                first_pack = False
                continue

            # ─── Step B: visual verify ────────────────────────
            # Determine how many we now have in the cart for this SKU
            cart_now = await self._read_cart_count(context.platform)
            incremented_this_pack = max(0, cart_now - cart_before - total_added)

            # Zepto fast-path: prefer DOM cart delta before expensive vision verify.
            if context.platform == "zepto" and incremented_this_pack > 0:
                verify = VerifyAddResult(success=True, signal="cart_incremented")
            else:
                # ─── Step B: visual verify ────────────────────────
                shot_after = await self.browser.screenshot(
                    f"post_combo_{step.step_id}_{total_added}"
                )
                verify = await self._vision.verify_add(shot_before, shot_after, pack_name)
                log.info("combo_add_verified", pack=pack_name, success=verify.success,
                         signal=verify.signal, observed=verify.observed)

            # On Zepto, if click navigated to PDP, do not count success.
            # Run self-correction first; only mark added if correction succeeds.
            if context.platform == "zepto" and verify.signal == "navigated_to_product_page":
                corrected = await self._self_correct_add(
                    step=step, context=context,
                    product_hint=pack_name,
                    cart_before=cart_before + total_added,
                    verify=verify,
                    add_x=_sc_add_x,
                    add_y=_sc_add_y,
                    attempt_label=f"combo_navfix_{step.step_id}_{total_added}",
                )
                if corrected:
                    total_added += 1
                else:
                    log.warning("combo_navfix_failed", pack=pack_name)
                continue

            # For Zepto, avoid trusting vision-only success. Require either
            # cart increment or a visible product stepper.
            if context.platform == "zepto":
                zepto_confirmed = incremented_this_pack > 0 or current_stepper_qty > 0
                if zepto_confirmed:
                    total_added += 1
                elif verify.success:
                    log.warning("zepto_verify_only_ignored", pack=pack_name)
            elif verify.success or incremented_this_pack > 0:
                total_added += 1  # first unit confirmed
            elif current_stepper_qty > 0:
                # Stepper was ALREADY there before the click — previous session leftover
                log.info("stepper_pre_existing", pack=pack_name,
                         current_qty=current_stepper_qty, target=units_needed)
                # Count what’s already there toward our total
                already_have = current_stepper_qty
                if already_have >= units_needed:
                    total_added += units_needed
                    continue  # no increments needed
                total_added += already_have
                units_needed = units_needed - already_have  # need this many MORE
                # Fall through to stepper increment below
            else:
                # ─── Self-correction: vision said add failed ─────────────────────────
                corrected = await self._self_correct_add(
                    step=step, context=context,
                    product_hint=pack_name,
                    cart_before=cart_before + total_added,
                    verify=verify,
                    add_x=_sc_add_x,
                    add_y=_sc_add_y,
                    attempt_label=f"combo_{step.step_id}_{total_added}",
                )
                if corrected:
                    total_added += 1
                else:
                    log.warning("combo_first_unit_not_confirmed", pack=pack_name)
                    continue

            # ─── Step C: increment stepper for additional units ────────────
            extra = units_needed - 1  # we have 1 (or more via stepper), need this many more
            if extra > 0:
                ok = await self.browser.increment_quantity_stepper(
                    times=extra,
                    product_hint=pack_name,
                    allow_unknown_qty=(context.platform != "zepto"),
                )
                if ok:
                    total_added += extra
                    log.info("stepper_incremented", pack=pack_name,
                             extra=extra, total_added=total_added)
                else:
                    log.warning("stepper_increment_partial", pack=pack_name,
                                wanted=extra)

        if total_added > 0:
            pack_desc = " + ".join(
                f"{(p.units_needed if hasattr(p,'units_needed') else p.get('units_needed',1))}"
                f"×{(p.pack_name if hasattr(p,'pack_name') else p.get('pack_name',''))}"
                for p in plan
            )
            _mark_done(step, context, step.item_name, total_added, pack_desc=pack_desc)
            log.info("combination_plan_done", item=step.item_name,
                     units=total_added, desc=pack_desc)
            return True

        step.status = "failed"
        return False

    async def _self_correct_add(
        self,
        step: TaskStep,
        context: AgentContext,
        product_hint: str,
        cart_before: int,
        verify: VerifyAddResult,
        add_x: Optional[int],
        add_y: Optional[int],
        attempt_label: str,
    ) -> bool:
        """
        Self-correction pass: vision said the ADD click didn't work.
        Strategy (in priority order):
          1. If verify.retry_instruction mentions 'scroll', scroll and retry vision.
          2. If vision coords available, click directly at those coords.
          3. Re-run vision analyse on current page to get fresh ADD coords.
          4. If DOM add works, success.
        Returns True if the item was successfully added.
        """
        retry_hint = (verify.retry_instruction or "").lower()
        log.info("self_correct_start",
                 item=step.item_name,
                 product=product_hint,
                 signal=verify.signal,
                 retry_hint=retry_hint[:120])

        # Strategy 0: if we navigated to a product detail page, go back first.
        # This restores the search results page so subsequent strategies can
        # re-analyse and click the ADD button from the correct context.
        if verify.signal == "navigated_to_product_page":
            log.info("self_correct_go_back", item=step.item_name,
                     reason="navigated_to_product_page")
            try:
                await self.browser._page.go_back(wait_until="domcontentloaded",
                                                  timeout=8_000)
                await asyncio.sleep(1.5)
            except Exception as _back_err:
                log.warning("self_correct_go_back_failed",
                            item=step.item_name, error=str(_back_err))

        # Strategy 1: scroll if vision says content is off-screen
        if "scroll" in retry_hint:
            await self.browser._page.evaluate("window.scrollBy(0, 300)")
            await asyncio.sleep(0.6)
            log.info("self_correct_scrolled", item=step.item_name)

        # Strategy 2: use known vision coords as immediate fallback
        if add_x is not None and add_y is not None and verify.signal == "no_change":
            log.info("self_correct_vision_coords",
                     item=step.item_name, x=add_x, y=add_y)
            shot_pre = await self.browser.screenshot(f"correct_pre_{attempt_label}")
            await self.browser.move_and_click(add_x, add_y)
            await asyncio.sleep(1.2)
            shot_post = await self.browser.screenshot(f"correct_post_{attempt_label}")
            v2 = await self._vision.verify_add(shot_pre, shot_post, product_hint)
            log.info("self_correct_verify", item=step.item_name,
                     success=v2.success, signal=v2.signal, observed=v2.observed)
            if v2.success:
                return True
            cart_now = await self._read_cart_count(context.platform)
            if cart_now > cart_before:
                return True

        # Strategy 3: re-run vision on current page — get fresh ADD button coords
        fresh_shot = await self.browser.screenshot(f"correct_reanalyse_{attempt_label}")
        try:
            fresh_analysis = await self._vision.analyse(
                screenshot_b64=fresh_shot,
                item_name=product_hint,
                target_quantity=step.item_quantity,
                context=context,
            )
            log.info("self_correct_reanalyse",
                     item=step.item_name,
                     selected=fresh_analysis.selected_product,
                     already_in_cart=fresh_analysis.item_already_in_cart,
                     cart_qty=fresh_analysis.cart_quantity_for_item,
                     reasoning=(fresh_analysis.reasoning or "")[:200])

            # If vision now sees item already in cart, we're done
            if fresh_analysis.item_already_in_cart:
                log.info("self_correct_already_in_cart", item=step.item_name)
                return True

            # Use fresh coords if available
            if (fresh_analysis.add_button_x_norm is not None and
                    fresh_analysis.add_button_y_norm is not None):
                fx, fy = self._analysis_add_coords(fresh_analysis)
                log.info("self_correct_fresh_coords",
                         item=step.item_name, x=fx, y=fy)
                shot_pre2 = await self.browser.screenshot(f"correct_pre2_{attempt_label}")
                await self.browser.move_and_click(fx, fy)
                # Wait generously: Zepto and other apps may add from product page
                # and auto-navigate back to search results (1–3 s total).
                # Check cart count FIRST — it's more reliable than vision
                # when the page changes between pre and post screenshots.
                await asyncio.sleep(3.0)
                cart_after_click = await self._read_cart_count(context.platform)
                if cart_after_click > cart_before:
                    log.info("self_correct_s3_success_by_cart",
                             item=step.item_name,
                             cart_before=cart_before, cart_after=cart_after_click)
                    return True
                shot_post2 = await self.browser.screenshot(f"correct_post2_{attempt_label}")
                v3 = await self._vision.verify_add(shot_pre2, shot_post2, product_hint)
                log.info("self_correct_verify2", item=step.item_name,
                         success=v3.success, signal=v3.signal, observed=v3.observed)
                if v3.success:
                    return True
                cart_now = await self._read_cart_count(context.platform)
                if cart_now > cart_before:
                    return True
        except Exception as exc:
            log.warning("self_correct_reanalyse_error",
                        item=step.item_name, error=str(exc))

        # Strategy 4: final DOM attempt with product name
        dom_ok = await self.browser.click_add_button(product_hint)
        if dom_ok:
            await asyncio.sleep(1.0)
            cart_now = await self._read_cart_count(context.platform)
            if cart_now > cart_before:
                log.info("self_correct_dom_worked", item=step.item_name)
                return True

        log.warning("self_correct_exhausted", item=step.item_name)
        return False

    async def _handle_unavailable(
        self,
        step: TaskStep,
        context: AgentContext,
    ) -> bool:
        """Ask SubstitutionAgent for a replacement and retry once."""
        recipe_ctx = context.recipe_context.recipe_name if context.recipe_context else None
        sub_result = await self._substitution.find_substitute(
            item_name=step.item_name,
            quantity=step.item_quantity or "1 unit",
            recipe_context=recipe_ctx,
            already_tried=list(context.substitutions_made.keys()),
            platform=context.platform,
        )

        sub_name = sub_result.substitute_name
        # Safety: strip verbose parenthetical descriptions and "or" compounds that
        # Gemini sometimes generates (e.g. "Paneer (for savory) or Greek Yogurt (for baking)")
        import re as _re
        sub_name = _re.sub(r'\s*\(.*?\)', '', sub_name).strip()  # remove "(for ...)" notes
        sub_name = sub_name.split(' or ')[0].strip()             # take first option before " or "
        sub_name = sub_name.split(' / ')[0].strip()              # take first option before " / "
        if not sub_name:
            sub_name = sub_result.substitute_name                # fall back to raw if cleaning wiped it
        log.info("substitution_attempt", original=step.item_name, sub=sub_name,
                 reason=sub_result.reason)
        context.substitutions_made[step.item_name] = sub_name
        context.substitution_reasons[step.item_name] = sub_result.reason

        # Search for substitute
        search_ok = await self.browser.search_via_dom(sub_name, context.platform)
        if not search_ok:
            step.status = "failed"
            return False

        await asyncio.sleep(1.5)
        cart_before = context.cart_count

        # Take fresh screenshot to find the substitute product
        screenshot = await self.browser.screenshot(f"sub_{step.step_id}")
        analysis = await self._vision.analyse(
            screenshot_b64=screenshot,
            item_name=sub_name,
            target_quantity=sub_result.substitute_quantity,
            context=context,
        )
        if analysis.no_relevant_product:
            step.status = "failed"
            return False

        product_hint = analysis.selected_product or sub_name
        clicked = await self.browser.click_add_button(product_hint)
        if not clicked:
            step.status = "failed"
            return False

        await asyncio.sleep(1.2)
        cart_after = await self._read_cart_count(context.platform)
        if cart_after > cart_before:
            _mark_done(step, context, product_hint, 1, is_substitute=True)
            return True

        step.status = "failed"
        return False

    async def _has_stepper_for(self, product_hint: str) -> bool:
        """Check if a quantity stepper is visible (ADD was successful)."""
        try:
            return await self.browser._page.evaluate(f"""() => {{
                const target = {product_hint!r}.toLowerCase().split(' ').filter(w => w.length > 2);
                const steppers = Array.from(document.querySelectorAll('div,span')).filter(el => {{
                    return /^\\d+$/.test((el.innerText||'').trim()) && el.offsetParent !== null;
                }});
                for (const s of steppers) {{
                    const container = s.closest('[class*="card"],[class*="product"],[class*="item"]') || s.parentElement?.parentElement;
                    if (!container) continue;
                    const txt = (container.innerText||'').toLowerCase();
                    if (target.some(w => txt.includes(w))) return true;
                }}
                return false;
            }}""")
        except Exception:
            return False


# ── helpers ────────────────────────────────────────────────────────────────────

import math as _math
import re as _re


def _compute_units_needed(target_qty: str, pack_size_g_or_ml: int, llm_units: int = 1) -> int:
    """
    Programmatic arithmetic override – port of original product_selector.py.
    Parses a target quantity string (e.g. "2 kg", "500 g", "3 L", "1 dozen") and
    divides by the pack size in the same base unit.

    CLOSE-ENOUGH RULE: if a single pack covers ≥ 75 % of the target, 1 unit
    is sufficient.  This prevents adding unnecessary extra units when the pack
    is only slightly under the requested amount:
      900 g pack for 1 kg target → 900/1000 = 0.9 ≥ 0.75 → 1 unit  (not 2)
      500 g pack for 1 kg target → 500/1000 = 0.5 < 0.75 → 2 units ✓

    When pack_size_g_or_ml == 0 (count-based items like eggs, tablets), we cannot
    compute arithmetically so we trust the LLM's llm_units value.
    """
    if pack_size_g_or_ml <= 0:
        return llm_units  # trust LLM — no numeric pack spec (e.g. count-based items)
    s = (target_qty or "").strip().lower().replace(",", "")
    m = _re.search(
        r"([\d.]+)\s*"
        r"(dozen|dz|pcs?|pieces?|units?|count|ct|nos?\.?|eggs?|tablets?|tabs?|capsules?|caps?"
        r"|l|lt|lts|ltr|litre|liter|liters|litres|ml|kg|kilo|kilogram|kilograms|g|gm|gms|gram|grams)s?",
        s,
    )
    if not m:
        return llm_units  # unrecognised unit — trust LLM rather than defaulting to 1
    val = float(m.group(1))
    unit = m.group(2)
    if unit in ("dozen", "dz"):
        target_base = val * 12     # 1 dozen = 12 pieces
    elif unit in ("l", "lt", "lts", "ltr", "litre", "liter", "liters", "litres"):
        target_base = val * 1000   # convert to ml
    elif unit == "ml":
        target_base = val
    elif unit in ("kg", "kilo", "kilogram", "kilograms"):
        target_base = val * 1000   # convert to g
    elif unit in ("pc", "pcs", "piece", "pieces", "unit", "units", "count",
                  "ct", "no", "nos", "egg", "eggs", "tablet", "tablets",
                  "tab", "tabs", "capsule", "capsules", "cap", "caps"):
        target_base = val          # count as-is
    else:
        target_base = val          # already g / ml

    if target_base <= 0:
        return 1
    # Single-pack close-enough check
    if pack_size_g_or_ml / target_base >= 0.75:
        return 1
    return max(1, _math.ceil(target_base / pack_size_g_or_ml))


def _mark_done(
    step: TaskStep,
    context: AgentContext,
    product_name: str,
    units: int,
    is_substitute: bool = False,
    pack_desc: str = "",
) -> None:
    step.status = "done"
    step.item_added_to_cart = True
    context.cart_count += units
    context.items_in_cart_this_session.append(step.item_name)
    # Store for summary display
    if step.selected_product_details is None:
        step.selected_product_details = {}
    if pack_desc:
        step.selected_product_details["pack_desc"] = pack_desc
    step.selected_product_details["units_added"] = units
    log.info("item_added", item=step.item_name, product=product_name,
             units=units, pack_desc=pack_desc, sub=is_substitute)


def _build_summary(
    steps: list[TaskStep],
    context: AgentContext,
    duration: float,
    cart_vision: Optional[CartAnalysis] = None,
) -> CartSummary:
    """
    Build CartSummary from:
      1. cart_vision (ground truth from cart screenshot)  — quantities, prices, fees
      2. steps / context — status, what was requested, substitutions, session origin

    Previous-session items (those already in cart before this run) are included
    so the user gets the full picture, but flagged with from_previous_session=True.
    """

    def _fuzzy_match(a: str, b: str) -> bool:
        noise = {"kg", "gm", "g", "ml", "l", "lt", "the", "of", "a", "an",
                 "pack", "packet", "bag", "sachet", "pouch"}
        wa = set(a.lower().split()) - noise
        wb = set(b.lower().split()) - noise
        if not wa or not wb:
            return False
        return len(wa & wb) / max(len(wa), len(wb)) >= 0.4

    # Build lookup: lowercased product_name → CartLineItem
    vision_by_name: dict[str, object] = {}
    if cart_vision:
        for vi in cart_vision.items:
            vision_by_name[vi.product_name.lower()] = vi

    def _find_vision_item(names: list[str]):
        for candidate in names:
            if not candidate:
                continue
            # Exact substring first
            cl = candidate.lower()
            for vname, vi in vision_by_name.items():
                if cl in vname or vname in cl:
                    return vi
            # Fuzzy fallback
            for vname, vi in vision_by_name.items():
                if _fuzzy_match(candidate, vname):
                    return vi
        return None

    items_summary: list[CartItemSummary] = []
    items_this_session = 0

    for step in steps:
        details = step.selected_product_details or {}
        vi = _find_vision_item([
            step.item_name,
            step.product_selected or "",
            details.get("name", ""),
        ])

        is_prev_session = step.status == "skipped" and any(
            step.item_name.lower() in e.lower() or e.lower() in step.item_name.lower()
            for e in context.existing_cart_items
        )
        is_alt = step.item_name in context.substitutions_made

        if vi:
            pack_desc   = vi.pack_description or details.get("pack_desc") or ""
            qty_added   = vi.pack_description or vi.quantity or step.item_quantity or ""
            price       = vi.total_price or vi.unit_price or details.get("price") or ""
            unit_price  = vi.unit_price or details.get("price") or ""
            mrp         = vi.mrp or ""
            savings     = vi.savings or ""
            product     = vi.product_name or step.product_selected or ""
        else:
            pack_desc   = details.get("pack_desc") or details.get("pack_size") or ""
            units_added = details.get("units_added", 1 if step.status == "done" else 0)
            qty_added   = (
                f"{units_added}×{pack_desc}" if pack_desc and units_added > 1
                else pack_desc or step.item_quantity or ""
            )
            price       = details.get("price") or ""
            unit_price  = price
            mrp = savings = ""
            product = step.product_selected or ""

        if step.status == "done" and not is_prev_session:
            items_this_session += 1

        items_summary.append(CartItemSummary(
            item_name=step.item_name,
            quantity_requested=step.item_quantity,
            quantity_added=qty_added,
            pack_description=pack_desc or None,
            product_selected=product or None,
            unit_price=unit_price or None,
            total_price=price or None,
            mrp=mrp or None,
            savings=savings or None,
            quantity_note=details.get("quantity_note") or None,
            status=(
                "added"   if step.status == "done"
                else "skipped" if step.status == "skipped"
                else "failed"
            ),
            from_previous_session=is_prev_session,
            alternative_used=is_alt,
            alternative_reason=context.substitution_reasons.get(step.item_name, ""),
        ))

    # Include previous-session cart items that weren't in this request at all
    session_item_names_lower = {s.item_name.lower() for s in steps}
    # Build a lookup of product names added this session (steps + their resolved
    # product names) so we can detect "same product, different name" duplicates.
    _this_session_products: set[str] = set()
    for _s in steps:
        if _s.item_name:
            _this_session_products.add(_s.item_name.lower())
        if _s.product_selected:
            _this_session_products.add(_s.product_selected.lower())
        _det = _s.selected_product_details or {}
        if _det.get("name"):
            _this_session_products.add(_det["name"].lower())

    existing_lower = [e.lower() for e in context.existing_cart_items]

    for vi in (cart_vision.items if cart_vision else []):
        vl = vi.product_name.lower()

        # Check 1: exact/substring match against all names we know about this session
        already_listed = vl in _this_session_products or any(
            vl in p or p in vl for p in _this_session_products if p
        )

        # Check 2: fuzzy match against step item_name AND product_selected
        if not already_listed:
            already_listed = any(
                _fuzzy_match(vi.product_name, s.item_name) or
                _fuzzy_match(vi.product_name, s.product_selected or "")
                for s in steps
            )

        if already_listed:
            continue  # already represented in the per-step section above

        # Only include as "previous session" if this product was actually in the
        # cart BEFORE this session started.  If existing_cart_items is empty the
        # cart was clean — nothing should be labelled as pre-existing.
        was_pre_existing = bool(existing_lower) and any(
            vl in e or e in vl or _fuzzy_match(vi.product_name, e)
            for e in existing_lower
        )
        if not was_pre_existing:
            continue  # item added this session but product name didn't match step — skip

        items_summary.append(CartItemSummary(
            item_name=vi.product_name,
            quantity_added=vi.pack_description or vi.quantity or "",
            pack_description=vi.pack_description or None,
            product_selected=vi.product_name,
            unit_price=vi.unit_price or None,
            total_price=vi.total_price or vi.unit_price or None,
            mrp=vi.mrp or None,
            savings=vi.savings or None,
            status="added",
            from_previous_session=True,
        ))

    added   = sum(1 for s in steps if s.status == "done")
    skipped = sum(1 for s in steps if s.status == "skipped")
    failed  = sum(1 for s in steps if s.status not in ("done", "skipped"))

    # Bill breakdown — vision ground truth first, estimated fallback
    cv = cart_vision
    return CartSummary(
        items=items_summary,
        total_items_requested=len(steps),
        total_items_added=added,
        total_items_skipped=skipped,
        total_items_failed=failed,
        items_this_session=items_this_session,
        items_subtotal=cv.items_subtotal or "" if cv else "",
        delivery_charge=cv.delivery_charge or "" if cv else "",
        handling_charge=cv.handling_charge or "" if cv else "",
        platform_fee=cv.platform_fee or "" if cv else "",
        total_savings=cv.total_savings or "" if cv else "",
        grand_total=cv.cart_total or "" if cv else "",
        estimated_total=(
            cv.cart_total if cv and cv.cart_total
            else (f"~₹{context.estimated_spend}" if context.estimated_spend else "")
        ),
        duration_seconds=round(duration, 1),
        is_serviceable=cv.is_serviceable if cv else True,
        delivery_time=cv.delivery_time or "" if cv else "",
        late_night_fee=cv.late_night_fee or "" if cv else "",
        surge_charge=cv.surge_charge or "" if cv else "",
    )
