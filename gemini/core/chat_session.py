"""
Chat Session — Gemini implementation
--------------------------------------
Conversational state machine identical to the original chat_session.py
but using the Gemini-backed IntentParserAgent and RecipeExpanderAgent.

State machine:
  IDLE → [PREFERENCE_SETUP →] CLARIFYING → CONFIRMING → EXECUTING → DONE
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

import structlog
from pydantic import BaseModel, Field

from gemini.agents.intent_parser import (
    ClarifyingQuestion, DirectItem, IntentParserAgent, ParsedIntent
)
from gemini.agents.recipe_expander import RecipeExpanderAgent
from gemini.core.models import (
    AgentContext, ProductPreferences, QuantityRequirement, RecipeContext, TaskStep
)

# Reuse the UserProfile utility from the original package
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.user_profile import UserProfile  # type: ignore

log = structlog.get_logger()


# ── State ─────────────────────────────────────────────────────────────────────

class ChatState(str, Enum):
    IDLE            = "idle"
    PREFERENCE_SETUP = "preference_setup"
    PREF_REVIEW     = "pref_review"
    CLARIFYING      = "clarifying"
    CONFIRMING      = "confirming"
    EXECUTING       = "executing"
    DONE            = "done"
    ERROR           = "error"


class ConfirmedItem(BaseModel):
    name: str
    quantity: str
    category: str
    source_recipe: Optional[str] = None
    notes: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    state: ChatState
    message: str
    items: list[ConfirmedItem] = []
    task_id: Optional[str] = None


# ── Session ────────────────────────────────────────────────────────────────────

class ChatSession:

    def __init__(self, session_id: str = None, platform: str = "blinkit"):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.platform = platform
        self.state = ChatState.IDLE
        self.created_at = time.time()

        self.conversation_history: list[dict] = []
        self.parsed_intent: Optional[ParsedIntent] = None
        self.pending_questions: list[ClarifyingQuestion] = []
        self.confirmed_items: list[ConfirmedItem] = []
        self.existing_cart_items: list[str] = []
        self.error: Optional[str] = None
        self._pending_first_message: Optional[str] = None
        self._pref_review_done: bool = False  # once per session
        # Set by server after shopping completes — used for post-shopping Q&A
        self._shopping_result: str = ""

        self.user_profile: UserProfile = UserProfile.load()
        self._intent_parser = IntentParserAgent()
        self._recipe_expander = RecipeExpanderAgent()

    def set_shopping_complete(self, summary_text: str) -> None:
        """Called by the server after shopping finishes to record the result
        and unlock conversational Q&A in the DONE state."""
        self._shopping_result = summary_text
        self.state = ChatState.DONE
        log.info("shopping_marked_done", session=self.session_id)

    def _add_message(self, role: str, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content})

    async def handle_message(self, user_message: str) -> ChatResponse:
        self._add_message("user", user_message)
        log.info("chat_message", session=self.session_id, state=self.state, msg=user_message[:80])
        try:
            if self.state == ChatState.IDLE:
                return await self._handle_initial(user_message)
            elif self.state == ChatState.PREFERENCE_SETUP:
                return await self._handle_preference_setup(user_message)
            elif self.state == ChatState.PREF_REVIEW:
                return await self._handle_pref_review(user_message)
            elif self.state == ChatState.CLARIFYING:
                return await self._handle_clarification(user_message)
            elif self.state == ChatState.CONFIRMING:
                return await self._handle_confirmation(user_message)
            elif self.state in (ChatState.DONE, ChatState.EXECUTING, ChatState.ERROR):
                return await self._handle_freeform(user_message)
            else:
                return ChatResponse(
                    session_id=self.session_id, state=self.state,
                    message=f"Session is in state '{self.state}'. Start a new session.",
                )
        except Exception as e:
            log.error("chat_session_error", session=self.session_id, error=str(e))
            self.state = ChatState.ERROR
            self.error = str(e)
            return ChatResponse(
                session_id=self.session_id, state=ChatState.ERROR,
                message=f"Something went wrong: {e}. Please try again.",
            )

    # ── phases ────────────────────────────────────────────────────────────────

    async def _handle_initial(self, user_message: str) -> ChatResponse:
        lower = user_message.lower()
        if any(k in lower for k in ("reset preferences", "change preferences", "update preferences")):
            self.user_profile.reset()
            self.state = ChatState.PREFERENCE_SETUP
            prompt = self._pref_prompt()
            self._add_message("assistant", prompt)
            return ChatResponse(session_id=self.session_id, state=ChatState.PREFERENCE_SETUP, message=prompt)

        if not self.user_profile.setup_complete:
            self._pending_first_message = user_message
            self.state = ChatState.PREFERENCE_SETUP
            prompt = self._pref_prompt()
            self._add_message("assistant", prompt)
            return ChatResponse(session_id=self.session_id, state=ChatState.PREFERENCE_SETUP, message=prompt)

        # ── Per-session preference review (blocking, once per session) ────────
        if not self._pref_review_done:
            self._pref_review_done = True
            self._pending_first_message = user_message
            self.state = ChatState.PREF_REVIEW
            msg = self._pref_review_prompt()
            self._add_message("assistant", msg)
            return ChatResponse(session_id=self.session_id, state=ChatState.PREF_REVIEW, message=msg)

        self.parsed_intent = await self._intent_parser.parse(
            user_message=user_message,
            conversation_history=self.conversation_history,
        )
        self.user_profile.apply_to_intent(self.parsed_intent)
        self.parsed_intent.clarifying_questions = self.user_profile.filter_clarifying_questions(
            self.parsed_intent.clarifying_questions
        )

        if self.parsed_intent.clarifying_questions:
            self.state = ChatState.CLARIFYING
            self.pending_questions = list(self.parsed_intent.clarifying_questions)
            msg = self._format_questions(self.pending_questions)
            self._add_message("assistant", msg)
            return ChatResponse(session_id=self.session_id, state=ChatState.CLARIFYING, message=msg)

        return await self._build_and_confirm()

    def _pref_review_prompt(self) -> str:
        return (
            f"👤 **Your saved preferences:** {self.user_profile.summary()}\n\n"
            "Would you like to **update** any before we begin?\n"
            "• Type **update** or **yes** to review/change preferences\n"
            "• Type **no** or press **Enter** to continue"
        )

    async def _handle_pref_review(self, user_message: str) -> ChatResponse:
        """User replied to the per-session preference-review prompt."""
        lower = user_message.strip().lower()
        wants_update = any(w in lower for w in (
            "yes", "update", "change", "yep", "yeah", "haan", "edit", "modify"
        ))
        pending = self._pending_first_message
        self._pending_first_message = None

        if wants_update:
            # Keep the original request as pending so we continue after setup
            self._pending_first_message = pending
            self.state = ChatState.PREFERENCE_SETUP
            prompt = self._pref_prompt()
            self._add_message("assistant", prompt)
            return ChatResponse(
                session_id=self.session_id,
                state=ChatState.PREFERENCE_SETUP,
                message=prompt,
            )

        # User chose to continue — process original message
        self.state = ChatState.IDLE
        if pending:
            return await self._handle_initial(pending)
        msg = "What would you like to cook or buy today?"
        self._add_message("assistant", msg)
        return ChatResponse(session_id=self.session_id, state=ChatState.IDLE, message=msg)

    def _pref_prompt(self) -> str:
        return (
            "Let me save your preferences to personalise every session:\n\n"
            "🥗 **Diet** — veg / non-veg / vegan / halal / gluten-free?\n"
            "💰 **Budget** — budget-friendly / mid-range / premium?\n"
            "🌿 **Organic** — prefer organic/farm-fresh, or regular brands?\n"
            "👨‍👩‍👧 **Household** — how many people do you usually cook for?\n"
            "🛒 **Platform** — Blinkit / Zepto?\n"
            "🏷️ **Brands** — any preferred brands? (e.g. Amul, Tata — or skip)\n"
            "📦 **Pack sizes** — exact match / generous (next size up OK) / flexible?\n\n"
            'Answer naturally — e.g. "non-veg, mid-range, Blinkit, exact" — or type **skip**.'
        )

    async def _handle_preference_setup(self, user_message: str) -> ChatResponse:
        skipped = user_message.strip().lower() in ("skip", "s", "no thanks", "skip all")
        if not skipped:
            try:
                pref = await self._intent_parser.parse(user_message=user_message)
                if pref.servings and not self.user_profile.default_servings:
                    self.user_profile.default_servings = pref.servings
                self.user_profile.update_from_intent(pref)
                if pref.platform:
                    self.user_profile.preferred_platform = pref.platform
            except Exception as e:
                log.warning("pref_parse_failed", error=str(e))

            # ── Extract preferred brands from free-text ─────────────────────
            import re as _re_pref
            _known_brands = (
                "amul|tata|nestle|britannia|haldirams|mtr|patanjali|fortune|saffola"
                "|aashirvaad|mother dairy|brookbond|tropicana|real|maggi|24 mantra"
                "|godrej|dabur|iqf|fresho|reliance smart|d-mart|bigbasket"
            )
            brand_hits = _re_pref.findall(
                r"\b(" + _known_brands + r")\b",
                user_message.lower(),
            )
            if brand_hits:
                self.user_profile.preferred_brands = list({
                    b.title() for b in brand_hits
                })

            # ── Parse quantity / pack-size sensitivity ───────────────────────
            lmsg = user_message.lower()
            if any(w in lmsg for w in ("exact", "precise", "closest", "minimum", "no overshoot")):
                self.user_profile.quantity_sensitivity = "exact"
            elif any(w in lmsg for w in ("generous", "next size", "bigger ok", "larger ok")):
                self.user_profile.quantity_sensitivity = "generous"
            elif any(w in lmsg for w in ("flexible", "any", "don't mind", "doesn't matter", "no preference")):
                self.user_profile.quantity_sensitivity = "any"
        self.user_profile.setup_complete = True
        self.user_profile.save()

        ack = "No problem, skipped! " if skipped else f"Got it — saved: {self.user_profile.summary()}. "
        ack += 'Say "change my preferences" anytime.\n\n'

        self.state = ChatState.IDLE
        pending = self._pending_first_message
        self._pending_first_message = None
        if pending:
            resp = await self._handle_initial(pending)
            resp.message = ack + resp.message
            return resp
        msg = ack + "What would you like to cook or buy today?"
        self._add_message("assistant", msg)
        return ChatResponse(session_id=self.session_id, state=ChatState.IDLE, message=msg)

    async def _handle_clarification(self, user_message: str) -> ChatResponse:
        self.parsed_intent = await self._intent_parser.parse(
            user_message=user_message,
            conversation_history=self.conversation_history,
            existing_intent=self.parsed_intent,
        )
        self.parsed_intent.clarifying_questions = self.user_profile.filter_clarifying_questions(
            self.parsed_intent.clarifying_questions
        )
        if self.parsed_intent.clarifying_questions:
            self.pending_questions = list(self.parsed_intent.clarifying_questions)
            msg = self._format_questions(self.pending_questions)
            self._add_message("assistant", msg)
            return ChatResponse(session_id=self.session_id, state=ChatState.CLARIFYING, message=msg)
        return await self._build_and_confirm()

    async def _build_and_confirm(self) -> ChatResponse:
        intent = self.parsed_intent
        all_items: list[ConfirmedItem] = []

        # Expand recipes — batch if multiple
        if intent.recipes:
            recipe_tuples = [(r, intent.servings or self.user_profile.default_servings or 2)
                             for r in intent.recipes]
            expanded_list = await self._recipe_expander.expand_many(
                recipes=recipe_tuples,
                dietary=intent.dietary,
                budget_level=intent.budget_level or "medium",
                already_have=self.existing_cart_items or None,
            )
            for expanded in expanded_list:
                for item in expanded.items:
                    if any(item.name.lower() == e.lower() for e in self.existing_cart_items):
                        continue
                    all_items.append(ConfirmedItem(
                        name=item.name,
                        quantity=item.quantity,
                        category=item.category,
                        source_recipe=expanded.recipe_name,
                        notes=item.notes,
                    ))

        # Direct items
        for di in intent.direct_items:
            if isinstance(di, DirectItem):
                name = di.name
                qty = di.quantity or self._default_quantity(di.name)
            else:
                name, qty = str(di), "1 unit"
            if any(name.lower() == e.lower() for e in self.existing_cart_items):
                continue
            all_items.append(ConfirmedItem(name=name, quantity=qty, category="general"))

        all_items = _deduplicate(all_items)
        self.confirmed_items = all_items

        if self.user_profile.update_from_intent(intent):
            self.user_profile.save()

        if not all_items:
            # Stay IDLE — don't lock into CONFIRMING with an empty list.
            # This can happen when the requested item was already in existing_cart_items
            # or when the intent parser returned no direct_items/recipes.
            msg = (
                "I couldn't find any items to add. "
                "Try rephrasing — e.g. \"add eggs\" or \"buy 500g onion\"."
            )
            self._add_message("assistant", msg)
            return ChatResponse(
                session_id=self.session_id, state=ChatState.IDLE, message=msg
            )

        msg = self._format_list(all_items, intent)
        self.state = ChatState.CONFIRMING
        self._add_message("assistant", msg)
        return ChatResponse(
            session_id=self.session_id, state=ChatState.CONFIRMING,
            message=msg, items=all_items,
        )

    async def _handle_confirmation(self, user_message: str) -> ChatResponse:
        lower = user_message.lower().strip()
        if any(w in lower for w in ["yes","yeah","yep","ok","okay","sure","go","proceed","confirm","add","haan","ha"]):
            self.state = ChatState.EXECUTING
            msg = f"Starting to add {len(self.confirmed_items)} items to cart..."
            self._add_message("assistant", msg)
            return ChatResponse(
                session_id=self.session_id, state=ChatState.EXECUTING,
                message=msg, items=self.confirmed_items,
            )
        if any(w in lower for w in ["no","cancel","stop","nope","nahi"]):
            self.state = ChatState.IDLE
            msg = "Shopping cancelled. Tell me what you'd like to buy!"
            self._add_message("assistant", msg)
            return ChatResponse(session_id=self.session_id, state=ChatState.IDLE, message=msg)

        # ── Treat as an edit to the existing list ─────────────────────────────
        # Use the LLM to apply the edit diff rather than losing the whole list
        return await self._handle_list_edit(user_message)

    async def _handle_list_edit(self, edit_message: str) -> ChatResponse:
        """
        Apply a free-text edit to ``confirmed_items`` without losing the list.
        Delegates to the LLM which returns a modified JSON array.
        Changes are committed in-place; we stay in CONFIRMING state so the user
        can accept, cancel, or keep editing.
        """
        from gemini.core.client import get_client
        import json as _json

        current_json = _json.dumps(
            [{"name": it.name, "quantity": it.quantity, "category": it.category,
              "source_recipe": it.source_recipe, "notes": it.notes}
             for it in self.confirmed_items],
            ensure_ascii=False,
        )

        system = (
            "You are a shopping list editor. "
            "You receive the CURRENT shopping list as JSON and a user edit instruction. "
            "Apply the edit precisely — change quantities, add or remove items — and return "
            "the COMPLETE updated list as a JSON array (same schema). "
            "IMPORTANT: keep all items the user did NOT request changes to unchanged. "
            "Preserve the source_recipe, category, and notes fields as-is. "
            "Return ONLY a valid JSON array, no markdown, no explanation."
        )
        prompt = (
            f"Current shopping list (JSON):\n{current_json}\n\n"
            f"User's edit instruction: \"{edit_message}\"\n\n"
            "Return the updated list as a JSON array."
        )

        try:
            client = get_client()
            raw = await client.text(prompt=prompt, system=system, estimated_tokens=400)

            # Strip any markdown fences the model may have added
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[-2].split("\n", 1)[-1] if "```" in raw else raw
                raw = raw.strip("`").strip()

            updated = _json.loads(raw)
            if not isinstance(updated, list) or not updated:
                raise ValueError("LLM returned empty or non-list")

            self.confirmed_items = [
                ConfirmedItem(
                    name=it.get("name", ""),
                    quantity=it.get("quantity", "1 unit"),
                    category=it.get("category", "general"),
                    source_recipe=it.get("source_recipe"),
                    notes=it.get("notes"),
                )
                for it in updated
                if it.get("name")
            ]

        except Exception as exc:
            log.warning("list_edit_failed", error=str(exc))
            # Fallback: re-parse normally but seed with existing items
            self.state = ChatState.IDLE
            return await self._handle_initial(edit_message)

        self.state = ChatState.CONFIRMING
        msg = self._format_list(self.confirmed_items, self.parsed_intent or ParsedIntent(intent_type="shopping"))
        self._add_message("assistant", msg)
        return ChatResponse(
            session_id=self.session_id, state=ChatState.CONFIRMING,
            message=msg, items=self.confirmed_items,
        )

    async def _handle_freeform(self, user_message: str) -> ChatResponse:
        """
        Post-shopping conversational handler for DONE / EXECUTING / ERROR states.

        Behaviour:
          • If the message looks like a new shopping request → reset to IDLE and
            handle it as a fresh intent.
          • Otherwise → use the Gemini LLM with full session context to answer
            follow-up questions (e.g. "what did you add?", "how much did it cost?").
        """
        from gemini.core.client import get_client

        lower = user_message.lower()

        # Detect clear new shopping requests (not questions about the previous run)
        is_question = any(q in lower for q in (
            "?", "what", "how", "which", "when", "who", "did you", "can you",
            "tell me", "show me", "list", "summary",
        ))
        new_request_words = ("cook", "make", "prepare", "want to", "i need", "buy",
                             "order", "add to", "get me", "please add", "shop for")
        looks_new = any(w in lower for w in new_request_words)

        if looks_new and not is_question and self.state != ChatState.EXECUTING:
            # Reset and treat as a fresh shopping intent
            self.state = ChatState.IDLE
            return await self._handle_initial(user_message)

        # Build a rich system context for the LLM
        items_text = ""
        if self.confirmed_items:
            lines = []
            for item in self.confirmed_items:
                recipe_tag = f" [{item.source_recipe}]" if item.source_recipe else ""
                lines.append(f"  • {item.name} — {item.quantity}{recipe_tag}")
            items_text = "Items that were requested for the cart:\n" + "\n".join(lines)

        result_text = self._shopping_result or (
            "Shopping is in progress." if self.state == ChatState.EXECUTING
            else "Shopping encountered an error." if self.state == ChatState.ERROR
            else ""
        )

        # Last 12 turns of conversation for context
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:300]}"
            for m in self.conversation_history[-12:]
        )

        system = (
            "You are CookWithMe, a friendly Indian grocery shopping assistant. "
            "You help users plan recipes and add groceries to their Blinkit/Zepto cart. "
            "Be concise. Use ₹ for prices. Answer only what is asked."
        )

        prompt = (
            f"Session context:\n{items_text}\n\n"
            f"Shopping outcome:\n{result_text}\n\n"
            f"Conversation so far:\n{history_text}\n\n"
            f"USER: {user_message}\n\n"
            "Answer the user's question using the session context above. "
            "If they want to shop for something new, tell them to type their new request "
            "and you'll start fresh."
        )

        try:
            client = get_client()
            answer = await client.text(prompt=prompt, system=system, estimated_tokens=250)
            self._add_message("assistant", answer)
            return ChatResponse(
                session_id=self.session_id, state=self.state, message=answer,
            )
        except Exception as exc:
            log.error("freeform_llm_error", error=str(exc))
            # Graceful fallback using local data
            if self.confirmed_items:
                item_names = ", ".join(i.name for i in self.confirmed_items[:10])
                fallback = (
                    f"Items in the cart: {item_names}"
                    + (f" (and {len(self.confirmed_items)-10} more)" if len(self.confirmed_items) > 10 else "")
                    + f"\n{result_text}"
                    + "\n\nType a new request to start another shopping session!"
                )
            else:
                fallback = "Shopping complete! Type a new request to start again."
            self._add_message("assistant", fallback)
            return ChatResponse(
                session_id=self.session_id, state=self.state, message=fallback,
            )

    # ── task steps ────────────────────────────────────────────────────────────

    def build_task_steps(self, platform: str = None) -> list[TaskStep]:
        target = platform or self.platform
        steps = []
        for i, item in enumerate(self.confirmed_items):
            qty_ctx = f" (target: {item.quantity})" if item.quantity and item.quantity != "1 unit" else ""
            steps.append(TaskStep(
                step_id=f"chat_{target[:3]}_{self.session_id}_{i}",
                description=f"Add '{item.name}' to {target} cart{qty_ctx}",
                item_name=item.name,
                item_quantity=item.quantity if item.quantity and item.quantity != "1 unit" else None,
                expected_outcome=f"{item.name} added to cart",
            ))
        return steps

    def build_agent_context_extras(self) -> dict:
        intent = self.parsed_intent
        if not intent:
            return {}
        extras: dict = {}
        prefs = ProductPreferences(
            prefer_organic=bool(intent.prefer_organic),
            budget_level=intent.budget_level or "medium",
            dietary=intent.dietary,
            brand_preferences=self.user_profile.preferred_brands or [],
            quantity_sensitivity=self.user_profile.quantity_sensitivity,
        )
        extras["product_preferences"] = prefs
        if intent.budget_limit_inr:
            extras["budget_limit"] = intent.budget_limit_inr
        if intent.recipes and intent.servings:
            qty_reqs = {}
            for item in self.confirmed_items:
                if item.source_recipe:
                    qty_reqs[item.name] = QuantityRequirement(
                        min_quantity=item.quantity,
                        max_quantity=item.quantity,
                        ideal_quantity=item.quantity,
                        reasoning=f"For {item.source_recipe}",
                    )
            extras["recipe_context"] = RecipeContext(
                recipe_name=", ".join(intent.recipes),
                servings=intent.servings or 2,
                quantity_requirements=qty_reqs,
            )
        return extras

    # ── helpers ───────────────────────────────────────────────────────────────

    def _format_questions(self, questions: list[ClarifyingQuestion]) -> str:
        return "\n".join(f"• {q.question}" for q in questions[:2])

    def _format_list(self, items: list[ConfirmedItem], intent: ParsedIntent) -> str:
        if not items:
            return "I couldn't find any items to add. Please try again."
        lines = ["Here's what I'll add to your cart:\n"]
        current_recipe = None
        for item in items:
            if item.source_recipe != current_recipe:
                current_recipe = item.source_recipe
                if current_recipe:
                    lines.append(f"\n📦 {current_recipe}:")
            hint = _quantity_hint(item.quantity)
            lines.append(f"  • {item.name} — {item.quantity}{hint}")
        lines.append(f"\n{len(items)} item(s). Reply **yes** to start shopping or **no** to cancel.")
        return "\n".join(lines)

    _HERBS     = {"coriander","cilantro","dhaniya","mint","pudina","dill","suva","curry leaves","kari patta","parsley","basil"}
    _LEAFY     = {"spinach","palak","methi","fenugreek","cabbage","lettuce","kale"}
    _VEGETABLES= {"tomato","tomatoes","tamatar","onion","onions","pyaaz","potato","potatoes","aloo","carrot","carrots","gajar","capsicum","shimla mirch","peas","matar","beans","cauliflower","gobhi","brinjal","baingan","ladyfinger","bhindi","okra","cucumber","kheera"}
    _FRUITS    = {"banana","kela","apple","seb","orange","santra","mango","aam","grapes","angoor","pomegranate","anar"}

    def _default_quantity(self, item_name: str) -> str:
        lower = item_name.lower().strip()
        if any(h in lower for h in self._HERBS):      return "1 bunch"
        if any(h in lower for h in self._LEAFY):      return "250g"
        if any(h in lower for h in self._VEGETABLES): return "500g"
        if any(h in lower for h in self._FRUITS):     return "500g"
        return "1 unit"


# ── module-level helpers ───────────────────────────────────────────────────────

def _deduplicate(items: list[ConfirmedItem]) -> list[ConfirmedItem]:
    seen: set[str] = set()
    out: list[ConfirmedItem] = []
    for item in items:
        key = item.name.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _quantity_hint(qty: str) -> str:
    import re
    if not qty or qty == "1 unit":
        return ""
    m = re.match(r"^(\d+\.?\d*)\s*([a-zA-Z]+)", qty.strip())
    if not m:
        return ""
    val = float(m.group(1))
    unit = m.group(2).lower().rstrip("s")
    if unit in ("l","liter","litre") and val > 1:
        return f"  ← {int(val)}×1L combo"
    if unit in ("kg","kilogram") and val > 0.5:
        return f"  ← {int(val)}×1kg combo"
    return ""


# ── session registry ──────────────────────────────────────────────────────────

_sessions: dict[str, ChatSession] = {}


def get_or_create_session(session_id: str | None = None, platform: str = "blinkit") -> ChatSession:
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    session = ChatSession(session_id=session_id, platform=platform)
    _sessions[session.session_id] = session
    return session
