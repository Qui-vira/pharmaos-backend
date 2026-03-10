"""
PharmaOS AI - AI Provider Abstraction Layer
Supports OpenAI GPT-4o for MVP. Designed to be swappable to Anthropic or self-hosted models.

CRITICAL GUARDRAIL:
AI must ONLY perform: symptom intake, question flow, summary generation, intent detection.
AI must NEVER generate: diagnosis, treatment recommendations, drug plans.
These are enforced via system prompts and the PharmacistAction approval gate.
"""

import json
import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class AIProvider:
    """Abstract base for AI providers."""

    async def chat(self, system_prompt: str, messages: list[dict], temperature: float = 0.3) -> str:
        raise NotImplementedError


class OpenAIProvider(AIProvider):
    """OpenAI GPT-4o integration."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.base_url = "https://api.openai.com/v1/chat/completions"

    async def chat(self, system_prompt: str, messages: list[dict], temperature: float = 0.3) -> str:
        if not self.api_key:
            logger.warning("LLM API key not configured")
            return ""

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                *messages,
            ],
            "temperature": temperature,
            "max_tokens": 1000,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.base_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"AI provider error: {e}")
            return ""


# ─── System Prompts ─────────────────────────────────────────────────────────

CONSULTATION_INTAKE_PROMPT = """You are a pharmacy consultation assistant for PharmaOS AI.
Your role is to gather patient symptoms through a structured interview.

STRICT RULES:
1. You MUST NEVER provide a diagnosis.
2. You MUST NEVER recommend specific medications.
3. You MUST NEVER suggest treatment plans.
4. You ONLY ask follow-up questions to understand the patient's symptoms.
5. After 3-5 questions, generate a structured summary for the pharmacist.

When you have enough information, respond with a JSON block:
{"action": "summary", "summary": "Patient reports..."}

Otherwise, ask the next relevant follow-up question.

Be empathetic and professional. Use simple language. The patient may speak Nigerian English or Pidgin."""

INTENT_DETECTION_PROMPT = """You are an intent detection system for PharmaOS AI.
Analyze the user's message and extract the intent and any entities.

Supported intents:
- place_order: User wants to order drugs
- check_stock: User asks about availability
- ask_price: User asks about pricing
- reorder_previous_order: User wants to repeat a past order
- ask_pickup_time: User asks when order is ready
- ask_delivery_status: User asks about delivery
- speak_to_human: User wants to talk to a person
- product_substitution_request: User asks for alternatives
- start_consultation: User describes symptoms or health issues
- reminder_response: User responds to a reminder (PICKUP/DELIVERY/STOP)

Respond ONLY with JSON:
{
    "intent": "place_order",
    "confidence": 0.95,
    "entities": [
        {"type": "product", "value": "paracetamol", "quantity": 10},
        {"type": "product", "value": "amoxicillin", "quantity": 5}
    ]
}"""

ORDER_EXTRACTION_PROMPT = """You are an order extraction system for PharmaOS AI.
Extract product names and quantities from the user's natural language order.

Respond ONLY with JSON:
{
    "products": [
        {"name": "paracetamol 500mg", "quantity": 10},
        {"name": "amoxicillin 500mg", "quantity": 5}
    ]
}

If you cannot determine the quantity, default to 1.
Normalize drug names to standard forms where possible."""


# ─── Service Functions ──────────────────────────────────────────────────────


def get_ai_provider() -> AIProvider:
    """Factory function to get the configured AI provider."""
    # Currently only OpenAI. Add Anthropic/local model switches here.
    return OpenAIProvider()


async def detect_intent(message: str) -> dict:
    """Detect user intent from a WhatsApp message."""
    provider = get_ai_provider()
    response = await provider.chat(
        system_prompt=INTENT_DETECTION_PROMPT,
        messages=[{"role": "user", "content": message}],
        temperature=0.1,
    )

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse intent response: {response}")
        return {"intent": "unknown", "confidence": 0, "entities": []}


async def extract_order_items(message: str) -> list[dict]:
    """Extract product names and quantities from a natural language order."""
    provider = get_ai_provider()
    response = await provider.chat(
        system_prompt=ORDER_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": message}],
        temperature=0.1,
    )

    try:
        data = json.loads(response)
        return data.get("products", [])
    except json.JSONDecodeError:
        logger.error(f"Failed to parse order extraction: {response}")
        return []


async def process_consultation_message(
    conversation_history: list[dict],
) -> dict:
    """
    Process a consultation message. Returns either a follow-up question or a summary.

    GUARDRAIL: The system prompt explicitly forbids diagnosis or treatment.
    """
    provider = get_ai_provider()
    response = await provider.chat(
        system_prompt=CONSULTATION_INTAKE_PROMPT,
        messages=conversation_history,
        temperature=0.3,
    )

    # Check if AI returned a summary
    try:
        if '{"action": "summary"' in response:
            data = json.loads(response)
            return {"type": "summary", "content": data.get("summary", response)}
    except json.JSONDecodeError:
        pass

    return {"type": "question", "content": response}
