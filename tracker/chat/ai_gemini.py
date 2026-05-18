"""Tiny REST wrapper around Google's Generative Language API (Gemini).

Why not the official google-generativeai SDK:
    The SDK pulls in protobuf + grpcio (~30MB of dependencies, slow imports)
    just to call a JSON-over-HTTPS endpoint. The raw REST API is simple
    enough that a 60-line wrapper covers every call site we have.

Endpoint reference:
    https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}

The shape mirrors `tracker.chat.ai` (Anthropic wrapper) so callers in
`chat.ai` can dispatch by `config.provider` without branching on every
parameter.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
_TIMEOUT = 30  # seconds — Gemini Flash usually returns in <3s


def generate(api_key: str, model: str, system: str, messages: list[dict],
             *, max_tokens: int = 400, temperature: float = 0.7) -> Optional[str]:
    """Single-shot text generation. `messages` mirrors the Anthropic shape:
    [{'role': 'user'|'assistant', 'content': str}, ...]

    Returns the model's reply text, or None on failure (caller falls back).
    """
    if not api_key or not model:
        return None

    # Gemini uses {'role': 'user'|'model', 'parts': [{'text': ...}]}.
    contents = []
    for m in messages:
        role = 'user' if m.get('role') == 'user' else 'model'
        text = (m.get('content') or '').strip()
        if not text:
            continue
        contents.append({'role': role, 'parts': [{'text': text}]})
    if not contents:
        return None

    body: dict = {
        'contents': contents,
        'generationConfig': {
            'maxOutputTokens': max_tokens,
            'temperature': temperature,
        },
    }
    # System prompts go in their own field on Gemini, not as a message role.
    if system:
        body['systemInstruction'] = {'parts': [{'text': system}]}

    url = _ENDPOINT.format(model=model, key=api_key)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.warning('gemini HTTP %s: %s', e.code, e.read()[:200].decode('utf-8', 'ignore'))
        return None
    except Exception:
        logger.warning('gemini call failed', exc_info=True)
        return None

    # Response: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    try:
        cands = payload.get('candidates') or []
        if not cands:
            # Often the prompt was blocked — surface the reason in logs but
            # don't propagate (caller falls back).
            logger.info('gemini: no candidates. feedback=%s',
                        payload.get('promptFeedback'))
            return None
        parts = cands[0].get('content', {}).get('parts') or []
        text = ''.join(p.get('text', '') for p in parts).strip()
        return text or None
    except Exception:
        logger.warning('gemini: unexpected response shape', exc_info=True)
        return None
