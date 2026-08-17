from django.urls import path
from .views import RepositoryListCreateView, ReviewListView, ReviewDetailView

urlpatterns = [
    path('repositories/', RepositoryListCreateView.as_view(), name='repository-list-create'),
    path('reviews/', ReviewListView.as_view(), name='review-list'),
    path('reviews/<int:pk>/', ReviewDetailView.as_view(), name='review-detail'),
]