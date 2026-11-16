"""
Gerador de título, descrição, gancho e hashtags por plataforma
com funcionamento 100% offline por templates e enriquecimento opcional por LLM.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

PLATFORM_HASHTAGS: Dict[str, List[str]] = {
    "tiktok": ["#fyp", "#clips", "#gaming", "#cortes", "#twitchbrasil"],
    "shorts": ["#shorts", "#youtubeshorts", "#clips", "#gaming", "#cortes"],
    "reels": ["#reels", "#reelsinstagram", "#clips", "#gaming", "#cortes"],
    "youtube": ["#youtube", "#clips", "#highlights", "#gaming", "#cortes"],
    "x": ["#cortes", "#clips", "#live", "#viral", "#highlights"],
}


def _extract_hook(transcript_text: str) -> str:
    clean = re.sub(r"\s+", " ", (transcript_text or "").strip())
    if not clean:
        return "Momento imperdível da live"
    first_sentence = re.split(r"[.!?]", clean)[0].strip()
    if len(first_sentence) > 68:
        first_sentence = first_sentence[:65].rstrip() + "..."
    return first_sentence or clean[:68]


def generate_post_copy(
    transcript_text: str,
    platform: str = "tiktok",
    channel_name: Optional[str] = None,
    use_llm: bool = False,
) -> Dict[str, Any]:
    """Gera título, descrição, gancho e hashtags para a plataforma alvo."""
    plat = (platform or "tiktok").lower()
    if plat not in PLATFORM_HASHTAGS:
        plat = "tiktok"

    hook = _extract_hook(transcript_text)
    base_tags = list(PLATFORM_HASHTAGS[plat])
    if channel_name:
        slug = re.sub(r"[^a-zA-Z0-9]", "", channel_name.lower())
        if slug:
            base_tags.insert(0, f"#{slug}")

    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # type: ignore

            client = anthropic.Anthropic()
            msg = client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=240,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Crie 1 título curto (máx 65 caracteres) e 1 descrição de 1 frase em português para {plat} "
                            f"baseado neste trecho: '{transcript_text[:400]}'. "
                            "Responda no formato:\nTITULO: ...\nDESCRICAO: ..."
                        ),
                    }
                ],
            )
            text = msg.content[0].text
            m_tit = re.search(r"TITULO:\s*(.+)", text)
            m_desc = re.search(r"DESCRICAO:\s*(.+)", text)
            if m_tit and m_desc:
                return {
                    "platform": plat,
                    "hook": hook,
                    "title": m_tit.group(1).strip()[:80],
                    "description": m_desc.group(1).strip(),
                    "hashtags": base_tags[:6],
                    "source": "anthropic",
                }
        except Exception:
            pass

    title = hook if len(hook) <= 65 else hook[:62] + "..."
    credit = f"Créditos: {channel_name}. " if channel_name else ""
    description = f"{hook}. {credit}Assista até o final!"

    return {
        "platform": plat,
        "hook": hook,
        "title": title,
        "description": description,
        "hashtags": base_tags[:6],
        "source": "template",
    }
