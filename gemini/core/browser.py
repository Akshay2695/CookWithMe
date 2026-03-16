from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Optional

import structlog
from playwright.async_api import (
    Browser, BrowserContext, Page, Playwright, async_playwright
)

from gemini.config.settings import settings

log = structlog.get_logger()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en', 'hi'] });
window.chrome = { runtime: {} };
"""


class BrowserManager:

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._session_file: Optional[Path] = None

    async def start(self, platform: str = "default") -> Page:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.browser_headless,
            slow_mo=settings.browser_slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-position=0,0",
            ],
        )

        ctx_args: dict = {
            "viewport": {
                "width": settings.browser_viewport_width,
                "height": settings.browser_viewport_height,
            },
            "user_agent": random.choice(USER_AGENTS),
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
            "geolocation": {"latitude": 19.0760, "longitude": 72.8777},
            "permissions": ["geolocation"],
            "extra_http_headers": {"Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"},
        }

        self._session_file = settings.session_dir / f"{platform}_session.json"
        if self._session_file.exists():
            log.info("restoring_session", platform=platform)
            ctx_args["storage_state"] = str(self._session_file)

        self._context = await self._browser.new_context(**ctx_args)
        await self._context.add_init_script(STEALTH_SCRIPT)
        self._page = await self._context.new_page()
        log.info("browser_started", platform=platform)
        return self._page

    # ── screenshot ────────────────────────────────────────────────────────────

    async def screenshot(self, label: str = "") -> str:
        assert self._page, "Browser not started"
        path = settings.screenshot_dir / f"{label or 'ss'}_{int(time.time())}.png"
        await self._page.screenshot(path=str(path), full_page=False)
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()

    # ── interactions ──────────────────────────────────────────────────────────

    async def move_and_click(self, x: int, y: int) -> None:
        assert self._page
        steps = random.randint(8, 15)
        jx = random.randint(-3, 3)
        jy = random.randint(-3, 3)
        await self._page.mouse.move(x + jx, y + jy, steps=steps)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self._page.mouse.click(x + jx, y + jy)

    async def human_type(self, text: str) -> None:
        assert self._page
        for char in text:
            await self._page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.04, 0.12))

    async def random_delay(self) -> None:
        delay = random.randint(settings.action_delay_min, settings.action_delay_max) / 1000
        await asyncio.sleep(delay)

    async def wait_for_stable(self, timeout_ms: int = 3000) -> None:
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    # ── cart DOM helpers ──────────────────────────────────────────────────────

    async def get_cart_count(self) -> int:
        """Read cart item count directly from DOM."""
        try:
            count = await self._page.evaluate("""() => {
                // ── Named selectors (Blinkit, Zepto variants) ──────
                const selectors = [
                    // Blinkit
                    '[data-testid="cart-count"]',
                    '[class*="CartCount"]',
                    // Zepto (known class patterns from DOM inspection)
                    '[class*="cart-count"]',
                    '[class*="cartCount"]',
                    '[class*="cart_count"]',
                    '[class*="bagCount"]',
                    '[class*="bag-count"]',
                    '[class*="itemCount"]',
                    '[class*="item-count"]',
                    '[class*="cartBadge"]',
                    '[class*="cart-badge"]',
                    // Generic
                    '.cart-count',
                    '.cart-item-count',
                    '[aria-label*="cart" i] [class*="count"]',
                    '[aria-label*="cart" i] [class*="badge"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        const n = parseInt((el.innerText||'').trim(), 10);
                        if (!isNaN(n) && n > 0) return n;
                    }
                }

                // ── Text-based fallback: "N items" anywhere in cart-related elements ──
                const cartEls = document.querySelectorAll('[class*="cart"], [class*="Cart"], [class*="bag"], [class*="Bag"]');
                for (const el of cartEls) {
                    const m = (el.innerText||'').match(/(\d+)\s*item/i);
                    if (m) return parseInt(m[1], 10);
                }

                // ── Header-badge fallback: small numeric element in viewport header ──
                // Catches Zepto and any platform whose badge doesn't match class patterns.
                // Only looks at the top 150px (header) for small elements (badges).
                const allEls = Array.from(document.querySelectorAll('span,div,sup'));
                for (const el of allEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.top > 150 || rect.width > 48 || rect.height > 48) continue;
                    if (el.offsetParent === null) continue;
                    const t = (el.innerText||'').trim();
                    if (/^\d{1,3}$/.test(t)) {
                        const n = parseInt(t, 10);
                        if (n > 0 && n < 500) return n;
                    }
                }
                return 0;
            }""")
            return count or 0
        except Exception:
            return 0

    async def get_cart_count_from_badge(self) -> int:
        """Alias for get_cart_count; kept for compatibility."""
        return await self.get_cart_count()

    async def get_cart_count_strict(self, platform: str) -> int:
        """
        Platform-aware strict cart count.

        Use this when false positives are costly (notably Zepto), where broad
        text matching like "N items" can pick unrelated labels.
        """
        page = self._page
        if not page:
            return 0
        if platform != "zepto":
            return await self.get_cart_count()
        try:
            count = await page.evaluate("""() => {
                const controls = Array.from(document.querySelectorAll('a,button,div,span')).filter(el => {
                    if (el.offsetParent === null) return false;
                    const txt = (el.innerText || '').toLowerCase();
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    const cls = (el.className || '').toString().toLowerCase();
                    return (
                        txt === 'cart' || txt.includes(' cart') || txt.startsWith('cart ') ||
                        aria.includes('cart') || aria.includes('bag') ||
                        cls.includes('carticon') || cls.includes('cart-icon') || cls.includes('bagicon')
                    );
                });

                for (const c of controls) {
                    const near = [c, ...Array.from(c.querySelectorAll('*'))];
                    for (const el of near) {
                        if (el.offsetParent === null) continue;
                        const t = (el.innerText || '').trim();
                        if (!/^\\d{1,2}$/.test(t)) continue;
                        const n = parseInt(t, 10);
                        if (!isNaN(n) && n >= 0 && n < 100) return n;
                    }
                }
                return 0;
            }""")
            return count or 0
        except Exception:
            return 0

    def is_on_pdp(self, platform: str) -> bool:
        """
        Synchronous URL check — returns True if the current page is a product
        detail page (PDP).  Safe to call from async code without await.
        """
        page = self._page
        if not page:
            return False
        url = page.url or ""
        if platform == "zepto":
            # Zepto PDP URLs: zepto.com/pn/<slug>/pvid/<uuid>
            return "/pn/" in url and "/pvid/" in url
        return False

    async def click_pdp_add_to_cart_cta(self) -> bool:
        """
        Click the full-width 'Add To Cart' CTA on a product detail page.
        Identifies the button by its text ('ADD TO CART' / 'ADD TO BAG') and
        requires a minimum width of 120 px so tiny card ADD buttons are ignored.
        Returns True if the button was found and clicked.
        """
        page = self._page
        if not page:
            return False
        try:
            result = await page.evaluate("""() => {
                const isAddToCart = t => {
                    const up = t.replace(/\\s+/g, ' ').trim().toUpperCase();
                    return up === 'ADD TO CART' || up === 'ADD TO BAG';
                };
                // Check buttons and role-buttons first, then broader divs/spans.
                const pools = [
                    document.querySelectorAll('button, [role="button"]'),
                    document.querySelectorAll('div, span, a'),
                ];
                for (const pool of pools) {
                    for (const el of pool) {
                        if (el.offsetParent === null) continue;
                        if (!isAddToCart(el.innerText || '')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width >= 120 && r.height > 0) {
                            return { found: true, x: r.left + r.width / 2, y: r.top + r.height / 2 };
                        }
                    }
                }
                return { found: false };
            }""")
            if result.get("found"):
                await self.move_and_click(int(result["x"]), int(result["y"]))
                log.info("pdp_cta_clicked")
                return True
            log.warning("pdp_cta_not_found")
            return False
        except Exception as e:
            log.warning("pdp_cta_error", error=str(e))
            return False

    async def go_back(self, timeout: int = 8_000) -> None:
        """Navigate back to the previous page."""
        try:
            await self._page.go_back(wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            log.warning("go_back_failed", error=str(e))

    async def scroll_to_top(self) -> None:
        """Scroll the current page to top (used to recover from recommendation rows)."""
        page = self._page
        if not page:
            return
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.25)
        except Exception as e:
            log.debug("scroll_to_top_failed", error=str(e))

    async def click_and_type(self, x: int, y: int, text: str) -> bool:
        """
        Click at viewport coordinates (x, y), clear any existing text, type
        `text`, then press Enter.  Used for vision-located UI elements so the
        executor never needs DOM selectors to interact with the search bar.
        """
        page = self._page
        assert page
        try:
            await self.move_and_click(x, y)
            await asyncio.sleep(0.4)
            # Clear any existing text.  React controlled inputs ignore
            # Control+a/Delete so we use triple-click (selects all) then
            # JS nativeInputValueSetter to wipe the value and fire an
            # 'input' event that React's synthetic handler picks up.
            await page.mouse.click(x, y, click_count=3)
            await asyncio.sleep(0.15)
            cleared = await page.evaluate("""
                () => {
                    const el = document.activeElement;
                    if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return false;
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (nativeSetter) {
                        nativeSetter.call(el, '');
                    } else {
                        el.value = '';
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """)
            if not cleared:
                # Fallback: keyboard select-all + Backspace
                await page.keyboard.press("Control+a")
                await asyncio.sleep(0.05)
                await page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)
            await self.human_type(text)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            log.info("click_and_type_done", x=x, y=y, text=text)
            return True
        except Exception as e:
            log.debug("click_and_type_failed", x=x, y=y, error=str(e))
            return False

    # ── DOM search / add helpers ───────────────────────────────────────────────

    # Mirrors original orchestrator.py SEARCH_SELECTORS exactly.
    _SEARCH_SELECTORS: dict[str, list[str]] = {
        "blinkit": [
            "input[placeholder*='Search']",
            "input[placeholder*='search']",
            ".search-bar input",
            "[class*='search'] input",
            "[data-testid*='search'] input",
            "input[type='search']",
            "input[type='text']",
        ],
        "zepto": [
            "input[placeholder*='Search']",
            "input[placeholder*='search']",
            "input[type='search']",
            "[class*='search-input']",
            "[class*='SearchInput'] input",
        ],
        "default": [
            "input[placeholder*='Search']",
            "input[placeholder*='search']",
            "input[type='search']",
            "[role='search'] input",
        ],
    }

    # Known pixel coordinates of search bar per platform (1280×800 viewport).
    # From utils/inspect_dom.py: clicking here activates the search input even
    # when CSS selectors fail.
    _SEARCH_BAR_COORDS: dict[str, tuple[int, int]] = {
        "blinkit":   (448, 56),
        "zepto":     (560, 44),
    }

    async def search_via_dom(self, text: str, platform: str = "default") -> bool:
        """
        Type a search query into the platform's search bar, mirroring the
        original codebase (executor.search_via_dom_fallback + orchestrator
        coordinate fallback).

        Strategy 1 — CSS selectors via query_selector (same as original executor).
        Strategy 2 — Click at known pixel coordinates for the platform, then
                     confirm an <input> became active (from inspect_dom.py).
        Strategy 3 — JavaScript activeElement: focus whatever input the browser
                     thinks the user wants, then type.
        """
        page = self._page
        assert page

        await self._dismiss_modal()
        await asyncio.sleep(0.3)

        # ── Strategy 1: CSS selector (mirrors original search_via_dom_fallback) ──
        selectors = self._SEARCH_SELECTORS.get(platform, self._SEARCH_SELECTORS["default"])
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                box = await el.bounding_box()
                if not box or box["width"] < 80:
                    continue
                log.info("search_bar_found_via_selector", sel=sel, platform=platform)
                await el.click()
                await asyncio.sleep(0.3)
                await self._clear_input(page)
                await self.human_type(text)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Enter")
                await self.wait_for_stable(timeout_ms=3000)
                await self.wait_for_search_results(platform, timeout_ms=5000)
                log.info("dom_search_success", text=text, selector=sel, platform=platform)
                return True
            except Exception as e:
                log.debug("dom_search_selector_failed", sel=sel, platform=platform, error=str(e))
                continue

        # ── Strategy 2: click known coordinates → check activeElement ───────
        px, py = self._SEARCH_BAR_COORDS.get(platform, (448, 56))
        try:
            log.info("search_coord_click_attempt", platform=platform, x=px, y=py)
            await page.mouse.click(px, py)
            await asyncio.sleep(0.5)
            active = await page.evaluate("""() => {
                const el = document.activeElement;
                if (!el) return null;
                return {tag: el.tagName, type: el.type, width: el.offsetWidth};
            }""")
            if active and active.get("tag", "").upper() in ("INPUT", "TEXTAREA"):
                log.info("search_bar_activated_via_coords", active=active, platform=platform)
                await self._clear_input(page)
                await self.human_type(text)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Enter")
                await self.wait_for_stable(timeout_ms=3000)
                await self.wait_for_search_results(platform, timeout_ms=5000)
                log.info("dom_search_success", text=text, strategy="coord_click", platform=platform)
                return True
        except Exception as e:
            log.debug("coord_click_failed", platform=platform, error=str(e))

        # ── Strategy 3: JavaScript focus on first visible large input ─────────
        try:
            focused = await page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input'));
                for (const inp of inputs) {
                    if (inp.offsetParent !== null && inp.offsetWidth > 80) {
                        inp.focus();
                        inp.select();
                        return {tag: 'INPUT', type: inp.type, width: inp.offsetWidth};
                    }
                }
                return null;
            }""")
            if focused:
                log.info("search_bar_activated_via_js", result=focused, platform=platform)
                await self._clear_input(page)
                await self.human_type(text)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Enter")
                await self.wait_for_stable(timeout_ms=3000)
                await self.wait_for_search_results(platform, timeout_ms=5000)
                log.info("dom_search_success", text=text, strategy="js_focus", platform=platform)
                return True
        except Exception as e:
            log.debug("js_focus_search_failed", platform=platform, error=str(e))

        log.warning("dom_search_failed", text=text, platform=platform)
        return False

    async def wait_for_search_results(self, platform: str, timeout_ms: int = 5000) -> None:
        """Wait until search results (or a no-results indicator) appear."""
        _RESULT_SELECTORS = {
            "blinkit":   ["[class*='Product']", "[class*='product']",
                          "[class*='tw-relative']", ".product-card",
                          "[class*='search-result']"],
            "zepto":     ["[class*='Product']", "[class*='product']",
                          "[class*='item']", "[class*='result']"],
        }
        selectors = _RESULT_SELECTORS.get(platform, ["[class*='product']"])
        for sel in selectors:
            try:
                await self._page.wait_for_selector(sel, state="attached", timeout=timeout_ms)
                return
            except Exception:
                continue
        # If no product card appeared, just sleep as last resort
        await asyncio.sleep(2.0)

    async def _clear_input(self, page) -> None:
        """
        Reliably clear the currently focused input on React SPAs.

        React uses synthetic events and ignores programmatic `el.value = ''`
        unless it goes through the native input value setter.  Triple-click
        first ensures the caret is inside the field and all text is selected.
        """
        # Triple-click to place cursor and select all existing text
        active = await page.evaluate("() => { const e = document.activeElement; return e ? {x: e.getBoundingClientRect().x + e.offsetWidth/2, y: e.getBoundingClientRect().y + e.offsetHeight/2} : null; }")
        if active:
            await page.mouse.click(active["x"], active["y"], click_count=3)
            await asyncio.sleep(0.1)

        cleared = await page.evaluate("""
            () => {
                const el = document.activeElement;
                if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return false;
                // Use the native setter so React's synthetic onChange fires
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set;
                if (nativeSetter) {
                    nativeSetter.call(el, '');
                } else {
                    el.value = '';
                }
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
        """)
        if not cleared:
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.05)
            await page.keyboard.press("Backspace")
        await asyncio.sleep(0.1)

    async def _dismiss_modal(self) -> None:
        """Best-effort dismissal of any overlay modal."""
        page = self._page
        if not page:
            return
        _CLOSE_SELECTORS = [
            "button[class*='close' i]",
            "button[aria-label*='close' i]",
            "button[aria-label*='dismiss' i]",
            "[data-testid*='close' i]",
            "[class*='modal'] button",
            "[class*='Modal'] button",
            "[class*='overlay'] button",
            "[class*='Overlay'] button",
        ]
        for sel in _CLOSE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    box = await el.bounding_box()
                    if box and box["width"] > 0:
                        await el.click()
                        await asyncio.sleep(0.3)
                        return
            except Exception:
                continue
        # Try Escape as a generic dismissal
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass

    async def click_add_button(self, product_hint: str) -> bool:
        """
        Platform-agnostic DOM-first ADD button click.
        Strategy 1: Blinkit Tailwind cards (tw-* class names).
        Strategy 2: Generic visible 'Add' leaf element search.
        Returns True if clicked (or item already in cart).
        """
        page = self._page
        assert page

        # Dismiss any modal first
        await self._dismiss_modal()
        await asyncio.sleep(0.3)

        import json as _json
        result = await page.evaluate(
            f"""() => {{
            const targetName = {_json.dumps(product_hint.lower())};
            const targetWords = targetName.split(' ').filter(w => w.length > 2);

            // isAdd: matches 'ADD', 'Add', 'add', or buttons whose trimmed text
            // starts with 'add' (e.g. '+ Add', 'Add to cart') and is ≤ 20 chars.
            // Also matches buttons with aria-label='add' or 'add to cart'.
            const isAdd = (el) => {{
                const t = (el.innerText || '').trim();
                const tUp = t.toUpperCase();
                if (tUp === 'ADD') return true;
                if (tUp === '+' || tUp === '＋') return true;
                if (t.length <= 20 && tUp.startsWith('ADD') &&
                    !tUp.includes('ADDRESS') && !tUp.includes('ADDON')) return true;
                const lbl = (
                    el.getAttribute('aria-label') ||
                    el.getAttribute('title') || ''
                ).trim().toUpperCase();
                if (lbl === 'ADD' || lbl === 'ADD TO CART' ||
                    lbl === 'ADD ITEM' || lbl === 'ADD TO BAG') return true;
                return false;
            }};
            const hasAddChild = el => Array.from(el.querySelectorAll('div,button,span')).some(d => isAdd(d));
            // Returns current quantity if stepper present, 0 if no stepper.
            // A real stepper has BOTH a minus AND a plus button visible inside the card.
            const stepperQty = el => {{
                const ch = Array.from(el.querySelectorAll('div,span,button'));
                const minus = ch.some(d => {{ const t=(d.innerText||'').trim(); return (t==='-'||t==='\u2212') && d.offsetParent!==null; }});
                const plus  = ch.some(d => {{ const t=(d.innerText||'').trim(); return (t==='+'||t==='\uff0b'||t==='+') && d.offsetParent!==null; }});
                if (!minus || !plus) return 0;
                // Read the quantity number between the minus and plus
                const qtyEl = ch.find(d => {{ const t=(d.innerText||'').trim(); return /^\d+$/.test(t) && d.offsetParent!==null; }});
                return qtyEl ? parseInt(qtyEl.innerText.trim(), 10) : 1;
            }};
            const hasStepper = el => stepperQty(el) > 0;

            // Strategy 1: Blinkit Tailwind cards
            let bestCard = null, bestScore = 0;
            const twCards = Array.from(document.querySelectorAll('div')).filter(el => {{
                const cls = el.className || '';
                return cls.includes('tw-relative') && cls.includes('tw-flex') && cls.includes('tw-h-full') && cls.includes('tw-flex-col') && el.offsetParent !== null;
            }});
            if (twCards.length > 0) {{
                for (const card of twCards) {{
                    if (!hasAddChild(card)) continue;
                    const txt = (card.innerText||'').toLowerCase();
                    const score = targetWords.filter(w => txt.includes(w)).length;
                    if (score > bestScore) {{ bestScore = score; bestCard = card; }}
                }}
                if (!bestCard) bestCard = twCards.find(c => hasAddChild(c)) || null;
            }}

            // Strategy 2: generic visible 'Add' leaf elements
            if (!bestCard) {{
                const addEls = Array.from(document.querySelectorAll(
                    'button,div[role="button"],span[role="button"],div,span,[class*="add" i],[class*="Add"]'
                )).filter(el => isAdd(el) && el.offsetParent !== null);
                let bestBtn = null; bestScore = 0;
                for (const btn of addEls) {{
                    let container = btn.parentElement;
                    for (let i = 0; i < 8 && container; i++) {{
                        const cls = (container.className||'').toLowerCase();
                        if (cls.includes('product')||cls.includes('item')||cls.includes('card')||container.tagName==='LI'||container.tagName==='ARTICLE') break;
                        container = container.parentElement;
                    }}
                    if (!container) container = btn.parentElement?.parentElement?.parentElement;
                    if (!container) continue;
                    const txt = (container.innerText||'').toLowerCase();
                    const score = targetWords.filter(w => txt.includes(w)).length;
                    if (score > bestScore) {{ bestScore = score; bestCard = container; bestBtn = btn; }}
                }}
                if (!bestCard && addEls.length > 0) {{ bestCard = addEls[0].parentElement; bestBtn = addEls[0]; }}
                if (bestBtn) {{
                    const qty2 = stepperQty(bestCard);
                    if (qty2 > 0) return {{found:false,reason:'already_in_cart',already_added:true,current_qty:qty2}};
                    const rect = bestBtn.getBoundingClientRect();
                    if (rect.width === 0) return {{found:false,reason:'zero_size'}};
                    return {{found:true,centerX:rect.x+rect.width/2,centerY:rect.y+rect.height/2,strategy:'generic'}};
                }}
            }}

            // Strategy 3: class-name based for Zepto (no text match needed)
            if (!bestCard) {{
                const zepto_sels = [
                    '[class*="addButton" i]', '[class*="AddToCart" i]', '[class*="add-to-cart" i]',
                    '[class*="add-item" i]',  '[class*="addItem" i]',   '[class*="atc-btn" i]',
                    '[data-testid*="add" i]', '[data-testid*="Add"]',
                ];
                for (const sel of zepto_sels) {{
                    const candidates = Array.from(document.querySelectorAll(sel))
                        .filter(el => el.offsetParent !== null);
                    if (!candidates.length) continue;
                    // Pick the one whose nearest card contains the most target words.
                    // bestScore starts at -1 so even score=0 cards are accepted.
                    let bestBtn3 = null; bestScore = -1;
                    for (const btn of candidates) {{
                        let container = btn;
                        for (let d = 0; d < 8; d++) {{
                            if (!container.parentElement) break;
                            container = container.parentElement;
                            const txt = (container.innerText||'').toLowerCase();
                            if (txt.length > 20 && txt.length < 1000) {{
                                const score = targetWords.filter(w => txt.includes(w)).length;
                                if (score > bestScore) {{
                                    bestScore = score;
                                    bestBtn3 = btn;
                                    bestCard = container;
                                }}
                                break;
                            }}
                        }}
                    }}
                    // Fallback: if no button scored above -1, use first visible candidate
                    if (!bestBtn3 && candidates.length > 0) {{
                        bestBtn3 = candidates[0];
                        bestCard = bestBtn3.parentElement;
                    }}
                    if (bestBtn3) {{
                        const qty2 = bestCard ? stepperQty(bestCard) : 0;
                        if (qty2 > 0) return {{found:false,reason:'already_in_cart',already_added:true,current_qty:qty2}};
                        const rect = bestBtn3.getBoundingClientRect();
                        if (rect.width > 0) return {{found:true,centerX:rect.x+rect.width/2,centerY:rect.y+rect.height/2,strategy:'zepto_class'}};
                    }}
                }}
            }}

            if (!bestCard) return {{found:false,reason:'no_card'}};
            const qty = stepperQty(bestCard);
            if (qty > 0) return {{found:false,reason:'already_in_cart',already_added:true,current_qty:qty}};
            const addBtn = Array.from(bestCard.querySelectorAll('div,button,span')).find(d => isAdd(d) && d.offsetParent !== null);
            if (!addBtn) return {{found:false,reason:'no_add_btn'}};
            const rect = addBtn.getBoundingClientRect();
            if (rect.width === 0) return {{found:false,reason:'zero_size'}};
            return {{found:true,centerX:rect.x+rect.width/2,centerY:rect.y+rect.height/2,strategy:'tailwind'}};
        }}"""
        )

        if result.get("already_added"):
            log.info("item_already_in_cart", product=product_hint,
                     current_qty=result.get("current_qty", 1))
            return True
        if not result.get("found"):
            log.warning("add_button_not_found", product=product_hint, reason=result.get("reason"))
            return False

        strategy = result.get("strategy", "")
        cx, cy = result["centerX"], result["centerY"]
        await self.move_and_click(int(cx), int(cy))
        await asyncio.sleep(0.5)
        log.info("add_button_clicked", product=product_hint, strategy=strategy)
        return True

    async def click_zepto_add_button(self, product_hint: str, rank: int = 0) -> bool:
        """
        Zepto-specific ADD click on search-results cards.

        Picks visible leaf elements whose text is exactly "ADD", scores them by
        product-card text overlap with `product_hint`, then clicks candidate by
        rank (0=best, 1=next best, ...).  This avoids card-body clicks that
        navigate to PDP.
        """
        page = self._page
        assert page

        await self._dismiss_modal()
        await asyncio.sleep(0.2)

        import json as _json
        result = await page.evaluate(
            f"""() => {{
                const target = {_json.dumps(product_hint.lower())};
                const words = target.split(/\\s+/).filter(w => w.length > 2);
                const pickRank = {int(rank)};

                const addLeaves = Array.from(document.querySelectorAll('button,div,span,a'))
                    .filter(el => el.offsetParent !== null)
                    .filter(el => (el.innerText || '').trim().toUpperCase() === 'ADD')
                    .filter(el => {{
                        const r = el.getBoundingClientRect();
                        return r.width >= 20 && r.width <= 180 && r.height >= 16 && r.height <= 80;
                    }});

                const candidates = [];
                for (const btn of addLeaves) {{
                    let card = btn;
                    for (let i = 0; i < 10 && card.parentElement; i++) {{
                        card = card.parentElement;
                        const txt = (card.innerText || '').toLowerCase();
                        if (txt.length < 30 || txt.length > 1600) continue;
                        const hit = words.filter(w => txt.includes(w)).length;
                        if (hit === 0) continue;
                        const hasPrice = txt.includes('₹') || txt.includes('rs');
                        const score = hit * 10 + (hasPrice ? 1 : 0);
                        const r = btn.getBoundingClientRect();
                        candidates.push({{
                            x: r.left + r.width / 2,
                            y: r.top + r.height / 2,
                            score,
                        }});
                        break;
                    }}
                }}

                if (!candidates.length) return {{ found: false, reason: 'no_ranked_add' }};

                candidates.sort((a, b) => (b.score - a.score) || (a.y - b.y) || (a.x - b.x));
                const idx = Math.min(Math.max(0, pickRank), candidates.length - 1);
                const c = candidates[idx];
                return {{ found: true, x: c.x, y: c.y, score: c.score, idx, total: candidates.length }};
            }}"""
        )

        if not result.get("found"):
            log.warning("zepto_add_button_not_found", product=product_hint, reason=result.get("reason"))
            return False

        await self.move_and_click(int(result["x"]), int(result["y"]))
        log.info(
            "zepto_add_button_clicked",
            product=product_hint,
            rank=result.get("idx"),
            total=result.get("total"),
            score=result.get("score"),
        )
        return True

    async def read_cart_items(self, platform: str) -> list[str]:
        """
        Read product names currently in the cart via DOM injection.
        Called at session start so the loop can skip already-present items.
        """
        page = self._page
        if not page:
            return []
        try:
            cart_count = await self.get_cart_count()
            if cart_count == 0:
                return []
            log.info("reading_existing_cart", platform=platform, count=cart_count)
            js_map = {
                "blinkit": """
() => {
    const names = [];
    // 1. Named selectors used by Blinkit's current React build
    const sels = [
        '[class*="CartItem"] [class*="name"]',
        '[class*="CartItem"] [class*="Name"]',
        '[class*="CartItemDetails"] [class*="product"]',
        '[data-testid="cart-item-name"]',
        '[class*="Product__name"]',
    ];
    for (const sel of sels)
        document.querySelectorAll(sel).forEach(el => {
            const t = (el.innerText || '').trim();
            if (t && t.length > 3 && t.length < 80) names.push(t);
        });

    // 2. Stepper-adjacent name: each visible stepper (- N +) has a sibling
    //    element with the product name.  Walk up from the stepper to the card
    //    and pick the shortest text chunk that isn't price/qty noise.
    if (!names.length) {
        const noise = /^[\u20b9\d\s\/+\-\u00d7xX%gkmlMLKGlL.,]+$/;
        document.querySelectorAll('*').forEach(el => {
            const t = (el.innerText || '').trim();
            if (t !== '-' && t !== '+' && t !== '\u2212' && t !== '\uff0b') return;
            if (el.offsetParent === null) return;
            // Walk up to find a sibling/ancestor text node that looks like a name
            let node = el.parentElement;
            for (let d = 0; d < 6 && node; d++) {
                const candidate = (node.innerText || '').split('\\n')
                    .map(s => s.trim())
                    .filter(s => s.length > 3 && s.length < 80 && !noise.test(s));
                if (candidate.length > 0) {
                    names.push(candidate[0]);
                    break;
                }
                node = node.parentElement;
            }
        });
    }
    return [...new Set(names)];
}
""",
                "zepto": """
() => {
    const names = [];
    document.querySelectorAll('[data-testid*="product-name"],[class*="productName"],[class*="ProductName"]')
        .forEach(el => { const t=(el.innerText||'').trim(); if (t&&t.length>2) names.push(t); });
    return [...new Set(names)];
}
""",
                "default": """
() => {
    const names = [];
    ['[class*="cart"] [class*="name"]','[class*="Cart"] [class*="name"]'].forEach(sel =>
        document.querySelectorAll(sel).forEach(el => {
            const t=(el.innerText||'').trim();
            if (t&&t.length>2&&t.length<80) names.push(t);
        })
    );
    return [...new Set(names)];
}
""",
            }
            js = js_map.get(platform, js_map["default"])
            raw: list = await page.evaluate(js)
            items = [s.strip() for s in (raw or []) if len(s.strip()) > 2]
            log.info("existing_cart_items_read", platform=platform, items=items[:10])
            return items
        except Exception as e:
            log.warning("read_cart_items_failed", platform=platform, error=str(e))
            return []

    async def increment_quantity_stepper(
        self,
        times: int = 1,
        product_hint: str = "",
        allow_unknown_qty: bool = False,
    ) -> bool:
        """
        Click the '+' stepper button N times.

        Ported from original _click_plus_dom: card-first strategy (find smallest
        DOM element containing the product name, then find the last incrementor
        element inside it). scrollIntoView is called INSIDE the JS evaluate so
        getBoundingClientRect() gives fresh in-viewport coords with no async gap.

        Three attempts per click with progressive 0.5s/1.0s/2.0s backoff.
        Verifies stepper qty actually incremented before moving to next click.
        """
        page = self._page
        assert page

        # Let the DOM settle after ADD click before touching the stepper
        await asyncio.sleep(2.0)  # increased from 1.5 — Zepto stepper animates

        for i in range(times):
            expected_qty = i + 2   # after this click, qty should be i+2
            click_ok = False

            for wait_s in (0.5, 1.0, 2.0):
                await asyncio.sleep(wait_s)

                coords = await page.evaluate(
                    """
                    (productHint) => {
                        const words = productHint.toLowerCase()
                            .split(/\\s+/)
                            .filter(w => w.length > 2);
                        const needed = Math.min(2, words.length);
                        if (needed === 0) return null;

                        // isIncrement: recognises '+' buttons by text, aria-label,
                        // CSS class, or the sibling-heuristic (prev sibling is '-').
                        // Does NOT rely on offsetParent so it works in fixed/sticky cards.
                        const isIncrement = (el) => {
                            const t = (el.innerText || el.textContent || '')
                                .replace(/\\s+/g, '').trim();
                            const lbl = (
                                el.getAttribute('aria-label') ||
                                el.getAttribute('title') || ''
                            ).toLowerCase();
                            const cls = (el.getAttribute('class') || '').toLowerCase();

                            if (t === '+' || t === '\\uFF0B' || t === '\\u2795') return true;
                            if (t.length <= 3 && t.includes('+')) return true;
                            if (lbl.includes('increment') || lbl.includes('increase') ||
                                lbl.includes('plus') || lbl.includes('add more')) return true;
                            if (cls.includes('increment') || cls.includes('increase') ||
                                cls.includes('plus')) return true;

                            // Sibling heuristic: last button whose preceding sibling
                            // contains '-' is the '+' in a '− N +' stepper.
                            if (el.tagName === 'BUTTON' ||
                                el.getAttribute('role') === 'button') {
                                const parent = el.parentElement;
                                if (parent) {
                                    const siblings = [...parent.children];
                                    const idx = siblings.indexOf(el);
                                    for (let k = 0; k < idx; k++) {
                                        const st = (siblings[k].innerText ||
                                            siblings[k].textContent || '')
                                            .replace(/\\s+/g, '').trim();
                                        if (st === '-' || st === '\\u2212' ||
                                            st === '\\u2796') return true;
                                    }
                                }
                            }
                            return false;
                        };

                        // Scroll element into view and return fresh coords atomically.
                        const scrollAndCoords = (el) => {
                            el.scrollIntoView({ block: 'center', behavior: 'instant' });
                            const r = el.getBoundingClientRect();
                            return (r.width > 0 && r.height > 0)
                                ? { x: r.left + r.width / 2, y: r.top + r.height / 2 }
                                : null;
                        };

                        // Strategy A: card-first.  Find smallest element whose
                        // innerText contains the product name, then take the LAST
                        // incrementor inside it (the '+' in '− N +' is always last).
                        // Try with `needed` words first; fall back to 1 word.
                        const allNodes = [...document.querySelectorAll('*')];
                        for (let minWords = needed; minWords >= 1; minWords--) {
                            const cards = allNodes.filter(el => {
                                const txt = (el.innerText || '').toLowerCase();
                                return txt.length > 0 && txt.length < 600 &&
                                    words.filter(w => txt.includes(w)).length >= minWords;
                            });
                            cards.sort((a, b) =>
                                (a.innerText || '').length - (b.innerText || '').length
                            );
                            for (const card of cards.slice(0, 8)) {
                                const incs = [...card.querySelectorAll('*')]
                                    .filter(isIncrement);
                                if (incs.length > 0) {
                                    const btn = incs[incs.length - 1];  // last = '+'
                                    const pt = scrollAndCoords(btn);
                                    if (pt) return { ...pt, strategy: 'A' + minWords };
                                }
                            }
                        }

                        // Strategy B: button-first walk-up.  Find all '+' elements,
                        // walk up the DOM until an ancestor contains at least 1 word.
                        for (const btn of [...document.querySelectorAll(
                            'button, [role="button"], div, span, a'
                        )].filter(isIncrement)) {
                            let el = btn.parentElement;
                            for (let d = 0; d < 10; d++) {
                                if (!el) break;
                                const txt = (el.innerText || '').toLowerCase();
                                if (txt.length > 0 && txt.length < 800 &&
                                    words.filter(w => txt.includes(w)).length >= 1) {
                                    const pt = scrollAndCoords(btn);
                                    if (pt) return { ...pt, strategy: 'B' };
                                }
                                el = el.parentElement;
                            }
                        }

                        // Strategy C: class-name based — Zepto steppers
                        // have class names like incrementButton, plusButton, etc.
                        const incSels = [
                            '[class*="increment" i]', '[class*="plus" i]',
                            '[class*="increase" i]', '[class*="stepperPlus" i]',
                            '[class*="stepper-plus" i]', '[class*="qty-plus" i]',
                            '[class*="qtyPlus" i]',   '[data-testid*="increment" i]',
                            '[data-testid*="plus" i]',
                        ];
                        for (const sel of incSels) {
                            for (const btn of document.querySelectorAll(sel)) {
                                if (btn.offsetParent === null) continue;
                                const pt = scrollAndCoords(btn);
                                if (pt) return { ...pt, strategy: 'C_' + sel };
                            }
                        }
                        return null;
                    }
                    """,
                    product_hint,
                )

                if not coords:
                    log.debug("stepper_plus_not_found_attempt",
                              product=product_hint, click=i + 1, wait_s=wait_s)
                    continue

                await self.move_and_click(int(coords["x"]), int(coords["y"]))
                await asyncio.sleep(0.4)

                # Verify the qty actually went up before claiming success
                actual_qty = await self._get_stepper_qty(product_hint)
                if (actual_qty is not None and actual_qty >= expected_qty) or (
                    allow_unknown_qty and actual_qty is None
                ):
                    log.debug("stepper_incremented",
                              product=product_hint, click=i + 1,
                              strategy=coords.get("strategy"), qty=actual_qty)
                    click_ok = True
                    break
                log.debug("stepper_click_not_registered",
                          product=product_hint, expected=expected_qty,
                          actual=actual_qty, wait_s=wait_s)

            if not click_ok:
                log.warning("stepper_plus_not_found",
                            product=product_hint, attempt=i + 1)
                return False

            await asyncio.sleep(0.3)
        return True

    async def _get_stepper_qty(self, product_hint: str):
        """
        Read the current stepper quantity for the product matching product_hint.
        Returns int if found, None otherwise (caller skips verification).
        """
        page = self._page
        if not page:
            return None
        try:
            return await page.evaluate(
                """
                (hint) => {
                    const words = hint.toLowerCase()
                        .split(/\\s+/).filter(w => w.length > 2);
                    const needed = Math.min(2, words.length || 1);

                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };

                    const hasStepperGlyphs = (root) => {
                        const nodes = [root, ...Array.from(root.querySelectorAll('*'))];
                        let hasMinus = false;
                        let hasPlus = false;
                        for (const n of nodes) {
                            if (!isVisible(n)) continue;
                            const t = (n.innerText || n.textContent || '').replace(/\\s+/g, '').trim();
                            if (t === '-' || t === '\\u2212' || t === '\\u2796') hasMinus = true;
                            if (t === '+' || t === '\\uFF0B' || t === '\\u2795') hasPlus = true;
                            if (hasMinus && hasPlus) return true;
                        }
                        return false;
                    };

                    const nums = Array.from(document.querySelectorAll('div,span,button'))
                        .filter(isVisible)
                        .map(el => ({
                            el,
                            txt: (el.innerText || el.textContent || '').trim()
                        }))
                        .filter(x => /^\\d{1,2}$/.test(x.txt));

                    for (let minW = needed; minW >= 1; minW--) {
                        for (const n of nums) {
                            const qty = parseInt(n.txt, 10);
                            if (Number.isNaN(qty) || qty < 1 || qty > 50) continue;
                            let a = n.el;
                            for (let d = 0; d < 10 && a; d++) {
                                const txt = (a.innerText || '').toLowerCase();
                                if (txt.length > 0 && txt.length < 1800) {
                                    const hit = words.filter(w => txt.includes(w)).length;
                                    if (hit >= minW && hasStepperGlyphs(a)) return qty;
                                }
                                a = a.parentElement;
                            }
                        }
                    }
                    return null;
                }
                """,
                product_hint,
            )
        except Exception:
            return None

    async def scroll_cart_container(self, scroll_y: int) -> dict:
        """
        Find the scrollable cart container (drawer or page) and scroll it to
        scroll_y.  Returns info about what was found so the caller can plan.

        Strategy:
          1. Try known class-name patterns for Blinkit/Zepto drawers.
          2. If none found, pick the tallest element with overflow scroll/auto.
          3. Fall back to window.scrollTo as last resort.
        """
        try:
            result = await self._page.evaluate("""
                (scrollY) => {
                    // Known cart container class patterns for quick-commerce platforms
                    const ROOT_SELS = [
                        '[class*="CartPage"]', '[class*="cart-page"]',
                        '[class*="CartDrawer"]', '[class*="cart-drawer"]',
                        '[class*="CartBody"]',  '[class*="cart-body"]',
                        '[class*="MyCart"]',    '[class*="my-cart"]',
                        '[class*="CartSheet"]', '[class*="cart-sheet"]',
                        '[class*="CartContainer"]',
                        // Zepto common patterns
                        '[class*="checkout" i]', '[class*="Checkout" i]',
                        '[class*="bottom-sheet" i]', '[class*="BottomSheet" i]',
                        '[class*="drawer" i]', '[class*="Drawer" i]',
                        '[class*="bill" i]', '[class*="Bill" i]',
                        '[class*="summary" i]', '[class*="Summary" i]',
                        'aside[class*="cart"]', '[data-testid*="cart"]',
                    ];

                    // Collect candidates: scrollable elements inside cart roots
                    const candidates = [];
                    for (const sel of ROOT_SELS) {
                        const root = document.querySelector(sel);
                        if (!root) continue;
                        // The root itself might be the scroll container
                        const rs = getComputedStyle(root);
                        if ((rs.overflowY === 'auto' || rs.overflowY === 'scroll')
                                && root.scrollHeight > root.clientHeight + 40) {
                            candidates.push(root);
                        }
                        // Or a direct child might be
                        for (const child of root.querySelectorAll('*')) {
                            const cs = getComputedStyle(child);
                            if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                                    && child.scrollHeight > child.clientHeight + 40
                                    && child.clientHeight > 150) {
                                candidates.push(child);
                            }
                        }
                    }

                    // Global fallback: any tall scrollable element
                    if (candidates.length === 0) {
                        for (const el of document.querySelectorAll('*')) {
                            const s = getComputedStyle(el);
                            if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                                    && el.scrollHeight > el.clientHeight + 100
                                    && el.clientHeight > 200) {
                                candidates.push(el);
                            }
                        }
                    }

                    if (candidates.length === 0) {
                        // Last resort: scroll the window
                        window.scrollTo(0, scrollY);
                        return {
                            method: 'window',
                            scrollHeight: Math.max(document.body.scrollHeight,
                                                   document.documentElement.scrollHeight),
                            clientHeight: window.innerHeight,
                        };
                    }

                    // Pick the most likely cart scroller, not just the tallest wrapper.
                    const score = (el) => {
                        const txt = (el.innerText || '').toLowerCase();
                        const cls = (el.className || '').toString().toLowerCase();
                        const style = getComputedStyle(el);
                        const overflow = (style.overflowY === 'auto' || style.overflowY === 'scroll') ? 3 : 0;
                        const cartHints = (
                            (txt.includes('bill summary') ? 8 : 0) +
                            (txt.includes('to pay') ? 8 : 0) +
                            (txt.includes('delivery') ? 2 : 0) +
                            (txt.includes('missed something') ? 2 : 0) +
                            (cls.includes('cart') ? 3 : 0) +
                            (cls.includes('checkout') ? 3 : 0) +
                            (cls.includes('drawer') ? 2 : 0)
                        );
                        // Penalize giant app wrappers that include the whole page.
                        const giganticPenalty = el.scrollHeight > 12000 ? -8 : 0;
                        const viewportPenalty = el.clientHeight > window.innerHeight * 0.95 ? -3 : 0;
                        const scrollable = Math.max(0, Math.min(12, Math.floor((el.scrollHeight - el.clientHeight) / 300)));
                        return overflow + cartHints + scrollable + giganticPenalty + viewportPenalty;
                    };

                    const best = candidates.reduce((a, b) =>
                        score(a) >= score(b) ? a : b
                    );
                    best.scrollTo({ top: scrollY, behavior: 'instant' });
                    return {
                        method: 'element',
                        scrollHeight: best.scrollHeight,
                        clientHeight: best.clientHeight,
                        tagName: best.tagName,
                        className: best.className.toString().substring(0, 100),
                    };
                }
            """, scroll_y)
            return result or {"method": "window", "scrollHeight": 0, "clientHeight": 0}
        except Exception as e:
            log.warning("scroll_cart_container_failed", error=str(e))
            try:
                await self._page.evaluate(f"window.scrollTo(0, {scroll_y})")
            except Exception:
                pass
            return {"method": "window_fallback", "scrollHeight": 0, "clientHeight": 0}

    async def navigate_to_cart(self, platform: str) -> bool:  # noqa: E303
        """Navigate to the cart page via DOM click or URL fallback."""
        _CART_URLS = {
            "blinkit":   "https://blinkit.com/cart",
            "zepto":     "https://www.zeptonow.com/cart",
        }
        await asyncio.sleep(0.8)
        clicked = await self._page.evaluate("""() => {
            const sels = ['a[href*="/cart"]','[aria-label*="cart" i]','[data-testid*="cart" i]','[class*="CartIcon" i]'];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0) { el.click(); return sel; }
                }
            }
            return null;
        }""")
        if clicked:
            await self.wait_for_stable()
            return True
        url = _CART_URLS.get(platform)
        if url:
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=10_000)
                await self.wait_for_stable()
                return True
            except Exception as e:
                log.warning("cart_url_nav_failed", url=url, error=str(e))
        return False

    # ── session ───────────────────────────────────────────────────────────────

    async def save_session(self, platform: str) -> None:
        if self._context and self._session_file:
            await self._context.storage_state(path=str(self._session_file))
            log.info("session_saved", platform=platform)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("browser_stopped")
