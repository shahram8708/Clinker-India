from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterable, TypedDict

import google.generativeai as genai

MAX_MESSAGES = 5
MAX_CHARS = 1200
PAGE_CONTEXT_LIMIT = 900


class ChatMessage(TypedDict):
    role: str
    content: str


class MissingAPIKey(RuntimeError):
    """Raised when GOOGLE_API_KEY is not configured."""


def _normalize_text(value: str, limit: int = MAX_CHARS) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip() + " ..."
    return cleaned


def _sanitize_messages(messages: Iterable[dict] | None) -> list[ChatMessage]:
    safe_messages: list[ChatMessage] = []
    for raw in list(messages or [])[-MAX_MESSAGES:]:
        role = "user" if raw.get("role") == "user" else "model"
        text = _normalize_text(str(raw.get("content", "")))
        if not text:
            continue
        safe_messages.append({"role": role, "content": text})
    return safe_messages


@lru_cache(maxsize=1)
def _get_model() -> genai.GenerativeModel:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise MissingAPIKey("GOOGLE_API_KEY is not configured")

    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=(
            "You are the in-product AI guide for the Clinker India dashboard. "
            "Be concise, use Markdown, and prefer short bullet lists. "
            "Lean on the provided page context when the user asks about the current page. "
            "If information is missing, say so briefly."
        ),
        generation_config={
            "temperature": 0.35,
            "top_p": 0.9,
            "max_output_tokens": 500,
        },
    )


def generate_chat_reply(messages: Iterable[dict], page_context: str = "") -> str:
    history = _sanitize_messages(messages)
    if not history:
        raise ValueError("A user message is required")

    prepared = []
    for idx, msg in enumerate(history):
        content = msg["content"]
        is_latest_user = idx == len(history) - 1 and msg["role"] == "user"
        if is_latest_user and page_context:
            content = f"{content}\n\nPage context:\n{_normalize_text(page_context, PAGE_CONTEXT_LIMIT)}"
        prepared.append({"role": msg["role"], "parts": [{"text": content}]})

    model = _get_model()
    response = model.generate_content(prepared)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Model returned an empty response")
    return text
