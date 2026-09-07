from django.urls import path
from .views import PullRequestListView, PullRequestReviewView, RepositoryListCreateView, ReviewListView, ReviewDetailView

urlpatterns = [
    path('repositories/', RepositoryListCreateView.as_view(), name='repository-list-create'),
    path('reviews/', ReviewListView.as_view(), name='review-list'),
    path('reviews/<int:pk>/', ReviewDetailView.as_view(), name='review-detail'),
    path(
    "pull-requests/",
    PullRequestListView.as_view(),
    name="pull-request-list"
),
   path(
    "pull-requests/<int:pk>/review/",
    PullRequestReviewView.as_view(),
    name="pull-request-review"
),
]