import logging

from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from github_integration.tasks import process_manual_review
from github_integration.github_service import get_oauth_github_client
from .models import Repository, Review, PullRequest, Finding
from .serializers import (
    PullRequestSerializer,
    RepositorySerializer, 
    ReviewListSerializer, 
    ReviewDetailSerializer
)


logger = logging.getLogger(__name__)


# GET /api/repositories/  &  POST /api/repositories/
class RepositoryListCreateView(generics.ListCreateAPIView):
    serializer_class = RepositorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Repository.objects.filter(
            owner=self.request.user
        ).order_by("-created_at")


# GET /api/reviews/
class ReviewListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReviewListSerializer

    def get_queryset(self):
        return (
            Review.objects
            .filter(pull_request__repository__owner=self.request.user)
            .select_related("pull_request", "pull_request__repository")
            .order_by("-created_at")
        )


# GET /api/reviews/{id}/
class ReviewDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReviewDetailSerializer

    def get_queryset(self):
        return (
            Review.objects
            .filter(pull_request__repository__owner=self.request.user)
            .select_related("pull_request", "pull_request__repository")
        )



class PullRequestListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PullRequestSerializer

    def get_queryset(self):
        return PullRequest.objects.filter(
            repository__owner=self.request.user
        ).select_related(
            "repository",
            "author",
        ).order_by("-created_at")

      
class PullRequestReviewView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PullRequestSerializer

    def get_queryset(self):
        # Scoping the queryset makes get_object() reject pull requests
        # owned by other users for every HTTP verb on this view.
        return PullRequest.objects.filter(
            repository__owner=self.request.user
        ).select_related("repository", "author")

    def post(self, request, pk):
        pull_request = self.get_object()

        try:
            repository = pull_request.repository

            github_client = get_oauth_github_client(request.user)

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

        if existing_review and existing_review.status in (
            "completed",
            "processing",
        ):
            return Response(
                {
                    "message": (
                        "This commit has already been reviewed."
                        if existing_review.status == "completed"
                        else "A review for this commit is already processing."
                    ),
                    "pull_request_id": pull_request.id,
                    "review_id": existing_review.id,
                    "commit_sha": commit_sha,
                    "status": existing_review.status,
                },
                status=status.HTTP_200_OK,
            )

      # Reuse a failed review for this commit when retrying.
        if existing_review and existing_review.status == "failed":
            review = existing_review
            review.status = "processing"
            review.started_at = timezone.now()
            review.completed_at = None
            review.score = None
            review.summary = ""
            review.save(
                update_fields=[
                    "status",
                    "started_at",
                    "completed_at",
                    "score",
                    "summary",
                ]
            )

            # A previous attempt may have saved findings before failing,
            # so clear them to avoid duplicating them on this retry.
            review.findings.all().delete()
        else:
            # Create a new Review for a commit that has never been reviewed.
            review = Review.objects.create(
                pull_request=pull_request,
                commit_sha=commit_sha,
                status="processing",
                started_at=timezone.now(),
            )
        # Start the background AI review.
        try:
            task = process_manual_review.delay(
                pull_request.id,
                commit_sha,
                review.id,
            )
        except Exception:
            # The task never reached the broker, so the review would stay
            # "processing" forever. Mark it failed so it can be retried.
            logger.exception(
                "Failed to enqueue AI review for PullRequest ID %s.",
                pull_request.id,
            )

            review.status = "failed"
            review.completed_at = timezone.now()
            review.save(
                update_fields=["status", "completed_at"]
            )

            return Response(
                {
                    "message": (
                        "Could not start the AI review. Please try again."
                    ),
                    "pull_request_id": pull_request.id,
                    "review_id": review.id,
                    "commit_sha": commit_sha,
                    "status": "failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
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