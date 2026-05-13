from django.shortcuts import render


PUBLIC_PAGES = [
    {
        'title': 'Compare',
        'url': '/compare/',
        'summary': 'Compare LiveVisitorHub with Google Analytics, Microsoft Clarity, Intercom, Hotjar, Tawk.to and more.',
        'keywords': 'compare comparison vs alternative google analytics clarity intercom hotjar tawk',
    },
    {
        'title': 'Features',
        'url': '/features/',
        'summary': 'Complete feature list — AI bot, chatbot builder, knowledge base, WhatsApp, segments, departments, SLA, surveys, PWA & more.',
        'keywords': 'features ai bot chatbot knowledge base whatsapp segments departments sla surveys pwa dark mode',
    },
    {
        'title': 'About Us',
        'url': '/about/',
        'summary': 'Our story, mission, numbers, and team behind LiveVisitorHub.',
        'keywords': 'about company mission story team',
    },
    {
        'title': 'Privacy Policy',
        'url': '/privacy/',
        'summary': 'How we collect, use, store, and protect account and visitor data.',
        'keywords': 'privacy data gdpr cookies security retention',
    },
    {
        'title': 'Terms of Service',
        'url': '/terms/',
        'summary': 'Rules, acceptable use, billing, liability, and account terms.',
        'keywords': 'terms service billing liability acceptable use',
    },
    {
        'title': 'Refund Policy',
        'url': '/refund/',
        'summary': 'Eligibility, timelines, and process for refunds and cancellations.',
        'keywords': 'refund cancellation payment policy',
    },
    {
        'title': 'Contact',
        'url': '/contact/',
        'summary': 'Contact channels and support form for sales or technical help.',
        'keywords': 'contact support email sales help',
    },
]


def all_pages(request):
    query = request.GET.get('q', '').strip().lower()
    pages = PUBLIC_PAGES

    if query:
        pages = [
            page for page in PUBLIC_PAGES
            if query in page['title'].lower()
            or query in page['summary'].lower()
            or query in page['keywords']
        ]

    return render(request, 'pages/index.html', {
        'pages': pages,
        'query': request.GET.get('q', '').strip(),
    })


def about(request):
    return render(request, 'pages/about.html', {'active_page': 'about'})


def privacy(request):
    return render(request, 'pages/privacy.html')


def terms(request):
    return render(request, 'pages/terms.html')


def refund(request):
    return render(request, 'pages/refund.html')


def contact(request):
    return render(request, 'pages/contact.html', {'active_page': 'contact'})


def features(request):
    return render(request, 'pages/features.html', {'active_page': 'features'})


def compare(request):
    return render(request, 'pages/compare.html', {'active_page': 'compare'})


def pricing(request):
    return render(request, 'pages/pricing.html', {'active_page': 'pricing'})


def integrations(request):
    return render(request, 'pages/integrations.html', {'active_page': 'integrations'})


def faq(request):
    return render(request, 'pages/faq.html', {'active_page': 'faq'})


# ────────────────────────────────────────────────
# Programmatic /alternatives/<slug>/ pages — SEO landing pages targeting
# high-intent searches like "Intercom alternative".
# ────────────────────────────────────────────────
ALTERNATIVES = {
    'intercom': {
        'name': 'Intercom',
        'tagline': 'A capable chat suite, but priced for enterprises and locks key features behind expensive add-ons.',
        'pros': ['Polished UI', 'Mature integrations marketplace', 'Strong product analytics'],
        'cons': ['Starts at $74/mo per seat — climbs fast', 'AI Resolution add-on costs $0.99/resolution', 'Visitor tracking limited compared to dedicated tools', 'Self-serve onboarding feels gated'],
        'why_us': ['Free forever plan with real features (not a 14-day trial)', 'Visitor tracking + heatmaps + session recordings included', 'AI bot pay-per-use via your own Claude key — no markup', 'Slack/Discord webhooks built-in, no Zapier needed'],
        'price_them': '$74/mo+ per seat',
        'price_us': '$0 free / $29 Pro / $99 Enterprise',
        'meta_desc': 'LiveTrack Pro vs Intercom: get real-time visitor tracking, AI live chat, and Slack alerts at 1/4th the price. Free forever plan, no credit card.',
    },
    'tawk': {
        'name': 'Tawk.to',
        'tagline': 'Free live chat, but the upsells (agent removal, translation, video) add up — and visitor tracking is bare-bones.',
        'pros': ['Genuine free tier', 'Simple to install', 'Mobile apps for agents'],
        'cons': ['$1/hr to remove "Powered by Tawk.to" branding', 'No real visitor analytics or session recording', 'No native Slack/Discord webhook', 'AI features cost extra per month'],
        'why_us': ['White-label widget on Pro plan (not per-hour)', 'Built-in heatmaps, session recording, frustration signals', 'AI bot with your own Claude key — no per-message charges', 'Native Slack & Discord webhooks free'],
        'price_them': 'Free + $1/hr removal + AI extras',
        'price_us': 'Free, white-label on $29 Pro',
        'meta_desc': 'LiveTrack Pro vs Tawk.to: white-label, AI-powered live chat with built-in visitor tracking and heatmaps. Better feature set, transparent pricing.',
    },
    'crisp': {
        'name': 'Crisp',
        'tagline': 'Solid product but the "Magic Browse" co-browsing and unlimited history live behind their priciest plan.',
        'pros': ['Co-browsing on Unlimited plan', 'MagicReply AI suggestions', 'Multi-channel inbox (email, Insta DM, etc.)'],
        'cons': ['Free plan limited to 2 seats', '$95/mo for unlimited history & co-browse', 'Visitor tracking analytics are very basic', 'Self-host is enterprise-only'],
        'why_us': ['Unlimited chat history on Pro for $29', 'WebRTC voice/video built-in (no add-on)', 'AI bot you control, with your own API key', 'Per-website segmentation included'],
        'price_them': '$25/mo Pro / $95/mo Unlimited',
        'price_us': '$29/mo unlimited everything',
        'meta_desc': 'LiveTrack Pro vs Crisp: unlimited chat history, AI bot, voice/video and visitor tracking — at one third of Crisp Unlimited\'s price.',
    },
    'drift': {
        'name': 'Drift',
        'tagline': 'A revenue-team tool. Powerful playbooks, but priced like a CRM and overkill for support-first teams.',
        'pros': ['ABM playbooks for sales teams', 'Salesforce integration depth', 'Drift AI conversation routing'],
        'cons': ['Pricing on request — typically $2.5k+/mo', 'Sales-heavy UX, not great for support agents', 'No real heatmaps or session recording', 'Steep learning curve'],
        'why_us': ['Transparent pricing — no "contact sales" wall', 'Equally good for support, sales and marketing', 'Built-in session recording + heatmaps + frustration signals', 'AI bot is BYOK, not a flat enterprise tax'],
        'price_them': '$2,500+/mo (custom)',
        'price_us': '$0 free / $29 Pro / $99 Enterprise',
        'meta_desc': 'LiveTrack Pro vs Drift: same revenue-team superpowers without the $2,500/month bill. Real-time tracking, AI chat, and session insights included.',
    },
}


def alternative_detail(request, slug):
    """Programmatic 'vs <competitor>' SEO landing page."""
    from django.http import Http404
    data = ALTERNATIVES.get(slug)
    if not data:
        raise Http404
    return render(request, 'pages/alternative_detail.html', {
        'slug': slug,
        'data': data,
        'all_alternatives': ALTERNATIVES,
    })


def alternatives_index(request):
    return render(request, 'pages/alternatives_index.html', {
        'alternatives': ALTERNATIVES,
    })


# ────────────────────────────────────────────────
# Changelog
# ────────────────────────────────────────────────
def changelog(request):
    from tracker.chat.models import ChangelogEntry
    entries = ChangelogEntry.objects.filter(is_published=True).order_by('-published_at')
    return render(request, 'pages/changelog.html', {'entries': entries})

