"""LLM wrappers for the chat bot, summarisation, and snippet completion.

Two providers are supported and selected via `AIBotConfig.provider`:
    - 'gemini'    — Google Generative Language API (default, cheapest)
    - 'anthropic' — Claude (high quality, costlier)

The Anthropic SDK is an optional dependency: if it's not installed (or no
API key is configured), the caller falls back to keyword-overlap matching
so the product still works on customers who haven't set up AI.

Gemini uses our own tiny REST wrapper (`ai_gemini.py`) so we don't need to
add the heavyweight google-generativeai SDK to deployment images.

Cache hits are 1h TTL on (org_id, message_text) so refreshing a chat or a
visitor re-sending the same question doesn't burn another API call.
"""
from __future__ import annotations

import logging
import hashlib
from typing import Optional

from django.core.cache import cache

from tracker.chat import ai_gemini

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


def _provider_ready(config) -> Optional[str]:
    """Returns the resolved provider name when callable, else None.
    Handles the 'no key configured' case once so call sites stay clean."""
    if not config or not getattr(config, 'api_key', ''):
        return None
    provider = (config.provider or '').lower()
    if provider == 'anthropic':
        return 'anthropic' if _SDK_AVAILABLE else None
    if provider == 'gemini':
        return 'gemini'
    return None


def _call_llm(config, *, system: str, messages: list[dict],
              max_tokens: int = 400) -> Optional[str]:
    """Provider-agnostic single-turn completion.

    `messages` is the Anthropic shape: [{'role': 'user'|'assistant', 'content'}].
    For Gemini we translate 'assistant' → 'model' inside the wrapper.
    """
    provider = _provider_ready(config)
    if not provider:
        return None
    api_key = config.api_key_plain
    if not api_key:
        return None
    model = config.model_name or ('claude-haiku-4-5-20251001' if provider == 'anthropic' else 'gemini-2.0-flash')

    if provider == 'gemini':
        return ai_gemini.generate(api_key, model, system, messages, max_tokens=max_tokens)

    # Anthropic
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        out = ''.join(getattr(b, 'text', '') for b in resp.content).strip()
        return out or None
    except Exception:
        logger.warning('anthropic call failed', exc_info=True)
        return None


def _build_kb_context(org, limit: int = 12) -> str:
    """Pull the org's knowledge base + AIBotKnowledge entries as compact context.

    We keep this small — unbounded context still costs money per request.
    12 entries × ~200 chars ≈ 2.4k tokens, fine.
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
    """Ask the configured LLM for a reply. Returns None to fall back to KB matching."""
    if not _provider_ready(config):
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

    out = _call_llm(config, system=system, messages=messages, max_tokens=400)
    if out:
        cache.set(cache_key, out, timeout=3600)
    return out


def summarize_chat(config, room) -> Optional[dict]:
    """Generate a 1-2 sentence summary + topic tags for a closed chat."""
    if not _provider_ready(config):
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

    out = _call_llm(
        config,
        system=SUMMARY_SYSTEM,
        messages=[{'role': 'user', 'content': transcript}],
        max_tokens=300,
    )
    if not out:
        return None
    try:
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
        logger.warning('summarize_chat: JSON parse failed for room=%s', getattr(room, 'room_id', None), exc_info=True)
        return None


# ────────────────────────────────────────────────
# NEW: AI Snippet completion (slash-command driven)
# ────────────────────────────────────────────────

# Built-in templates the LLM uses as guidance. Agents can extend per-org via
# CannedResponse rows (shortcut field) — those override these.
_BUILTIN_SNIPPETS = {
    'refund': "Help the agent issue a refund. Confirm the order, processing time, and apologize briefly.",
    'shipping': "Reassure the customer about shipping status, give an ETA, and offer help if needed.",
    'pricing': "Explain the pricing tiers concisely. Mention the free plan + the most relevant upgrade.",
    'thanks': "Warmly thank the customer and close the chat positively.",
    'greet': "Greet the visitor by name (if available) and ask how you can help.",
    'apology': "Acknowledge the customer's frustration sincerely and outline the next concrete step.",
    'feature': "The feature isn't available yet — be honest, explain the workaround if any, offer to log it.",
    'bug': "Acknowledge the bug, ask 1-2 minimal clarifying questions, set expectation on follow-up.",
    'follow_up': "Polite follow-up after a delay — recap last context and ask if the customer still needs help.",
    'escalate': "Escalate to a senior agent / specialist. Promise an update and a timeline.",
}


def suggest_snippet(config, room, command: str, transcript_tail: list[dict],
                    visitor_name: str = '') -> Optional[str]:
    """Generate a personalised reply for a slash-command shortcut.

    The agent types e.g. `/refund` and we return a full draft message that
    references the visitor's name and the actual chat context. The agent
    can edit it before sending — this is *suggestion*, not auto-send.
    """
    if not _provider_ready(config):
        return None
    cmd = (command or '').strip().lower().lstrip('/')
    if not cmd:
        return None

    # Allow per-org overrides via CannedResponse shortcut.
    org = room.organization
    org_template = None
    if org:
        from tracker.chat.models import CannedResponse
        cr = CannedResponse.objects.filter(organization=org, shortcut__iexact=cmd).first()
        if cr:
            org_template = cr.message

    intent = org_template or _BUILTIN_SNIPPETS.get(cmd)
    if not intent:
        # Free-form: let the agent type `/anything` — model improvises.
        intent = f"Compose a brief, helpful reply about: {cmd}"

    # Context: last ~6 messages so the suggestion grounds in actual conversation.
    convo_lines = []
    for m in transcript_tail[-6:]:
        role = 'Visitor' if m.get('role') == 'visitor' else 'Agent'
        text = (m.get('content') or '').strip()
        if text:
            convo_lines.append(f'{role}: {text[:300]}')
    convo = '\n'.join(convo_lines) or '(no prior messages)'

    visitor_label = visitor_name.strip() or 'the visitor'
    kb = _build_kb_context(org, limit=6) if org else ''

    system = (
        "You are an assistant that drafts SHORT chat replies for a human "
        "support agent. Write in the agent's voice, 1-3 sentences, friendly "
        "and concrete. Use the visitor's first name if appropriate. NEVER "
        "invent facts (order numbers, prices, dates) — leave [bracketed "
        "placeholders] if specifics are unknown so the agent can fill them. "
        "Output ONLY the reply text, no preamble, no quotes."
    )
    user = (
        f"Visitor name: {visitor_label}\n"
        f"Business name: {org.name if org else 'us'}\n"
        f"Agent intent (slash command '/{cmd}'): {intent}\n\n"
        f"Recent conversation:\n{convo}\n\n"
        f"Knowledge base hints:\n{kb}\n\n"
        "Draft the agent's next message:"
    )

    return _call_llm(
        config,
        system=system,
        messages=[{'role': 'user', 'content': user}],
        max_tokens=220,
    )


# ────────────────────────────────────────────────
# NEW: On-demand message translation
# ────────────────────────────────────────────────

def translate_text(config, text: str, target_language: str = 'en',
                   source_language: str = '') -> Optional[str]:
    """Translate `text` into `target_language`. Returns the translated string
    or None on failure. Used by the per-message "Translate" button on the
    agent dashboard and visitor widget."""
    if not _provider_ready(config):
        return None
    if not text or not target_language:
        return None
    # Don't pay for translations we already have.
    cache_key = _cache_key(f'tr:{target_language}', 0, text)
    cached = cache.get(cache_key)
    if cached:
        return cached

    if source_language:
        prompt = (
            f"Translate the following {source_language} text into "
            f"{target_language}. Output ONLY the translation, no quotes, "
            f"no explanation.\n\nText: {text}"
        )
    else:
        prompt = (
            f"Translate the following text into {target_language}. Detect "
            "the source language automatically. Output ONLY the translation, "
            "no quotes, no explanation.\n\nText: " + text
        )
    out = _call_llm(
        config,
        system="You are a precise bilingual translator. Preserve tone, names, and emojis.",
        messages=[{'role': 'user', 'content': prompt}],
        max_tokens=400,
    )
    if out:
        cache.set(cache_key, out, timeout=86400)  # translations are stable, cache 1 day
    return out
