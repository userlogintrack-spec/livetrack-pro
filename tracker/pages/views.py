from django.shortcuts import render


PUBLIC_PAGES = [
    {
        'title': 'About Us',
        'url': '/about/',
        'summary': 'Our story, mission, numbers, and team behind LiveTrack Pro.',
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
    return render(request, 'pages/about.html')


def privacy(request):
    return render(request, 'pages/privacy.html')


def terms(request):
    return render(request, 'pages/terms.html')


def refund(request):
    return render(request, 'pages/refund.html')


def contact(request):
    return render(request, 'pages/contact.html')
