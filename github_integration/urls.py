from django.urls import path
from .views import SyncRepositoryView

urlpatterns = [
    path("sync-repository/", SyncRepositoryView.as_view(), name="sync-repository"),
]