"""
Shared Pydantic models for the Gemini implementation.
These mirror core/models.py from the original codebase but are self-contained.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    CLICK      = "click"
    TYPE       = "type"
    SCROLL     = "scroll"
    HOVER      = "hover"
    KEY        = "key"
    WAIT       = "wait"
    SCREENSHOT = "screenshot"


# ── Low-level action / result models ──────────────────────────────────────────

class Coordinate(BaseModel):
    x: float   # normalised 0–1
    y: float


class ActionPlan(BaseModel):
    action: ActionType
    coordinate: Optional[Coordinate] = None
    text: Optional[str] = None
    key: Optional[str] = None
    scroll_direction: Optional[Literal["up", "down", "left", "right"]] = None
    scroll_amount: int = 3
    element_description: str = ""
    expected_outcome: str = ""
    confidence: float = 0.8
    reasoning: str = ""


class ActionResult(BaseModel):
    success: bool
    action_taken: ActionPlan
    screenshot_before: str = ""
    screenshot_after: str = ""
    elapsed_ms: float = 0.0
    error: Optional[str] = None


class VerificationResult(BaseModel):
    success: bool
    signal: str = "no_change"
    confidence: float = 0.5
    observed_change: str = ""
    retry_instruction: str = ""
    next_action: Literal["proceed", "retry", "skip", "escalate"] = "retry"


# ── Task / session models ─────────────────────────────────────────────────────

class QuantityRequirement(BaseModel):
    min_quantity: str
    max_quantity: str
    ideal_quantity: str
    reasoning: str = ""


class RecipeContext(BaseModel):
    recipe_name: str
    servings: int = 2
    quantity_requirements: dict[str, QuantityRequirement] = {}


class ProductPreferences(BaseModel):
    prefer_organic: bool = False
    budget_level: str = "medium"
    dietary: Optional[str] = None
    brand_preferences: list[str] = []
    avoid_brands: list[str] = []
    quantity_sensitivity: str = "exact"  # "exact" | "generous" | "any"


class TaskStep(BaseModel):
    step_id: str
    description: str
    item_name: str
    item_quantity: Optional[str] = None
    expected_outcome: str = ""
    status: str = "pending"          # pending | in_progress | done | failed | skipped
    item_added_to_cart: bool = False
    product_selected: Optional[str] = None
    selected_product_details: Optional[dict] = None
    combination_plan: Optional[list[dict]] = None
    combination_step_index: int = 0
    last_cart_count_seen: int = 0


class AgentContext(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_goal: str = ""
    platform: str = "blinkit"
    current_url: str = ""
    cart_count: int = 0
    budget_limit: Optional[int] = None
    estimated_spend: int = 0

    existing_cart_items: list[str] = []
    items_in_cart_this_session: list[str] = []
    substitutions_made: dict[str, str] = {}         # original → substitute name
    substitution_reasons: dict[str, str] = {}       # original → reason string

    recipe_context: Optional[RecipeContext] = None
    product_preferences: Optional[ProductPreferences] = None
    selected_product_details: Optional[dict] = None

    current_step_id: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}


# ── Cart summary models ────────────────────────────────────────────────────────

class CartItemSummary(BaseModel):
    item_name: str
    quantity_requested: Optional[str] = None
    quantity_added: Optional[str] = None
    pack_description: Optional[str] = None
    product_selected: Optional[str] = None
    unit_price: Optional[str] = None       # price per single pack
    total_price: Optional[str] = None      # line-item total (unit_price × qty)
    mrp: Optional[str] = None              # original MRP for savings display
    savings: Optional[str] = None         # discount on this line
    quantity_note: Optional[str] = None    # mismatch warning e.g. "Needed 100ml. Only 1L available."
    status: Literal["added", "skipped", "failed"] = "added"
    from_previous_session: bool = False    # True if item was already in cart
    alternative_used: bool = False
    alternative_reason: str = ""           # why the substitute was chosen


class CartSummary(BaseModel):
    items: list[CartItemSummary] = []
    total_items_requested: int = 0
    total_items_added: int = 0
    total_items_skipped: int = 0
    total_items_failed: int = 0
    items_this_session: int = 0            # items added in this run (not pre-existing)
    # Bill breakdown (vision-read from cart page)
    items_subtotal: str = ""               # e.g. "₹152"
    delivery_charge: str = ""             # e.g. "₹30"
    handling_charge: str = ""             # e.g. "₹11"
    platform_fee: str = ""                # e.g. "₹3"
    total_savings: str = ""               # e.g. "₹68"
    grand_total: str = ""                 # final amount e.g. "₹193"
    estimated_total: str = ""             # fallback when vision can't read total
    duration_seconds: float = 0.0
    # Serviceability + delivery (vision-read from cart page)
    is_serviceable: bool = True           # False = cart cannot be delivered
    delivery_time: str = ""               # e.g. "8 mins", "10-15 mins"
    late_night_fee: str = ""              # e.g. "₹15" (odd-hours surcharge)
    surge_charge: str = ""               # e.g. "₹10" (high demand fee)


# ── Platform comparison models ─────────────────────────────────────────────────

class PlatformFees(BaseModel):
    delivery_fee: int = 0
    handling_fee: int = 0
    platform_fee: int = 0
    surge_fee: int = 0
    discount: int = 0

    @property
    def total_extra(self) -> int:
        return (self.delivery_fee + self.handling_fee +
                self.platform_fee + self.surge_fee - self.discount)


class PlatformResult(BaseModel):
    platform: str
    summary: Optional[CartSummary] = None
    fees: PlatformFees = Field(default_factory=PlatformFees)
    cart_value: int = 0
    effective_total: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    # per-item availability for cross-platform comparison
    item_coverage: dict[str, str] = Field(default_factory=dict)  # item→status


class MultiPlatformComparison(BaseModel):
    platforms_run: list[str]
    results: dict[str, PlatformResult] = Field(default_factory=dict)
    recommended_platform: Optional[str] = None
    recommendation_reason: str = ""
