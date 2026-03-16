"""
User Profile
------------
Persistent user preferences loaded once at session start.
The chat session uses these silently — the user is never re-asked for anything
already stored here.

Storage: sessions/user_profile.json

Setup flow (UI):
  1. UI calls GET /preferences/setup  → receives structured setup questions
  2. User answers them in the UI (e.g. onboarding screen)
  3. UI calls POST /preferences        → saves answers
  4. All subsequent chat sessions load and apply the profile automatically

To update: user explicitly calls POST /preferences or DELETE /preferences (reset).
"""

import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from pydantic import BaseModel, Field

import structlog

if TYPE_CHECKING:
    from agents.intent_parser import ParsedIntent

log = structlog.get_logger()

PROFILE_PATH = Path("sessions/user_profile.json")


class UserProfile(BaseModel):
    # ── Core preferences ──────────────────────────────────────────────────────
    dietary: Optional[str] = None
    """'veg' | 'non-veg' | 'vegan' | 'gluten-free' | None"""

    budget_level: Optional[str] = None
    """'low' (budget) | 'medium' (mid-range) | 'high' (premium) | None"""

    prefer_organic: Optional[bool] = None
    """True = prefer organic/farm-fresh. False = regular brands fine. None = no preference set."""

    # ── Household ─────────────────────────────────────────────────────────────
    default_servings: Optional[int] = None
    """Default number of people to cook for. Used when user doesn't specify servings."""

    # ── Platform & brand ──────────────────────────────────────────────────────
    preferred_platform: Optional[str] = None
    """'blinkit' | 'zepto' | 'amazon' | 'flipkart' | None"""

    preferred_brands: list[str] = Field(default_factory=list)
    """Specific brands the user prefers, e.g. ['Amul', 'Tata', 'Nestle']"""

    # ── Pack-size behaviour ──────────────────────────────────────────────────
    quantity_sensitivity: str = "exact"
    """'exact' = prefer pack closest to requested | 'generous' = next size up OK | 'any' = flexible"""

    # ── Health & safety ───────────────────────────────────────────────────────
    allergens: list[str] = Field(default_factory=list)
    """Ingredients to avoid: ['nuts', 'dairy', 'gluten', 'seafood', 'eggs']"""

    # ── Metadata ──────────────────────────────────────────────────────────────
    setup_complete: bool = False
    """True once the user has completed the onboarding preference setup."""

    last_updated: float = Field(default_factory=time.time)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.last_updated = time.time()
        PROFILE_PATH.write_text(self.model_dump_json(indent=2))
        log.info(
            "user_profile_saved",
            dietary=self.dietary,
            budget_level=self.budget_level,
            prefer_organic=self.prefer_organic,
            default_servings=self.default_servings,
            preferred_platform=self.preferred_platform,
        )

    @classmethod
    def load(cls) -> "UserProfile":
        if PROFILE_PATH.exists():
            try:
                profile = cls.model_validate_json(PROFILE_PATH.read_text())
                log.info(
                    "user_profile_loaded",
                    setup_complete=profile.setup_complete,
                    dietary=profile.dietary,
                    budget_level=profile.budget_level,
                    prefer_organic=profile.prefer_organic,
                )
                return profile
            except Exception as e:
                log.warning("user_profile_load_failed_using_blank", error=str(e))
        return cls()

    def reset(self) -> None:
        """Wipe all preferences and delete the profile file."""
        if PROFILE_PATH.exists():
            PROFILE_PATH.unlink()
        # Reset all fields to defaults
        self.dietary = None
        self.budget_level = None
        self.prefer_organic = None
        self.default_servings = None
        self.preferred_platform = None
        self.preferred_brands = []
        self.allergens = []
        self.quantity_sensitivity = "exact"
        self.setup_complete = False
        log.info("user_profile_reset")

    # ── Intent integration ────────────────────────────────────────────────────

    def apply_to_intent(self, intent: "ParsedIntent") -> None:
        """
        Seed None fields in a ParsedIntent with saved profile values.
        Only fills gaps — anything the user said this message takes precedence.
        """
        if intent.dietary is None and self.dietary:
            intent.dietary = self.dietary
        if intent.budget_level is None and self.budget_level:
            intent.budget_level = self.budget_level
        if intent.prefer_organic is None and self.prefer_organic is not None:
            intent.prefer_organic = self.prefer_organic
        if intent.servings is None and self.default_servings:
            intent.servings = self.default_servings

    def filter_clarifying_questions(self, questions: list) -> list:
        """
        Drop any clarifying questions whose answers are already in the profile.
        Called after IntentParser generates questions, before showing them to user.
        """
        keep = []
        for q in questions:
            key = q.key
            if key == "organic_preference" and self.prefer_organic is not None:
                log.info("question_skipped_from_profile", key=key)
                continue
            if key == "budget_preference" and self.budget_level is not None:
                log.info("question_skipped_from_profile", key=key)
                continue
            if key == "dietary" and self.dietary is not None:
                log.info("question_skipped_from_profile", key=key)
                continue
            if key == "servings" and self.default_servings is not None:
                log.info("question_skipped_from_profile", key=key)
                continue
            keep.append(q)
        return keep

    def update_from_intent(self, intent: "ParsedIntent") -> bool:
        """
        Merge newly resolved values from intent back into the profile.
        Only updates when the intent has a real value that differs from current.
        Returns True if anything changed (caller should save).
        """
        changed = False
        if intent.prefer_organic is not None and self.prefer_organic != intent.prefer_organic:
            self.prefer_organic = intent.prefer_organic
            changed = True
        if intent.budget_level and self.budget_level != intent.budget_level:
            self.budget_level = intent.budget_level
            changed = True
        if intent.dietary and self.dietary != intent.dietary:
            self.dietary = intent.dietary
            changed = True
        return changed

    # ── Display ───────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Short human-readable string for display in UI / chat welcome."""
        parts = []
        if self.dietary:
            parts.append(self.dietary)
        if self.budget_level:
            labels = {"low": "budget-friendly", "medium": "mid-range", "high": "premium"}
            parts.append(labels.get(self.budget_level, self.budget_level))
        if self.prefer_organic is True:
            parts.append("organic preferred")
        elif self.prefer_organic is False:
            parts.append("regular brands ok")
        if self.default_servings:
            parts.append(f"cooking for {self.default_servings}")
        if self.allergens:
            parts.append(f"avoids {', '.join(self.allergens)}")
        quant_labels = {"generous": "generous sizing", "any": "flexible sizing"}
        if self.quantity_sensitivity in quant_labels:
            parts.append(quant_labels[self.quantity_sensitivity])
        if self.preferred_brands:
            parts.append(f"prefers {', '.join(self.preferred_brands[:3])}")
        return ", ".join(parts) if parts else "no preferences saved"


# ── Setup questions (returned by GET /preferences/setup) ─────────────────────
# Structured so a UI can render them as form controls.

SETUP_QUESTIONS = [
    {
        "key": "dietary",
        "label": "Dietary preference",
        "description": "We'll only suggest recipes and products that match your diet.",
        "type": "single_choice",
        "required": False,
        "options": [
            {"value": "veg",          "label": "Vegetarian"},
            {"value": "non-veg",      "label": "Non-vegetarian"},
            {"value": "vegan",        "label": "Vegan"},
            {"value": "halal",        "label": "Halal"},
            {"value": "gluten-free",  "label": "Gluten-free"},
        ],
    },
    {
        "key": "budget_level",
        "label": "Shopping style",
        "description": "Guides which product variants we pick for you.",
        "type": "single_choice",
        "required": False,
        "options": [
            {"value": "low",    "label": "Budget-friendly — cheapest option that works"},
            {"value": "medium", "label": "Mid-range — balance of price and quality"},
            {"value": "high",   "label": "Premium — quality first, price second"},
        ],
    },
    {
        "key": "prefer_organic",
        "label": "Organic / fresh products",
        "description": "When both are available, which do you prefer?",
        "type": "boolean",
        "required": False,
        "options": [
            {"value": True,  "label": "Yes — prefer organic or farm-fresh"},
            {"value": False, "label": "No — regular brands are fine"},
        ],
    },
    {
        "key": "default_servings",
        "label": "Household size",
        "description": "How many people do you usually cook for? We'll default recipes to this.",
        "type": "number",
        "required": False,
        "min": 1,
        "max": 20,
    },
    {
        "key": "preferred_platform",
        "label": "Preferred grocery platform",
        "description": "We'll use this as the default when you don't specify.",
        "type": "single_choice",
        "required": False,
        "options": [
            {"value": "blinkit",  "label": "Blinkit"},
            {"value": "zepto",    "label": "Zepto"},
            {"value": "amazon",   "label": "Amazon Fresh"},
            {"value": "flipkart", "label": "Flipkart Grocery"},
        ],
    },
    {
        "key": "allergens",
        "label": "Allergens to avoid",
        "description": "We'll flag or skip products containing these.",
        "type": "multi_choice",
        "required": False,
        "options": [
            {"value": "nuts",     "label": "Nuts"},
            {"value": "dairy",    "label": "Dairy"},
            {"value": "gluten",   "label": "Gluten"},
            {"value": "seafood",  "label": "Seafood"},
            {"value": "eggs",     "label": "Eggs"},
            {"value": "soy",      "label": "Soy"},
        ],
    },
    {
        "key": "preferred_brands",
        "label": "Preferred brands",
        "description": "Any brands you always want us to prioritise (e.g. Amul, Tata, Nestle).",
        "type": "text_list",
        "required": False,
        "placeholder": "Type a brand name and press Enter",
    },
    {
        "key": "quantity_sensitivity",
        "label": "Pack size preference",
        "description": "When the exact pack size is unavailable, what should we do?",
        "type": "single_choice",
        "required": False,
        "options": [
            {"value": "exact",    "label": "Exact — always pick the pack closest to requested amount"},
            {"value": "generous", "label": "Generous — next size up is fine (avoid waste)"},
            {"value": "any",      "label": "Flexible — I don't mind overshooting slightly"},
        ],
    },
]
