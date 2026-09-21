"""OpenAI function-calling orchestration for the read-only ERP assistant."""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
from django.conf import settings

from ai.permissions import can_search_documents
from ai.services.document_search import DEFAULT_SEARCH_LIMIT, MAX_QUERY_CHARACTERS, MAX_SEARCH_LIMIT
from ai.services.rag_sources import RAG_SYSTEM_INSTRUCTIONS, RAGSources
from ai.tools import TOOL_FUNCTIONS, TOOL_REQUIRED_ROLES, get_user_role


logger = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 3
TOOL_LIMIT = 50

_TOOL_DEFINITION_BY_NAME = {
    "search_products": {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Aktif ürünleri kod veya ada göre arar. Stok ya da fiyat döndürmez.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 100}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    "get_stock_by_product": {
        "type": "function",
        "function": {
            "name": "get_stock_by_product",
            "description": "Ürünün depo bazındaki mevcut, rezerve ve kullanılabilir stoklarını getirir.",
            "parameters": {
                "type": "object",
                "properties": {"code_or_name": {"type": "string", "minLength": 1, "maxLength": 100}},
                "required": ["code_or_name"],
                "additionalProperties": False,
            },
        },
    },
    "get_lot_details": {
        "type": "function",
        "function": {
            "name": "get_lot_details",
            "description": "Lot numarası, barkodu veya QR değeri ile lot izlenebilirlik bilgisini getirir.",
            "parameters": {
                "type": "object",
                "properties": {"lot_code": {"type": "string", "minLength": 1, "maxLength": 255}},
                "required": ["lot_code"],
                "additionalProperties": False,
            },
        },
    },
    "get_production_orders": {
        "type": "function",
        "function": {
            "name": "get_production_orders",
            "description": "Üretim emirlerini isteğe bağlı durum filtresiyle listeler.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["planned", "released", "in_progress", "quality_check", "completed", "cancelled"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": TOOL_LIMIT},
                },
                "additionalProperties": False,
            },
        },
    },
    "get_sales_orders": {
        "type": "function",
        "function": {
            "name": "get_sales_orders",
            "description": "Satış siparişlerini isteğe bağlı durum filtresiyle listeler. Müşteriler sadece kendi siparişlerini görebilir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["draft", "confirmed", "in_production", "ready_to_ship", "shipped", "completed", "cancelled"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": TOOL_LIMIT},
                },
                "additionalProperties": False,
            },
        },
    },
    "get_fire_records": {
        "type": "function",
        "function": {
            "name": "get_fire_records",
            "description": "En güncel üretim fire kayıtlarını listeler.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": TOOL_LIMIT}},
                "additionalProperties": False,
            },
        },
    },
    "search_documents": {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Yüklenen dokümanlarda prosedür, talimat ve açıklama arar. Canlı ERP verisi değildir. Dönen citation etiketlerini yanıtta kullan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARACTERS},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT, "default": DEFAULT_SEARCH_LIMIT},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
}


class LLMService:
    """LLM client that may call only the approved, read-only ERP tools."""

    def __init__(self):
        self.api_key = getattr(settings, "OPENAI_API_KEY", "")
        self.model = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")
        self.client = openai.OpenAI(api_key=self.api_key) if self.api_key else None

    def is_available(self):
        return bool(self.client and self.api_key)

    def tool_definitions_for(self, user) -> list[dict[str, Any]]:
        role = get_user_role(user)
        return [
            definition
            for name, definition in _TOOL_DEFINITION_BY_NAME.items()
            if role in TOOL_REQUIRED_ROLES[name]
            and (name != "search_documents" or can_search_documents(user))
        ]

    def ask(self, prompt, system_prompt, user, temperature=0.2):
        if not self.is_available():
            raise RuntimeError("LLM service is unavailable")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt + "\n\n" + RAG_SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ]
        seen_calls: set[str] = set()
        tools = self.tool_definitions_for(user)
        sources = RAGSources()

        for round_number in range(MAX_TOOL_ROUNDS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                if sources.documents and not can_search_documents(user):
                    return sources.failure()
                return sources.finalize((message.content or "").strip())

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                result = self._execute_tool_call(user, call, seen_calls)
                result = sources.record(call.function.name, result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            citation_instructions = sources.response_instructions()
            if citation_instructions:
                # All tool calls have matching results before this reminder.
                # Only server-assigned labels enter the system message; never
                # promote retrieved document content/metadata into instructions.
                messages.append({"role": "system", "content": citation_instructions})

            logger.info("AI completed tool round %s", round_number + 1)

        logger.warning("AI tool loop reached its maximum of %s rounds", MAX_TOOL_ROUNDS)
        return "İsteğinizi güvenli şekilde tamamlayamadım. Lütfen sorunuzu daha daraltarak tekrar deneyin."

    def _execute_tool_call(self, user, call, seen_calls: set[str]) -> dict[str, Any]:
        tool_name = call.function.name
        if tool_name not in TOOL_FUNCTIONS:
            logger.warning("AI requested an unknown tool: %s", tool_name)
            return {"ok": False, "error": "tool_not_available", "data": []}

        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            logger.warning("AI supplied invalid JSON arguments for tool %s", tool_name)
            return {"ok": False, "error": "invalid_tool_arguments", "data": []}

        if not isinstance(arguments, dict):
            logger.warning("AI supplied non-object arguments for tool %s", tool_name)
            return {"ok": False, "error": "invalid_tool_arguments", "data": []}

        if tool_name == "search_documents":
            if not can_search_documents(user):
                return {"ok": False, "error": "access_denied", "data": []}
            query = arguments.get("query")
            limit = arguments.get("limit", DEFAULT_SEARCH_LIMIT)
            if (
                set(arguments) - {"query", "limit"}
                or not isinstance(query, str)
                or not query.strip()
                or len(query) > MAX_QUERY_CHARACTERS
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_SEARCH_LIMIT
            ):
                return {"ok": False, "error": "invalid_tool_arguments", "data": []}
            # Equivalent whitespace/default-limit variants are the same call.
            arguments = {"query": query.strip(), "limit": limit}

        call_key = f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
        if call_key in seen_calls:
            logger.warning("AI repeated tool call %s", call_key)
            return {"ok": False, "error": "duplicate_tool_call", "data": []}
        seen_calls.add(call_key)

        try:
            return TOOL_FUNCTIONS[tool_name](user=user, **arguments)
        except TypeError:
            logger.warning("AI supplied unsupported arguments for tool %s", tool_name)
            return {"ok": False, "error": "invalid_tool_arguments", "data": []}
        except Exception:
            logger.exception("Read-only AI tool %s failed", tool_name)
            return {"ok": False, "error": "tool_unavailable", "data": []}
