from django.urls import path
from .views import SyncRepositoryView, github_webhook_view 

urlpatterns = [
    path("sync-repository/", SyncRepositoryView.as_view(), name="sync-repository"),
    path('webhook/', github_webhook_view, name='github_webhook_view'),
]