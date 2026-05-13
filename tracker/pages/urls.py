from django.urls import path
from . import views

app_name = 'pages'

urlpatterns = [
    path('pages/', views.all_pages, name='all_pages'),
    path('about/', views.about, name='about'),
    path('privacy/', views.privacy, name='privacy'),
    path('terms/', views.terms, name='terms'),
    path('refund/', views.refund, name='refund'),
    path('contact/', views.contact, name='contact'),
    path('features/', views.features, name='features'),
    path('compare/', views.compare, name='compare'),
    path('pricing/', views.pricing, name='pricing'),
    path('integrations/', views.integrations, name='integrations'),
    path('faq/', views.faq, name='faq'),
    path('alternatives/', views.alternatives_index, name='alternatives_index'),
    path('alternatives/<slug:slug>/', views.alternative_detail, name='alternative_detail'),
    path('changelog/', views.changelog, name='changelog'),
]
