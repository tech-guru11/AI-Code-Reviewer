from rest_framework import generics
from .models import Repository, Review
from .serializers import (
    RepositorySerializer, 
    ReviewListSerializer, 
    ReviewDetailSerializer
)


# GET /api/repositories/  &  POST /api/repositories/
class RepositoryListCreateView(generics.ListCreateAPIView):
    queryset = Repository.objects.all().order_by('-created_at')
    serializer_class = RepositorySerializer


# GET /api/reviews/
class ReviewListView(generics.ListAPIView):
    queryset = Review.objects.all().order_by('-created_at')
    serializer_class = ReviewListSerializer


# GET /api/reviews/{id}/
class ReviewDetailView(generics.RetrieveAPIView):
    queryset = Review.objects.all()
    serializer_class = ReviewDetailSerializer