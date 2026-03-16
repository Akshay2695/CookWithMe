"""
Fused Vision Agent — Gemini implementation
-------------------------------------------
Makes a single, rich vision call per item at the decision point where the
LLM is genuinely needed: after search results load and we must pick a
product + plan the add-to-cart action.

Architecture benefit
--------------------
The original system made 5 separate vision calls per sub-action:
  1. CartChecker  (is the item already in cart?)
  2. StateEvaluator (anomaly detection)
  3. OrchestratorAgent (next-action planner)
  4. ProductSelectorAgent (best variant + combination plan)
  5. VerifierAgent (did the action succeed?)

This agent replaces all five with ONE call per item:
  • Called only when DOM cannot determine what to do (search results page).
  • All other state (cart badge, stepper, search bar) is read from the DOM
    at zero LLM cost by the CoreLoop.
  • Returns: page_state + selected_product + combination_plan + action +
    quantity_reasoning + confidence.
"""

from __future__ import annotations

from typing import Literal, Optional

import structlog
from pydantic import BaseModel, Field

from gemini.config.settings import settings
from gemini.core.client import get_client
from gemini.core.models import AgentContext

log = structlog.get_logger()


# ── Output model ──────────────────────────────────────────────────────────────

class PackUnit(BaseModel):
    pack_name: str        # e.g. "Amul Full Cream Milk 1L"
    pack_size_ml_or_g: int
    units_needed: int
    price_each: Optional[str] = None


class VisiblePack(BaseModel):
    """One product card visible on the search-results page."""
    name: str
    pack_size: str           # as shown on card, e.g. "500ml", "1L", "5 kg"
    pack_size_ml_or_g: int   # numeric base unit for comparison
    price: str               # e.g. "₹49"
    mrp: Optional[str] = None
    discount_pct: Optional[int] = None
    is_available: bool = True


class FusedAnalysis(BaseModel):
    """
    Combined result of: state evaluation + product selection + action planning.
    Returned by a single vision call at the search-results stage.
    """
    page_state: Literal[
        "homepage", "search_results", "product_page", "cart_view",
        "login_wall", "captcha", "error", "other"
    ]
    item_already_in_cart: bool = False

    # ALL product cards on screen — required, even ones not chosen
    all_visible_packs: list[VisiblePack] = Field(default_factory=list)

    # Product selection (populated when page_state == "search_results")
    selected_product: Optional[str] = None      # display name of chosen product
    selected_pack_size: Optional[str] = None    # e.g. "500g", "1L"
    selected_price: Optional[str] = None        # e.g. "₹45"
    combination_plan: list[PackUnit] = Field(default_factory=list)
    needs_scroll: bool = False                  # scroll down to see more variants?
    no_relevant_product: bool = False           # item genuinely unavailable

    # How much of this item is already in cart (from a visible stepper)
    # = stepper_count × pack_size_base_units. 0 when no stepper is visible.
    cart_quantity_for_item: int = 0

    # Quantity mismatch signals
    quantity_mismatch_note: str = ""   # e.g. "Needed 100ml. Only 1L available."
    mismatch_ratio: float = 1.0        # chosen_total / target; >3.0 or <0.4 = warning

    # What the executor should do next
    next_action: Literal[
        "click_add",       # DOM click — executor handles without LLM
        "open_search",     # navigate to search bar
        "scroll_for_more", # scroll down before deciding
        "done",            # item added / already in cart
        "substitute",      # no match found — trigger substitution
    ] = "click_add"

    confidence: float = 0.7
    reasoning: str = ""

    # Normalised pixel click target for the ADD button of the selected product.
    # x_norm = pixel_x / 1280,  y_norm = pixel_y / 800  (viewport 1280×800).
    # Null when item is already in cart or Add button not clearly visible.
    add_button_x_norm: Optional[float] = None
    add_button_y_norm: Optional[float] = None

    # Explicit quantity math shown by the model (for logging + debugging).
    quantity_reasoning: Optional[dict] = None


# ── System instruction — rich platform knowledge baked in ────────────────────

_SYSTEM = """\
You are an expert grocery shopping vision agent for Indian quick-commerce platforms.
Analyse the screenshot carefully and return a complete, accurate JSON plan.

PLATFORM LAYOUTS
────────────────
Blinkit:
  - Bright green header bar (#0c831f). Logo top-left.
  - Search bar centred near the top (x≈0.42, y≈0.06).
  - Product cards arranged in a 3–4 column grid.
  - Each card has a green "Add" button (bottom-right of card).
  - Once a product is added, the "Add" button is replaced by a stepper (− qty +).
  - Cart icon is top-right with a badge showing the item count.
  - Discount tags shown in red/orange on the card image.

Zepto:
  - Dark navy/black header. Logo top-left.
  - Search icon or bar at the top centre.
  - Product cards in a 2-column grid with purple/pink "Add" buttons.
  - Price is shown in bold; MRP is crossed out if discounted.
  - Stepper appears inline after add.

BRAND NEUTRALITY — MANDATORY
─────────────────────────────
Do NOT favour any brand unless the prompt explicitly lists "User preferred brands".
When no brand preference is given:
  • Choose purely on size match, availability, and price.
  • Do NOT default to popular brands just because they are well-known
    (e.g. don't always pick "India Gate" rice or "Amul" for every dairy product).
  • Two products of similar type: prefer the one whose pack size is closest
    to the requested quantity, or the cheaper option if sizes are equal.
When preferred brands ARE listed:
  • Prefer that brand IF its pack size is within 50% of the target.
  • Do NOT override quantity logic — a 1 kg brand-preferred pack is NOT better
    than a 500 g generic pack when the target is 500 g.

STRUCTURED OUTPUT — MANDATORY FIELDS
─────────────────────────────────────
You MUST populate ALL of the following:

1. all_visible_packs — List EVERY product card on screen, including ones you did
   NOT choose. For each card capture:
     name              (product name as shown, e.g. "Amul Fresh Cream")
     pack_size         (as shown, e.g. "1L", "500ml", "200ml")
     pack_size_ml_or_g (integer base unit: ml for liquids, g for solids,
                       COUNT for eggs/pcs/pieces/dozen → use piece count as integer,
                       e.g. "6 pcs" → 6, "12 pcs" → 12, "1 dozen" → 12)
     price             (e.g. "₹49")
     mrp               (crossed-out original price if shown, else omit)
     discount_pct      (integer e.g. 20 for "20% OFF", else omit)
     is_available      (false if "Out of Stock" shown, else true)

2. selected_product, selected_pack_size, selected_price
   — The single product card you recommend adding.

3. combination_plan — How many packs to add to meet the target:
     pack_name        (product name)
     pack_size_ml_or_g (integer base unit — same rules as above including count)
     units_needed     (integer ≥1)
     price_each       (per-pack price string)
   Always at least one entry when a product is being added.

4. quantity_mismatch_note — REQUIRED when chosen pack differs significantly
   from what was requested. Use format:
     "Needed {target}. Only {chosen_pack_size} packs available. Added {chosen_pack_size}."
   Leave as empty string when the chosen pack is a reasonable match (ratio 0.5–2×).

5. mismatch_ratio — float: chosen_total_base / target_base (1.0 = exact).
   Examples:  100ml needed, 1L chosen → 1000/100 = 10.0 (severe overshoot)
              500g needed, 400g chosen → 400/500 = 0.8  (slight undershoot, OK)
              2kg needed, 4×500g → 2000/2000 = 1.0 (exact match)

6. needs_scroll — true ONLY when:
   - Fewer than 4 distinct pack sizes visible AND
   - Target quantity does not match any visible pack within a 2× ratio.
   Set next_action="scroll_for_more" in this case.

PRODUCT SELECTION RULES
───────────────────────
1. Name match: prefer exact name match. Accept brand variants only if the product
   type is correct and no closer size option exists.

2. QUANTITY MATH — follow all 6 steps in strict order whenever a target
   quantity is provided. Show the arithmetic explicitly in reasoning.

   STEP 1 — CONVERT to a base unit:
     Liquids → ml     (1 L = 1000 ml, 2.5 L = 2500 ml, 100 ml = 100 ml)
     Solids  → g      (1 kg = 1000 g, 2 kg = 2000 g, 500 g = 500 g)
     Counted items → pieces  (1 dozen = 12, 6 pcs = 6, 10 pcs = 10, 30 pcs = 30)
     Examples: target="1 dozen eggs", pack="6 pcs" → target_base=12, pack_base=6 → 2 units needed

   STEP 2 — LIST every visible pack by reading all_visible_packs.

   STEP 3 — SCORE each pack vs the target (EXACT MATCH WINS):
     ╔══════════════════════════════════════════════════════════╗
     ║ GOLDEN RULE: if a pack is within 10% of target (90–110%)║
     ║ ALWAYS use 1× that pack — never pick a larger option.   ║
     ╚══════════════════════════════════════════════════════════╝
     Exact / near-exact (90–110% of target):    score = 2.0  ← ALWAYS first choice
     Slightly under (75–89%):                   score = 1.2  ← 1 unit is fine
     Slight overshoot (111–150%):               score = 0.8  ← use 1 unit
     Large overshoot (151–300%):                score = 0.3  ← flag mismatch
     Severe overshoot (> 300%):                 score = 0.1  ← MUST flag mismatch
     Way under (< 50%):                         score = 0.2  ← need multiple units

     CRITICAL EXAMPLES — memorise these:
       target=500g, visible=[500g, 1kg]            → pick 500g (score 2.0)  NOT 1kg (0.3)
       target=1kg,  visible=[900g, 1.5kg]          → pick 900g (score 1.2)  NOT 1.5kg (0.8)
       target=1kg,  visible=[500g, 1kg, 2kg]       → pick 1kg  (score 2.0)
       target=100ml,visible=[200ml, 1L]            → pick 200ml (score 0.8), flag mismatch
       target=1 dozen eggs (=12 pcs), visible=[6 pcs, 12 pcs]  → pick 12 pcs (score 2.0) NOT 6 pcs (score 0.2)
       target=1 dozen eggs (=12 pcs), visible=[6 pcs ONLY]     → pick 6 pcs, units_needed=2 (score 0.2, way under, 6/12=50%)

   STEP 4 — COMBINATION MATH using the HIGHEST-SCORED pack:
     Use the best-scored pack from STEP 3.
     Only add multiple units when the best single pack scores ≤ 1.2:
       target=500g, best=500g  → 1×500g ✓ (NEVER use 2×250g instead)
       target=1kg,  best=500g  → 2×500g ✓ (no 1kg available)
       target=1kg,  best=900g  → 1×900g ✓ (DO NOT add 2×900g = 1.8kg waste)
     Write the arithmetic and verify sum ≥ target.

   STEP 5 — BUILD combination_plan (always at least one entry).
     VERIFY: sum(pack_size_ml_or_g × units_needed) ≥ target_base.

   STEP 6 — POPULATE quantity_reasoning dict AND quantity_mismatch_note:
     Weight example:
     { "target": "1kg", "target_base": 1000, "unit": "g",
       "packs_visible": [{"name": "Chicken 500g", "size_g": 500}, {"name": "Chicken 1kg", "size_g": 1000}],
       "best_pack": "Chicken 1kg (score 2.0, exact match)",
       "chosen": "1 × 1kg pack", "units_needed": 1 }
     Count example:
     { "target": "1 dozen eggs", "target_base": 12, "unit": "pcs",
       "packs_visible": [{"name": "Eggs 6 pcs", "size_pcs": 6}, {"name": "Eggs 12 pcs", "size_pcs": 12}],
       "best_pack": "Eggs 12 pcs (score 2.0, exact match: 12/12=100%)",
       "chosen": "1 × 12 pcs pack", "units_needed": 1 }

3. Unavailability:
   - Set no_relevant_product=true ONLY if no matching product exists anywhere
     on screen and scrolling is unlikely to help.
   - Set needs_scroll=true if results are partial and more variants may appear.

4. item_already_in_cart — QUANTITY-AWARE (do NOT simply set true on any stepper):

   When a stepper (− N +) is visible for this product, compute:
     already_in_cart_base = stepper_count (N) × pack_size_base_units

   • already_in_cart_base >= target_base  → item_already_in_cart=True
       (target is FULLY met, no more action needed)
       Set cart_quantity_for_item = already_in_cart_base.

   • already_in_cart_base < target_base   → item_already_in_cart=False
       (quantity is INSUFFICIENT — must add the remaining amount)
       Set cart_quantity_for_item = already_in_cart_base.
       remaining_base = target_base - already_in_cart_base.
       Build combination_plan ONLY for the REMAINING amount.

   • No stepper visible → item_already_in_cart=False, cart_quantity_for_item=0.
       Build combination_plan normally from search results.

   MANDATORY EXAMPLES (eggs, target = 1 dozen = 12 pcs):
     Stepper "1" on 6-pcs pack → 6 pcs in cart, need 6 more
       item_already_in_cart=False, cart_quantity_for_item=6
       combination_plan: [{pack_name="...", pack_size_ml_or_g=6, units_needed=1}]  (← adds 1 more 6-pcs)
     Stepper "2" on 6-pcs pack → 12 pcs in cart, target met
       item_already_in_cart=True, cart_quantity_for_item=12
     Stepper "1" on 12-pcs pack → 12 pcs in cart, target met
       item_already_in_cart=True, cart_quantity_for_item=12
     No stepper visible → item_already_in_cart=False, cart_quantity_for_item=0

   Always include cart_quantity_for_item in your quantity_reasoning dict.

5. Confidence: 0.0–1.0. A clearly visible product with visible Add button = 0.95+.

6. reasoning: show the scoring table and selection decision, e.g.:
   "target=500g. Visible: [500g=score2.0, 1kg=score0.3]. Pick 500g. 1×500g=500g ✓"

ADD-BUTTON COORDINATES
──────────────────────
Always populate add_button_x_norm and add_button_y_norm:
- Fraction of viewport: x_norm = pixel_x / 1280, y_norm = pixel_y / 800.
- Point to the centre of the "Add" button for the FIRST product in combination_plan.
- Leave both null if item is already in cart or no Add button is visible.

⚠️  ZEPTO ADD-BUTTON — READ THIS CAREFULLY:
  On Zepto search results each product card is a 2-column grid cell.
  The entire card BACKGROUND is a clickable link to the product detail page.
  The ADD button is a SEPARATE element at the BOTTOM-RIGHT of the card,
  displayed as a small green or pink rectangular button labelled "ADD" or "+".
  You MUST report the pixel coordinates of THAT button, NOT the card centre,
  NOT the product image, NOT the product name text.
  Clicking anywhere else on the card will open the product page instead.
  Typical Zepto ADD button position within a card: x ≈ card_right - 30px,
  y ≈ card_bottom - 20px.  Normalise to the full 1280×800 viewport.
"""


# ── Search-bar locator model + prompt ────────────────────────────────────────

class SearchBarLocation(BaseModel):
    found: bool = False
    x_norm: float = 0.35   # fraction of viewport width  (1280px)
    y_norm: float = 0.07   # fraction of viewport height (800px)
    confidence: float = 0.5
    reasoning: str = ""


_SEARCH_BAR_SYSTEM = """\
You are a UI element locator for Indian quick-commerce web apps in a 1280×800 browser.
Your ONLY task: find the search bar (or search icon that opens a text input) and return
where to click it. Normalise coordinates to the viewport:
  x_norm = pixel_x / 1280
  y_norm = pixel_y / 800

PLATFORM HINTS
──────────────
Blinkit  : white/grey rectangular search box centred inside the green header.
           Typical position: x_norm ≈ 0.35, y_norm ≈ 0.07
Zepto    : search bar or magnifying-glass icon in the dark navy header.
           Typical position: x_norm ≈ 0.44, y_norm ≈ 0.06

Return found=true and the exact normalised centre of the search input/icon.
Return found=false only if the page has no search UI at all (e.g. login wall).
"""


# ── Visual verify — did the ADD actually work? ────────────────────────────────

class VerifyAddResult(BaseModel):
    success: bool = False
    signal: str = "no_change"   # stepper_appeared | cart_incremented | toast_appeared | no_change
    cart_count_after: int = 0
    observed: str = ""          # human-readable description of what changed
    retry_instruction: str = "" # filled only when success=False


# ── Cart analysis — read what's actually in the cart ─────────────────────────

class CartLineItem(BaseModel):
    product_name: str
    quantity: str = ""          # units in cart e.g. "2"
    pack_description: str = ""  # e.g. "1 kg × 2"
    unit_price: Optional[str] = None   # e.g. "₹27"
    mrp: Optional[str] = None          # original MRP e.g. "₹35" (crossed out)
    total_price: Optional[str] = None  # e.g. "₹54"
    savings: Optional[str] = None      # e.g. "₹16" saved on this line


class CartAnalysis(BaseModel):
    items: list[CartLineItem] = []
    items_subtotal: Optional[str] = None  # subtotal before fees e.g. "₹152"
    delivery_charge: Optional[str] = None # e.g. "₹30"
    handling_charge: Optional[str] = None # e.g. "₹11"
    platform_fee: Optional[str] = None    # e.g. "₹3"
    total_savings: Optional[str] = None   # "Your total savings ₹68"
    cart_total: Optional[str] = None      # grand total e.g. "₹193"
    item_count: int = 0
    has_more_items_below: bool = False     # True when list is cut off and more items exist below
    # Serviceability + delivery info
    is_serviceable: bool = True           # False when "Cart is Unserviceable" / delivery not available
    delivery_time: str = ""               # e.g. "8 mins", "10-15 mins"
    late_night_fee: str = ""              # e.g. "₹15" (odd-hours surcharge)
    surge_charge: str = ""               # e.g. "₹10" (high demand fee)
    reasoning: str = ""


_CART_SYSTEM = """\
You are analysing a cart/basket page screenshot from an Indian quick-commerce app
(Blinkit or Zepto). The cart may appear as a SLIDE-IN DRAWER
(right side panel) or a full dedicated cart page — both are valid.

This screenshot captures ONE scroll position. Extract every product VISIBLE here.
Items from other scroll positions will be merged externally — do not worry about
items that are off-screen above or below.

CRITICAL — has_more_items_below
────────────────────────────────
Set has_more_items_below = true when ANY of these are true:
  • The item list is cut off at the bottom (last item is partially visible)
  • The "Bill Details" / "Proceed to Pay" / grand total section is NOT visible
  • A scroll bar is visible and the thumb is not at the bottom
  • The cart badge / header shows more items than you can count in this view
Set has_more_items_below = false when:
  • The "Proceed to Pay" button OR "Bill Details" section IS visible
  • The list ends cleanly before the bottom of the viewport
  • This is clearly the last screen of the cart

For each VISIBLE cart item extract:
  product_name    — exact product name as shown  (e.g. "Onion (Kanda)")
  quantity        — number of units in the cart  (e.g. "2")
  pack_description — pack size × qty            (e.g. "1 kg × 2",  "500 ml × 1")
  unit_price      — price for a SINGLE pack      (e.g. "₹27")
  mrp             — original MRP if crossed out  (e.g. "₹35")
  total_price     — line-item total for all units (e.g. "₹54")
  savings         — discount amount on this line if shown (e.g. "₹16")

Bill Details section (extract IF visible in this screenshot):
  items_subtotal  — subtotal of all items before fees (e.g. "₹152")
  delivery_charge — delivery fee shown             (e.g. "₹30"; "FREE" if free)
  handling_charge — handling/packing fee           (e.g. "₹11")
  platform_fee    — platform fee if shown          (e.g. "₹3")
  total_savings   — total savings banner           (e.g. "₹68")
  cart_total      — Grand Total / "Proceed to Pay" amount (e.g. "₹193")
  item_count      — number of distinct product lines visible in this screenshot

Serviceability and delivery info (extract from cart header or banner if visible):
  is_serviceable  — set FALSE when text like "Cart is Unserviceable",
                    "Currently not serviceable", "Delivery not available",
                    or "Service not available in your area" appears anywhere.
                    Default TRUE.
  delivery_time   — estimated delivery shown in header or banner
                    e.g. "8 mins", "10-15 mins", "Delivery by 11 PM"
                    Leave empty string if not visible.
  late_night_fee  — late-night / odd-hours surcharge if a separate line shows it
                    e.g. "₹15" for "Late night delivery fee ₹15"
                    Leave empty string if not shown.
  surge_charge    — high-demand surge fee if a separate line shows it
                    e.g. "₹10" for "Surge charge ₹10"
                    Leave empty string if not shown.

Rules:
- Read ALL numbers EXACTLY as shown, including the ₹ symbol.
- If a stepper shows "2", quantity is "2".
- Do NOT invent data; omit fields you cannot clearly read.
- If the cart is empty or not visible, return items=[].

Return ONLY valid JSON with no markdown fences.
"""


_VERIFY_SYSTEM = """\
You are a visual verification agent for grocery shopping automation.
You receive TWO screenshots: BEFORE and AFTER clicking the ADD button for a product.
Determine if the add succeeded.

SUCCESS signals (any one is enough):
- The "Add" button for that product changed to a quantity stepper (− N +)
- The cart badge count in the header increased by at least 1
- A green toast/notification saying "Added to cart" appeared

FAILURE signals:
- No visible difference between the two screenshots
- The Add button is still present, unchanged
- An error modal appeared

⚠️  PRODUCT PAGE NAVIGATION — CRITICAL FAILURE:
If the AFTER screenshot shows a PRODUCT DETAIL PAGE — characterised by:
  • A large product image dominating the centre of the screen
  • Product title, pack size, and a prominent price displayed centrally
  • "Add to cart" or "Add" CTA that fills the bottom bar or centre of the page
  • Ingredients / description / nutritional information below the fold
  • A back arrow ← at the top left
Then the click NAVIGATED to the product page instead of adding.
Set success=false, signal="navigated_to_product_page",
retry_instruction="Click the exact ADD button at the bottom-right corner of the
product card on the search results page, not the product image or card body."

IMPORTANT: Steppers look like  − 1 +  or  - 1 +  replacing the "Add" button.
Report the cart count you can read from the badge (0 if not visible).

Return ONLY valid JSON:
{
  "success": true,
  "signal": "stepper_appeared",
  "cart_count_after": 1,
  "observed": "Add button for Onion 1 kg changed to − 1 + stepper. Cart badge shows 1.",
  "retry_instruction": ""
}
"""


class FusedVisionAgent:
    """
    Vision-first agent: Gemini vision locates UI elements and returns
    pixel coordinates the executor clicks directly.
    DOM interaction is used only as a last-resort fallback.
    """

    async def analyse_cart(self, screenshot_b64: str) -> CartAnalysis:
        """
        Look at the cart page screenshot and return what's actually in the cart.
        Used to build an accurate post-shopping summary from ground truth.
        """
        client = get_client()
        try:
            result: CartAnalysis = await client.vision(
                screenshot_b64=screenshot_b64,
                prompt="Analyse the cart page and extract every item with its quantity and price.",
                system=_CART_SYSTEM,
                response_model=CartAnalysis,
                estimated_tokens=1000,
                compress=True,
            )
            log.info("cart_analysed_by_vision",
                     item_count=result.item_count or len(result.items),
                     cart_total=result.cart_total)
            return result
        except Exception as e:
            log.warning("cart_vision_failed", error=str(e))
            return CartAnalysis()

    async def locate_search_bar(
        self,
        screenshot_b64: str,
        context: AgentContext,
    ) -> Optional[tuple[int, int]]:
        """
        Vision call to find the search bar on the current page.
        Returns absolute pixel (x, y) for the 1280×800 viewport, or None.
        """
        client = get_client()
        prompt = (
            f"Platform: {context.platform.upper()}\n"
            "Locate the search bar. Return its normalised centre coordinates."
        )
        try:
            result: SearchBarLocation = await client.vision(
                screenshot_b64=screenshot_b64,
                prompt=prompt,
                system=_SEARCH_BAR_SYSTEM,
                response_model=SearchBarLocation,
                estimated_tokens=500,
                compress=False,
            )
            if result.found and result.confidence >= 0.45:
                x = int(result.x_norm * settings.browser_viewport_width)
                y = int(result.y_norm * settings.browser_viewport_height)
                log.info("search_bar_located_by_vision", x=x, y=y, conf=result.confidence)
                return (x, y)
            log.info("search_bar_vision_low_conf", conf=result.confidence, reasoning=result.reasoning)
        except Exception as e:
            log.warning("search_bar_vision_failed", error=str(e))
        return None

    async def verify_add(
        self,
        screenshot_before: str,
        screenshot_after: str,
        item_name: str,
    ) -> VerifyAddResult:
        """
        Compare before/after screenshots to confirm the ADD button click worked.
        Gives the agent a human-like awareness: it looks at what changed.
        """
        client = get_client()
        prompt = (
            f"I just clicked the ADD button for '{item_name}'.\n"
            "Image 1 = BEFORE, Image 2 = AFTER.\n"
            "Did the add succeed? Check for a stepper (− N +), cart badge increase, or toast."
        )
        try:
            result: VerifyAddResult = await client.vision_pair(
                screenshot_before_b64=screenshot_before,
                screenshot_after_b64=screenshot_after,
                prompt=prompt,
                system=_VERIFY_SYSTEM,
                response_model=VerifyAddResult,
                estimated_tokens=600,
                compress=True,
            )
            log.info(
                "verify_add_result",
                item=item_name,
                success=result.success,
                signal=result.signal,
                cart_after=result.cart_count_after,
                observed=result.observed,
            )
            return result
        except Exception as e:
            log.warning("verify_add_failed", item=item_name, error=str(e))
            # Assume success on API failure to avoid re-adding items
            return VerifyAddResult(success=True, signal="api_error", observed=str(e))

    async def analyse(
        self,
        screenshot_b64: str,
        item_name: str,
        target_quantity: Optional[str],
        context: AgentContext,
    ) -> FusedAnalysis:
        """
        Analyse a search-results (or homepage) screenshot.

        Returns a FusedAnalysis with the product to add and how many units.
        """
        client = get_client()
        prompt = _build_prompt(item_name, target_quantity, context)

        log.info("fused_vision_call", item=item_name, platform=context.platform)
        result: FusedAnalysis = await client.vision(
            screenshot_b64=screenshot_b64,
            prompt=prompt,
            system=_SYSTEM,
            response_model=FusedAnalysis,
            estimated_tokens=2000,
            compress=True,
        )
        log.info(
            "fused_vision_result",
            item=item_name,
            page_state=result.page_state,
            selected=result.selected_product,
            next=result.next_action,
            confidence=result.confidence,
        )
        return result


def _build_prompt(
    item_name: str,
    target_quantity: Optional[str],
    context: AgentContext,
) -> str:
    qty = f" ({target_quantity})" if target_quantity else ""
    cart = f"Cart currently has {context.cart_count} item(s)."
    recipe = ""
    if context.recipe_context:
        recipe = f"This is for: {context.recipe_context.recipe_name} × {context.recipe_context.servings} servings."

    # Build a preferences block so the model applies brand neutrality correctly
    pref_lines: list[str] = []
    if context.product_preferences:
        pp = context.product_preferences
        if pp.brand_preferences:
            pref_lines.append(f"User preferred brands: {', '.join(pp.brand_preferences)}")
        else:
            pref_lines.append("No brand preference — choose purely on size match and price")
        sensitivity = getattr(pp, "quantity_sensitivity", "exact")
        if sensitivity == "exact":
            pref_lines.append(
                "Quantity sensitivity: EXACT — choose the pack closest to the requested amount; "
                "DO NOT add a larger pack when a closer/exact size is available"
            )
        elif sensitivity == "generous":
            pref_lines.append("Quantity sensitivity: GENEROUS — next size up is acceptable")
        else:
            pref_lines.append("Quantity sensitivity: FLEXIBLE — any reasonable size is fine")
        if pp.dietary:
            pref_lines.append(f"Dietary restriction: {pp.dietary}")
    else:
        pref_lines.append("No brand preference — choose purely on size match and price")
        pref_lines.append(
            "Quantity sensitivity: EXACT — choose the pack closest to the requested amount"
        )

    prefs_block = "\n".join(f"  • {l}" for l in pref_lines)
    # Existing cart items tell vision what was pre-loaded so it can reason
    # about whether a stepper represents a previous-session item vs this run.
    if context.existing_cart_items:
        existing_str = "; ".join(context.existing_cart_items[:6])
        existing_note = f"Pre-existing items in cart from previous session: {existing_str}."
    else:
        existing_note = "Cart was empty at session start (no pre-existing items)."

    return (
        f"Platform: {context.platform.upper()} | {cart} {recipe}\n"
        f"{existing_note}\n"
        f"Task: add '{item_name}'{qty} to cart.\n"
        f"User preferences:\n{prefs_block}\n\n"
        "CATEGORY MATCH — NON-NEGOTIABLE\n"
        "───────────────────────────────\n"
        f"You are searching for: \"{item_name}\".\n"
        "If the search results show products from a COMPLETELY DIFFERENT food category "
        "than what was requested, set no_relevant_product=true IMMEDIATELY — do NOT "
        "pick the 'nearest thing' from the wrong category.\n"
        "Critical examples:\n"
        "  • Searching for a vegetable (tomato/onion/capsicum/peas/potato…) but results "
        "show ONLY packaged snacks (chips, Veggie Stix, namkeen, crisps) → "
        "no_relevant_product=true\n"
        "  • Searching for fresh produce but results show ONLY sauce/ketchup/pickle → "
        "check carefully; if nothing fresh is visible, no_relevant_product=true\n"
        "  • Searching for a spice/masala but results show ONLY snack packets → "
        "no_relevant_product=true\n"
        "A product name containing 'Veggie', 'Green', or 'Farm' does NOT make it the "
        "right product if it is clearly a packaged snack.\n\n"
        "Analyse the screenshot and return the JSON plan."
    )
