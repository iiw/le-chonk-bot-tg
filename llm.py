"""OpenAI-compatible API layer (Mistral, OpenAI, OpenRouter, ...)."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI


@dataclass
class BufferedMessage:
    """A single message from the chat buffer."""

    user_id: int | None
    sender: str
    text: str = ""
    image_path: str | None = None
    is_bot: bool = False


class LLMError(Exception):
    """An API call failed."""


def make_client(api_key: str, base_url: str) -> AsyncOpenAI:
    # 300 s timeout — leaves room for a full 3k-token answer (measured ~24 tok/s ≈ 130 s);
    # otherwise long generations time out and the SDK retries them (×3 attempts).
    kwargs: dict = {"api_key": api_key, "timeout": 300.0, "max_retries": 2}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def image_to_data_uri(path: str | Path) -> str:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    b = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b}"


def _img_part(uri: str, style: str) -> dict:
    # style="object" — OpenAI-style: {"url": ...}; style="string" — a bare URL string (some providers)
    return {"type": "image_url", "image_url": {"url": uri} if style == "object" else uri}


def build_llm_messages(
    system_prompt: str,
    buffer: list[BufferedMessage],
    bot_id: int | None = None,
    max_images: int = 0,
    image_style: str = "object",
) -> list[dict]:
    """Builds the chat.completions messages: system + chat history.

    Other participants' lines -> role=user as "Name: text";
    the bot's own messages -> role=assistant;
    the last max_images images are embedded as image_url (data:URI).
    """
    img_indices = [i for i, m in enumerate(buffer) if m.image_path]
    keep = set(img_indices[-max_images:]) if max_images > 0 else set()

    messages: list[dict] = []
    for i, m in enumerate(buffer):
        is_bot = m.is_bot or (bot_id is not None and m.user_id == bot_id)
        role = "assistant" if is_bot else "user"
        label = "" if is_bot else f"{m.sender}: "
        body = (m.text or "").strip()
        if m.image_path and i not in keep:
            body = (body + " [фото]").strip()

        if m.image_path and i in keep:
            parts: list[dict] = [{"type": "text", "text": (label + body).strip() or "[фото]"}]
            try:
                parts.append(_img_part(image_to_data_uri(m.image_path), image_style))
            except Exception:
                pass
            messages.append({"role": role, "content": parts})
        else:
            content = (label + body).strip()
            if not content:
                continue
            # merge consecutive participant lines into one block,
            # to avoid back-to-back user messages
            if (
                role == "user"
                and messages
                and messages[-1]["role"] == "user"
                and isinstance(messages[-1]["content"], str)
            ):
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})

    return [{"role": "system", "content": system_prompt}] + messages


def _has_image_parts(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages)


def _flip_image_style(messages: list[dict]) -> list[dict]:
    """Flips the image_url format object <-> string (for compatibility across APIs)."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            new_parts = []
            for p in c:
                if p.get("type") == "image_url":
                    iu = p.get("image_url")
                    if isinstance(iu, dict):
                        new_parts.append({"type": "image_url", "image_url": iu.get("url", "")})
                    else:
                        new_parts.append({"type": "image_url", "image_url": {"url": iu}})
                else:
                    new_parts.append(p)
            out.append({**m, "content": new_parts})
        else:
            out.append(m)
    return out


def _extract_text(content) -> str:
    """Extracts the answer from message.content.

    mistral-large-4 returns content as a list of parts:
    [{"type": "thinking", ...}, {"type": "text", "text": "..."}] —
    only text parts are taken; thinking is never shown to the user.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return "" if content is None else str(content)


async def call_llm(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.4,
    usage_out: dict | None = None,
) -> str:
    """chat.completions call; on an image error — one retry in the other format.

    usage_out, when passed, is filled with the token counters of the successful
    call (prompt_tokens / cached_tokens / completion_tokens) — numbers only.
    """

    async def _once(msgs: list[dict]) -> str:
        resp = await client.chat.completions.create(
            model=model, messages=msgs, max_tokens=max_tokens, temperature=temperature
        )
        if usage_out is not None:
            u = getattr(resp, "usage", None)
            details = getattr(u, "prompt_tokens_details", None) if u else None
            usage_out.update(
                {
                    "prompt_tokens": getattr(u, "prompt_tokens", None),
                    "cached_tokens": getattr(details, "cached_tokens", None),
                    "completion_tokens": getattr(u, "completion_tokens", None),
                }
            )
        return _extract_text(resp.choices[0].message.content).strip()

    try:
        return await _once(messages)
    except Exception as first_err:
        if _has_image_parts(messages):
            try:
                return await _once(_flip_image_style(messages))
            except Exception:
                pass
        raise LLMError(str(first_err)) from first_err
