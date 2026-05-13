"""Plan-tier feature gating: decorator + helpers.

Flow:
  - check_feature(org, key)  → True/False using Subscription.plan_limits.
  - @requires_feature(key)   → 402 JSON for AJAX, plan_required template otherwise.

Superusers bypass all gates so support staff can preview gated screens.
"""
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render


def _resolve_org(request):
    profile = getattr(request.user, 'agent_profile', None)
    return profile.organization if profile else None


def check_feature(org, feature):
    """Resolve feature flag from the org's active subscription.

    Lazy-creates a 'free' subscription if one is missing — keeps callers simple
    and matches the convention already used in core.views.get_plan_limits.
    """
    if not org:
        return False
    from tracker.core.models import Subscription
    sub = getattr(org, 'subscription', None)
    if sub is None:
        sub, _ = Subscription.objects.get_or_create(
            organization=org, defaults={'plan': 'free', 'status': 'active'},
        )
    return bool(sub.plan_limits.get(feature, False))


def _wants_json(request):
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept


def requires_feature(feature, plan_label='Pro'):
    """Gate a view behind a plan feature flag.

    Args:
        feature: key from Subscription.plan_limits (e.g. 'ai_bot').
        plan_label: shown in the upgrade screen (e.g. 'Pro' or 'Enterprise').
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.user.is_authenticated and request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            org = _resolve_org(request)
            if check_feature(org, feature):
                return view_func(request, *args, **kwargs)
            pretty = feature.replace('_', ' ').title()
            if _wants_json(request):
                return JsonResponse({
                    'error': f'{pretty} requires the {plan_label} plan.',
                    'code': 'PLAN_UPGRADE_REQUIRED',
                    'feature': feature,
                    'required_plan': plan_label,
                }, status=402)
            return render(request, 'dashboard/plan_required.html', {
                'feature': pretty,
                'required_plan': plan_label,
            }, status=402)
        return wrapped
    return decorator
