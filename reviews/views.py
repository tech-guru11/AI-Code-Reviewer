from rest_framework import generics
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from github_integration.tasks import process_manual_review
from github_integration.github_service import GitHubAppService
from .models import Repository, Review, PullRequest, Finding
from .serializers import (
    PullRequestSerializer,
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

class PullRequestListView(generics.ListAPIView):
    queryset = PullRequest.objects.all()
    serializer_class = PullRequestSerializer   
class PullRequestReviewView(generics.GenericAPIView):
    queryset = PullRequest.objects.all()
    serializer_class = PullRequestSerializer

    def post(self, request, pk):
        pull_request = self.get_object()

        try:
            repository = pull_request.repository
            service = GitHubAppService()
            github_client = service.get_client()

            repo_full_name = (
                repository.github_url
                .replace("https://github.com/", "")
                .rstrip("/")
            )

            github_repo = github_client.get_repo(repo_full_name)

            github_pr = github_repo.get_pull(
                pull_request.github_pr_number
            )

            commit_sha = github_pr.head.sha

        except Exception as e:
            return Response(
                {
                    "message": "Failed to retrieve the current GitHub commit.",
                    "error": str(e),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Check whether this exact commit already has a review
        existing_review = (
            Review.objects
            .filter(
                pull_request=pull_request,
                commit_sha=commit_sha,
            )
            .order_by("-created_at")
            .first()
        )

        if existing_review:
            return Response(
                {
                    "message": (
                        "This commit has already been reviewed."
                        if existing_review.status == "completed"
                        else "A review for this commit already exists."
                    ),
                    "pull_request_id": pull_request.id,
                    "review_id": existing_review.id,
                    "commit_sha": commit_sha,
                    "status": existing_review.status,
                },
                status=status.HTTP_200_OK,
            )

        # Create the Review BEFORE starting Celery.
        # This gives the frontend a permanent review_id to monitor.
        review = Review.objects.create(
            pull_request=pull_request,
            commit_sha=commit_sha,
            status="processing",
        )

        # Start the background AI review.
        task = process_manual_review.delay(
            pull_request.id,
            commit_sha,
            review.id,
        )

        return Response(
            {
                "message": "AI review request received",
                "pull_request_id": pull_request.id,
                "review_id": review.id,
                "task_id": task.id,
                "commit_sha": commit_sha,
                "status": "processing",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request, pk):
        pull_request = self.get_object()

        review = (
            Review.objects
            .filter(pull_request=pull_request)
            .order_by("-created_at")
            .first()
        )

        if not review:
            return Response(
                {
                    "pull_request_id": pull_request.id,
                    "status": "not_reviewed",
                    "message": "No AI review has been completed yet.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        findings = Finding.objects.filter(
            review=review
        )

        return Response(
            {
                "pull_request_id": pull_request.id,
                "review_id": review.id,
                "status": review.status,
                "commit_sha": review.commit_sha,
                "summary": review.summary,
                "score": review.score,
                "started_at": review.started_at,
                "completed_at": review.completed_at,
                "findings": [
                    {
                        "id": finding.id,
                        "file_path": finding.file_path,
                        "line_number": finding.line_number,
                        "severity": finding.severity,
                        "category": finding.category,
                        "title": finding.title,
                        "description": finding.description,
                        "suggestion": finding.suggestion,
                        "code_snippet": finding.code_snippet,
                    }
                    for finding in findings
                ],
            },
            status=status.HTTP_200_OK,
        )