"""Markdown (from the LLM) → Telegram HTML (parse_mode="HTML").

Supports:
  **bold**, __bold__, *italic*, _italic_, ~~strikethrough~~,
  `code`, ```code blocks```, [links](url), # headings (→ a bold line).

Everything else is escaped (& < >) so Telegram never complains about
unknown entities. Code/links are stashed before escaping and restored
afterwards as ready HTML.
"""

from __future__ import annotations

import re

_MARK = "\x00"  # stash marker for ready-made HTML

_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_tg_html(text: str) -> str:
    """Converts Markdown into Telegram-friendly HTML (parse_mode=HTML)."""
    stash: list[str] = []

    def put(html: str) -> str:
        stash.append(html)
        return f"{_MARK}{len(stash) - 1}{_MARK}"

    def _block(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = _escape(m.group(2).rstrip("\n"))
        cls = f' class="language-{lang}"' if lang else ""
        return put(f"<pre><code{cls}>{code}</code></pre>")

    def _link(m: re.Match) -> str:
        url = _escape(m.group(2)).replace('"', "%22")
        return put(f'<a href="{url}">{_escape(m.group(1))}</a>')

    # 1) code and links first, pre-escaping; their content is escaped explicitly
    text = _CODE_BLOCK_RE.sub(_block, text)
    text = _INLINE_CODE_RE.sub(lambda m: put(f"<code>{_escape(m.group(1))}</code>"), text)
    text = _LINK_RE.sub(_link, text)

    # 2) escape whatever is left as plain text
    text = _escape(text)

    # 3) inline markup
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\w*])__(?=\S)(.+?)(?<=\S)__(?![\w*])", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![_\w])_(?=\S)(.+?)(?<=\S)_(?![_\w])", r"<i>\1</i>", text, flags=re.DOTALL)

    # 4) headings → bold line
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)

    # 5) restore the stashed HTML
    for i, html in enumerate(stash):
        text = text.replace(f"{_MARK}{i}{_MARK}", html)
    return text
