"""
Intent Parser — Gemini implementation
--------------------------------------
Converts NL user input → ParsedIntent using Gemini Flash Lite (text-only).

Key design vs. original
-----------------------
* Single model call — same role as the original IntentParserAgent.
* Existing-intent serialised as clean JSON (not Python repr).
* Merge rule is part of the schema description, not the prompt body.
* Handles Hinglish, regional language item names, and Indian cooking conventions.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

import structlog
from pydantic import BaseModel, Field

from gemini.core.client import get_client

log = structlog.get_logger()


# ── Output models ─────────────────────────────────────────────────────────────

class ClarifyingQuestion(BaseModel):
    key: str
    question: str
    for_recipe: Optional[str] = None
    for_item: Optional[str] = None


class DirectItem(BaseModel):
    name: str
    quantity: Optional[str] = None


class ParsedIntent(BaseModel):
    intent_type: Literal["recipe", "direct_buy", "mixed", "meal_plan"]
    recipes: list[str] = []
    direct_items: list[DirectItem] = []
    servings: Optional[int] = None
    budget_level: Optional[Literal["low", "medium", "high"]] = None
    budget_limit_inr: Optional[int] = None
    platform: Optional[str] = None
    dietary: Optional[str] = None
    prefer_organic: Optional[bool] = None
    is_meal_plan: bool = False
    clarifying_questions: list[ClarifyingQuestion] = []
    raw_intent: str = ""


# ── System instruction ──────────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are an expert grocery shopping intent extractor for an Indian quick-commerce assistant. "
    "Parse the user’s natural language request into a structured shopping intent. "
    "Return ONLY valid JSON matching the exact schema provided. "
    "Handle colloquial Indian English, Hinglish, recipe names in Hindi/regional languages, "
    "and implied quantities based on standard Indian cooking conventions."
)


# ── Agent ─────────────────────────────────────────────────────────────────────

class IntentParserAgent:
    """Parse or update a shopping intent from a user message."""

    async def parse(
        self,
        user_message: str,
        conversation_history: list[dict] | None = None,
        existing_intent: ParsedIntent | None = None,
    ) -> ParsedIntent:
        client = get_client()
        prompt = _build_prompt(user_message, conversation_history, existing_intent)
        log.debug("intent_parser_call", msg_preview=user_message[:60])
        result: ParsedIntent = await client.text(
            prompt=prompt,
            system=_SYSTEM,
            response_model=ParsedIntent,
            estimated_tokens=600,
        )
        log.info("intent_parsed",
                 type=result.intent_type,
                 items=[i.name for i in result.direct_items],
                 recipes=result.recipes,
                 questions=[q.key for q in result.clarifying_questions])
        return result


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(
    user_message: str,
    history: list[dict] | None,
    existing: ParsedIntent | None,
) -> str:
    parts: list[str] = []

    # Compact conversation history (last 6 turns only)
    if history:
        tail = history[-6:]
        parts.append("HISTORY:\n" + "\n".join(
            f"{'U' if m['role']=='user' else 'A'}: {m['content']}" for m in tail
        ))

    # Existing intent (for clarification answers — MERGE, don't replace)
    if existing:
        items_json = json.dumps(
            [{"name": i.name, "quantity": i.quantity} for i in existing.direct_items],
            ensure_ascii=False,
        )
        parts.append(
            "EXISTING_INTENT (MERGE — only update what the user just answered, "
            "keep ALL other fields unchanged):\n"
            f"type={existing.intent_type} recipes={existing.recipes} "
            f"items={items_json} servings={existing.servings} "
            f"dietary={existing.dietary} prefer_organic={existing.prefer_organic} "
            f"budget_level={existing.budget_level}"
        )

    parts.append(f'USER: "{user_message}"')
    parts.append(_RULES)

    return "\n\n".join(parts)


# ── Rules block ──────────────────────────────────────────────────────────────

_RULES = """\
RULES:

intent_type:
  recipe     — user wants to cook something (extract recipe name; ask servings if missing)
  direct_buy — user wants to buy specific items by name
  mixed      — both recipes AND direct buy items mentioned
  meal_plan  — multiple meals planned together

RECIPE DISAMBIGUATION — MANDATORY
──────────────────────────────────
Certain recipe names are ambiguous and MUST trigger a clarifying question before
proceeding. Never assume the variant. Use the question format shown below.

  biryani → ask "What type of biryani would you like to cook?\n• Veg biryani\n• Chicken biryani\n• Mutton biryani\n• Prawn biryani"
  curry   → ask "Which curry do you want to make?\n• Paneer curry (veg)\n• Chicken curry\n• Mutton curry\n• Dal curry"
  pulao / pilaf → ask "What type of pulao?\n• Veg pulao\n• Chicken pulao\n• Peas pulao (matar pulao)"
  khichdi → ask "What type of khichdi?\n• Plain dal khichdi\n• Masala khichdi\n• Vegetable khichdi"
  dal     → ask "Which dal?\n• Dal tadka\n• Dal makhani\n• Yellow dal (moong/arhar)\n• Chana dal"
  pasta   → ask "What type of pasta?\n• Red sauce pasta\n• White sauce pasta\n• Pesto pasta"
  sandwich → ask "What type of sandwich?\n• Veg sandwich\n• Chicken sandwich\n• Grilled sandwich"
  If dietary preference is already known (non-veg set) → skip veg options in the question.
  If dietary preference is veg/jain → skip non-veg options; if only one veg option remains, proceed without asking.
  key for recipe disambiguation: "recipe_type_{recipe_lowercase}"

quantities: normalise all to standard forms
  "two liters / 2 litres / 2l" → "2L"
  "half kilo / 500 grams / half kg" → "500g"
  "quarter kg / 250 grams" → "250g"
  "a dozen / 12 pieces" → "1 dozen"
  "5 pieces / 5 pcs" → "5 pcs"
  "few" → leave null and ask

clarifying_questions: max 2 at a time; ONLY ask what is genuinely ambiguous
  Priority order: recipe_type > servings > quantity > dietary > organic > budget
  For recipe: MUST ask servings if not mentioned (AFTER recipe disambiguation)
  For direct produce with no quantity given:
    herbs (coriander/dhania/dhaniya/mint/pudina/curry leaves/kadi patta/kari patta/
           dill/suva/parsley/basil/methi/fenugreek leaves)
        → ask "How much {item} do you need? (e.g., 1 bunch, 50g)"
          key: "quantity_{item_lowercase}"
    vegetables (tomato/tamatar/onion/pyaaz/potato/aloo/carrot/gajar/capsicum/shimla mirch/
                peas/matar/cauliflower/gobhi/brinjal/baingan/ladyfinger/bhindi/cucumber/kheera)
        → ask "How much {item} do you need? (e.g., 250g, 500g, 1kg)"
          key: "quantity_{item_lowercase}"
    fruits (banana/kela/apple/seb/orange/santra/mango/aam/grapes/angoor/pomegranate/anar)
        → ask "How much {item} do you need? (e.g., 4 pieces, 500g, 1kg)"
          key: "quantity_{item_lowercase}"
  Packaged goods (salt/namak/sugar/oil/atta/maida/rice/chawal/dal/lentils/spices/
                  ghee/butter/milk/curd/paneer): do NOT ask for quantity, keep null
  Skip questions already clearly answered in conversation history

budget_level:
  "cheap / budget / affordable / sasta" → low
  "normal / standard / regular" → medium
  "premium / best / quality / expensive / branded" → high
  not mentioned → null

prefer_organic:
  "organic / fresh / natural / farm-fresh / desi" → true
  "regular / any / normal / packed / branded" → false
  not mentioned → null (do NOT assume)

platform: only set if user explicitly names a platform
  "blinkit / blinkqit" → blinkit | "zepto" → zepto

dietary: extract if mentioned
  veg/vegetarian → veg | non-veg/chicken/meat/egg → non-veg | vegan → vegan
  gluten-free → gluten-free | dairy-free → dairy-free | jain → jain
  not mentioned → null

Hinglish / regional name mappings:
  dhania/dhaniya → coriander | pudina → mint | jeera → cumin seeds
  haldi → turmeric | mirchi/mirch → chilli | laal mirch → red chilli
  hari mirch → green chilli | pyaaz → onion | tamatar → tomato
  aloo → potato | kela → banana | aam → mango | dahi → curd
  dudh → milk | chawal → rice | paneer/ghee/atta → keep as-is
"""
