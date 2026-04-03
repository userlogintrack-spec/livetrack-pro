from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('tracker.core.urls')),
    path('dashboard/', include('tracker.dashboard.urls')),
    path('api/', include('tracker.core.api_urls')),
    path('', include('tracker.pages.urls')),
    path('', include('tracker.core.landing_urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
