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
]
