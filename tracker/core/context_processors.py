from tracker.core.models import Website


def website_context(request):
    """Inject user's websites and the current selection into all templates.

    Exposes:
      user_websites           — list[Website] this user is allowed to see
      selected_website_ids    — list[int] currently selected (empty = "All")
      selected_website_id     — int or None (only when exactly one is selected;
                                kept for templates that haven't migrated yet)
      selected_website_label  — str shown in the sidebar pill
    """
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

    # Read selection (supports legacy single-int session key)
    raw = request.session.get('selected_website_ids')
    selected_ids = []
    if isinstance(raw, list):
        for v in raw:
            try:
                selected_ids.append(int(v))
            except (TypeError, ValueError):
                continue
    else:
        legacy = request.session.get('selected_website_id')
        if legacy:
            try:
                selected_ids = [int(legacy)]
            except (TypeError, ValueError):
                pass

    # Trim selection to what the user is actually allowed to see
    allowed_ids = {w.id for w in websites}
    selected_ids = [i for i in selected_ids if i in allowed_ids]

    # Compute the label shown in the sidebar pill
    if not selected_ids:
        label = 'All websites'
    elif len(selected_ids) == 1:
        match = next((w for w in websites if w.id == selected_ids[0]), None)
        label = match.name if match else 'All websites'
    else:
        label = f'{len(selected_ids)} websites'

    return {
        'user_websites': websites,
        'selected_website_ids': selected_ids,
        'selected_website_id': selected_ids[0] if len(selected_ids) == 1 else None,
        'selected_website_label': label,
    }
