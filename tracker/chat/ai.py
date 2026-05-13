"""LLM wrappers for the chat bot and chat summarisation.

Anthropic Claude (Haiku 4.5 by default) is the preferred backend — it's fast
and cheap enough for live chat. The `anthropic` SDK is an optional dependency:
when missing OR when an org hasn't configured an API key, callers automatically
fall back to the original keyword-overlap matcher so the product still works.

Cache hits are 1h ttl on (org_id, message_text) so refreshing a chat or a
visitor re-sending the same question doesn't burn another API call.
"""
from __future__ import annotations

import logging
import hashlib
from typing import Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)

try:
    import anthropic  # type: ignore
    _SDK_AVAILABLE = True
except ImportError:
    anthropic = None  # type: ignore
    _SDK_AVAILABLE = False


SUMMARY_SYSTEM = (
    "You are a support-conversation summarizer. Read the chat transcript and "
    "produce: (1) a 1-2 sentence summary of what the customer wanted and what "
    "happened, (2) up to 3 short topic tags. Reply ONLY as JSON: "
    '{"summary": "...", "topics": ["tag1", "tag2"]}'
)


def _cache_key(prefix: str, org_id: int, text: str) -> str:
    h = hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()[:16]
    return f'ai:{prefix}:{org_id}:{h}'


def _build_kb_context(org, limit: int = 12) -> str:
    """Pull the org's knowledge base + AIBotKnowledge entries as compact context.

    We keep this small — Haiku is cheap but unbounded context still costs money
    per request. 12 entries × ~200 chars ≈ 2.4k tokens, fine.
    """
    from tracker.chat.models import AIBotKnowledge, KBArticle
    chunks = []
    for k in AIBotKnowledge.objects.filter(organization=org, is_active=True).order_by('-priority')[:limit]:
        chunks.append(f'Q: {k.question}\nA: {k.answer}')
    for a in KBArticle.objects.filter(organization=org, is_published=True).order_by('-views_count')[:limit // 2]:
        snippet = (a.content or '')[:300]
        chunks.append(f'Article: {a.title}\n{snippet}')
    return '\n\n'.join(chunks) if chunks else '(no knowledge base entries yet)'


def generate_bot_reply(config, room, visitor_message: str, transcript_tail: list[dict]) -> Optional[str]:
    """Ask Claude for a reply, falling back to None when not configured.

    Args:
        config: AIBotConfig instance (must have provider='anthropic' + api_key).
        room: ChatRoom instance — used for org context lookup.
        visitor_message: latest message from visitor.
        transcript_tail: list of {role, content} dicts (last ~6 messages) for context.

    Returns the reply text or None to signal "fall back to KB matching".
    """
    if not _SDK_AVAILABLE:
        logger.info('anthropic SDK not installed; using keyword fallback')
        return None
    if config.provider != 'anthropic' or not config.api_key:
        return None
    api_key = config.api_key_plain
    if not api_key:
        return None

    org = room.organization
    cache_key = _cache_key('reply', org.id if org else 0, visitor_message)
    cached = cache.get(cache_key)
    if cached:
        return cached

    kb = _build_kb_context(org) if org else ''
    system = (
        f"You are {config.bot_name}, a helpful customer-support assistant for "
        f"{org.name if org else 'this business'}. Reply concisely (1-3 sentences). "
        "If the answer isn't in the knowledge base, say so honestly and suggest "
        "the visitor wait for a human agent. Never invent facts.\n\n"
        f"Knowledge base:\n{kb}\n\n"
        f"{config.system_prompt or ''}"
    ).strip()

    messages = []
    for m in transcript_tail[-6:]:
        role = 'user' if m.get('role') == 'visitor' else 'assistant'
        content = (m.get('content') or '').strip()
        if content:
            messages.append({'role': role, 'content': content})
    if not messages or messages[-1]['role'] != 'user':
        messages.append({'role': 'user', 'content': visitor_message})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=config.model_name or 'claude-haiku-4-5-20251001',
            max_tokens=400,
            system=system,
            messages=messages,
        )
        # `resp.content` is a list of blocks — for plain text replies the first
        # block has `.text`. Defensive: skip non-text blocks.
        out = ''.join(getattr(b, 'text', '') for b in resp.content).strip()
        if out:
            cache.set(cache_key, out, timeout=3600)
        return out or None
    except Exception:
        logger.warning('anthropic call failed for org=%s', getattr(org, 'id', None), exc_info=True)
        return None


def summarize_chat(config, room) -> Optional[dict]:
    """Generate a 1-2 sentence summary + topic tags for a closed chat.

    Returns {'summary': str, 'topics': [str, ...]} or None on failure /
    when AI isn't configured.
    """
    if not _SDK_AVAILABLE or config.provider != 'anthropic' or not config.api_key:
        return None
    api_key = config.api_key_plain
    if not api_key:
        return None
    from tracker.chat.models import Message
    msgs = list(Message.objects.filter(room=room).order_by('timestamp')[:80])
    if not msgs:
        return None
    transcript_lines = []
    for m in msgs:
        who = 'Visitor' if m.sender_type == 'visitor' else 'Agent' if m.sender_type == 'agent' else 'System'
        transcript_lines.append(f'{who}: {m.content[:400]}')
    transcript = '\n'.join(transcript_lines)

    cache_key = _cache_key('summary', room.organization_id or 0, str(room.id) + str(len(msgs)))
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=config.model_name or 'claude-haiku-4-5-20251001',
            max_tokens=300,
            system=SUMMARY_SYSTEM,
            messages=[{'role': 'user', 'content': transcript}],
        )
        out = ''.join(getattr(b, 'text', '') for b in resp.content).strip()
        # Defensive JSON parse — Claude sometimes wraps in markdown.
        import json, re
        out = re.sub(r'^```(?:json)?|```$', '', out, flags=re.MULTILINE).strip()
        data = json.loads(out)
        summary = (data.get('summary') or '')[:1000]
        topics = data.get('topics') or []
        if isinstance(topics, list):
            topics = ','.join(str(t)[:30] for t in topics[:5])
        result = {'summary': summary, 'topics': topics}
        cache.set(cache_key, result, timeout=3600)
        return result
    except Exception:
        logger.warning('summarize_chat failed for room=%s', getattr(room, 'room_id', None), exc_info=True)
        return None
