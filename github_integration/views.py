from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from reviews.models import Repository
from reviews.serializers import RepositorySerializer, SyncRepositorySerializer
from .github_service import sync_repository_to_db


class SyncRepositoryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SyncRepositorySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        repo_name = serializer.validated_data["repo_name"]

        try:
            repo = sync_repository_to_db(repo_name=repo_name, user=request.user)
            response_serializer = RepositorySerializer(repo)
            return Response(response_serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {"error": f"Failed to sync repository: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )