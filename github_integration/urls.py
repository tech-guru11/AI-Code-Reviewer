from django.urls import path
from .views import (
    SyncRepositoryView,
    GitHubRepositoriesView,
    github_webhook_view,
)

urlpatterns = [
    path(
        "repositories/",
        GitHubRepositoriesView.as_view(),
        name="github-repositories",
    ),
    path(
        "sync-repository/",
        SyncRepositoryView.as_view(),
        name="sync-repository",
    ),
    path(
        "webhook/",
        github_webhook_view,
        name="github_webhook_view",
    ),
]