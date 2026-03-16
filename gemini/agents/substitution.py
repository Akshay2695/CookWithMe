"""
Substitution Agent — Gemini implementation
-------------------------------------------
Finds a substitute for an unavailable item (text-only, ~100 tokens).
"""

from __future__ import annotations

from typing import Optional

import structlog
from pydantic import BaseModel

from gemini.core.client import get_client

log = structlog.get_logger()


class SubstitutionResult(BaseModel):
    substitute_name: str
    substitute_quantity: str
    reason: str
    user_message: str


_SYSTEM = (
    "You are an expert Indian grocery substitution advisor. "
    "When a product is unavailable on a quick-commerce platform, suggest the best practical "
    "substitute that is commonly available on Indian quick-commerce apps (Blinkit, Zepto, "
    "and other major Indian grocery apps). Consider the cooking context — a substitute should work in the recipe "
    "and be of similar flavour profile and function. "
    "CRITICAL: substitute_name MUST be a SHORT, SIMPLE product name suitable for a grocery "
    "search bar — 1 to 4 words maximum, no parentheses, no 'for X' notes, no 'or' compounds. "
    "Examples of GOOD substitute_name: 'Paneer', 'Coriander Leaves', 'Bay Leaf', 'Farm Eggs'. "
    "Examples of BAD substitute_name: 'Paneer (for savory dishes) or Thick Greek Yogurt (for baking)', "
    "'Fresh Herb Bundle or Dried Herbs'. "
    "Return ONLY valid JSON matching the schema."
)


class SubstitutionAgent:

    async def find_substitute(
        self,
        item_name: str,
        quantity: str,
        recipe_context: Optional[str],
        already_tried: list[str] | None,
        platform: str,
    ) -> SubstitutionResult:
        tried_note = f"Already tried and unavailable: {', '.join(already_tried)}." if already_tried else ""
        recipe_note = f"This ingredient is for: {recipe_context}." if recipe_context else ""
        prompt = (
            f"The item '{item_name}' ({quantity}) is not available on {platform}.\n"
            f"{recipe_note}\n"
            f"{tried_note}\n"
            "Suggest the best available substitute. Consider:\n"
            "  - Functional equivalent (same role in the recipe)\n"
            "  - Similar flavour profile and texture\n"
            "  - Commonly stocked on Indian quick-commerce apps\n"
            "In user_message, write a friendly one-line message explaining the "
            "substitution to the user (e.g. 'Fresh coriander isn’t available, "
            "so I’ll add dried coriander powder instead.')."
        )
        client = get_client()
        result: SubstitutionResult = await client.text(
            prompt=prompt,
            system=_SYSTEM,
            response_model=SubstitutionResult,
            estimated_tokens=400,
        )
        log.info("substitution_found", original=item_name, substitute=result.substitute_name)
        return result
