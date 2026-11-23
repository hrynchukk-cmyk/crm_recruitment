from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path


def healthz(request):
    """Для перевірки здоров'я на хостингу: 200, якщо застосунок живий і база відповідає."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


admin.site.site_header = "CRM рекрутингу — адміністрування"
admin.site.site_title = "CRM рекрутингу"

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("recruiting.urls")),
]
