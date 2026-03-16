"""
Recipe Expander — Gemini implementation
-----------------------------------------
Recipe name → ShoppingItem list (text-only call).

Batching: multiple recipes can be expanded in ONE call to save RPM quota.
"""

from __future__ import annotations

from typing import Optional

import structlog
from pydantic import BaseModel

from gemini.core.client import get_client

log = structlog.get_logger()


# ── Output models ─────────────────────────────────────────────────────────────

class ShoppingItem(BaseModel):
    name: str
    quantity: str
    category: str
    notes: Optional[str] = None
    is_optional: bool = False
    substitutes: list[str] = []


class ExpandedRecipe(BaseModel):
    recipe_name: str
    servings: int
    items: list[ShoppingItem]
    assumed_pantry: list[str] = []
    estimated_cost: Optional[str] = None


class ExpandedRecipes(BaseModel):
    """Wrapper for batch expansion of multiple recipes in one call."""
    recipes: list[ExpandedRecipe]


# ── Agent ─────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a professional Indian chef specialised in quick-commerce grocery lists. "
    "When given a recipe name and serving size, return the complete, correctly-scaled "
    "ingredient list using standard quick-commerce product names as found on Blinkit, Zepto, "
    "and similar Indian grocery apps. Scale quantities precisely for the given servings. "
    "Use common Indian grocery measurements (e.g. 200g, 500g, 1kg, 1L, 1 bunch, 1 packet). "
    "Return ONLY valid JSON matching the exact schema."
)


class RecipeExpanderAgent:

    async def expand(
        self,
        recipe_name: str,
        servings: int,
        dietary: Optional[str] = None,
        budget_level: str = "medium",
        already_have: list[str] | None = None,
    ) -> ExpandedRecipe:
        """Expand a single recipe into a shopping list."""
        client = get_client()
        prompt = _build_single_prompt(recipe_name, servings, dietary, budget_level, already_have)
        log.debug("recipe_expander_call", recipe=recipe_name, servings=servings)
        result: ExpandedRecipe = await client.text(
            prompt=prompt,
            system=_SYSTEM,
            response_model=ExpandedRecipe,
            estimated_tokens=800,
        )
        log.info("recipe_expanded", recipe=recipe_name, items=len(result.items))
        return result

    async def expand_many(
        self,
        recipes: list[tuple[str, int]],   # [(name, servings), ...]
        dietary: Optional[str] = None,
        budget_level: str = "medium",
        already_have: list[str] | None = None,
    ) -> list[ExpandedRecipe]:
        """
        Expand multiple recipes in ONE API call — saves RPM quota.
        Falls back to sequential calls if batch fails.
        """
        if not recipes:
            return []
        if len(recipes) == 1:
            return [await self.expand(recipes[0][0], recipes[0][1], dietary, budget_level, already_have)]

        client = get_client()
        prompt = _build_batch_prompt(recipes, dietary, budget_level, already_have)
        log.debug("recipe_expander_batch_call", count=len(recipes))
        try:
            result: ExpandedRecipes = await client.text(
                prompt=prompt,
                system=_SYSTEM,
                response_model=ExpandedRecipes,
                estimated_tokens=800 * len(recipes),
            )
            log.info("recipes_expanded", count=len(result.recipes))
            return result.recipes
        except Exception as e:
            log.warning("batch_recipe_expand_failed", error=str(e), fallback="sequential")
            results = []
            for name, servings in recipes:
                results.append(await self.expand(name, servings, dietary, budget_level, already_have))
            return results


# ── Prompt builders ────────────────────────────────────────────────────────────

def _build_single_prompt(
    recipe_name: str,
    servings: int,
    dietary: Optional[str],
    budget_level: str,
    already_have: list[str] | None,
) -> str:
    diet = f"Dietary restriction: {dietary}." if dietary else ""
    have = f"Assume already in pantry (EXCLUDE from list): {', '.join(already_have)}." if already_have else ""
    budget_map = {"low": "budget/standard brands", "medium": "mid-range brands", "high": "premium or organic"}
    bud = budget_map.get(budget_level, "mid-range brands")
    return (
        f"Recipe: {recipe_name}\n"
        f"Servings: {servings}\n"
        f"Budget preference: {bud}\n"
        f"{diet}\n"
        f"{have}\n"
        "List all ingredients needed, scaled to the given servings. "
        "Use quick-commerce product names (e.g. 'Tomato' not 'fresh plum tomato', "
        "'Coriander' not 'cilantro'). "
        "For assumed_pantry, list items commonly already in an Indian kitchen "
        "that you excluded (oil, salt, water, basic spices like turmeric/salt). "
        "Provide estimated_cost as a rough INR string like '~₹150'."
    )


def _build_batch_prompt(
    recipes: list[tuple[str, int]],
    dietary: Optional[str],
    budget_level: str,
    already_have: list[str] | None,
) -> str:
    recipe_lines = "\n".join(f"  - {n} (for {s} servings)" for n, s in recipes)
    diet = f"Dietary restriction: {dietary}." if dietary else ""
    have = f"Assume already in pantry: {', '.join(already_have)}." if already_have else ""
    budget_map = {"low": "budget", "medium": "mid-range", "high": "premium"}
    bud = budget_map.get(budget_level, "mid-range")
    return (
        f"Expand ALL of the following recipes. Budget preference: {bud}.\n"
        f"{diet}\n{have}\nRecipes:\n{recipe_lines}\n"
        "For EACH recipe, return a complete scaled ingredient list. "
        "Use quick-commerce product names. Return all recipes in the 'recipes' array."
    )
