from tracker.core.models import Website


def website_context(request):
    """Inject user's websites and selected website ID into all templates."""
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {}

    from tracker.core.views import get_user_org
    from tracker.chat.models import AgentWebsiteAccess

    org = get_user_org(request.user)
    if not org:
        return {}

    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))

    if is_owner:
        websites = list(Website.objects.filter(organization=org))
    else:
        accessible_ids = AgentWebsiteAccess.objects.filter(agent=profile).values_list('website_id', flat=True)
        if accessible_ids:
            websites = list(Website.objects.filter(id__in=accessible_ids))
        else:
            websites = list(Website.objects.filter(organization=org))

    selected_id = request.session.get('selected_website_id')

    return {
        'user_websites': websites,
        'selected_website_id': int(selected_id) if selected_id else None,
    }
