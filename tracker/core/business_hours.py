"""Business hours / "is the support team open?" check.

Used by widget endpoints so the chat box can render an offline message
instead of pretending an agent will reply at 3am.
"""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo


def is_organization_open(org, now=None):
    """Return True when current time falls within the org's business hours.

    Returns True when the toggle is off (default behavior — don't break
    existing widgets). Treats overnight ranges (start > end, e.g. 22:00–06:00)
    as crossing midnight, so a graveyard-shift team can still be 'open'.
    """
    if not org or not getattr(org, 'business_hours_enabled', False):
        return True

    tz_name = (org.business_hours_timezone or 'UTC').strip() or 'UTC'
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo('UTC')

    current = (now or datetime.now(tz)).astimezone(tz).time()
    start = org.business_hours_start
    end = org.business_hours_end

    if start <= end:
        return start <= current <= end
    # Overnight window: open if after start OR before end.
    return current >= start or current <= end
