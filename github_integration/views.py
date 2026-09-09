from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from reviews.serializers import RepositorySerializer, SyncRepositorySerializer
from .github_service import sync_repository_to_db
from .tasks import process_github_event 
from django.db import IntegrityError
from .models import GitHubWebhookDelivery
import json
import hmac
import hashlib
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
import logging
logger = logging.getLogger(__name__)

class SyncRepositoryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SyncRepositorySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        repo_full_name = serializer.validated_data["repo_full_name"]

        try:
            repo = sync_repository_to_db(repo_full_name=repo_full_name, user=request.user)
            response_serializer = RepositorySerializer(repo)
            return Response(response_serializer.data, status=status.HTTP_200_OK)
        except Exception:
            logger.exception(
                "Failed to sync repository '%s' for user '%s'.",
                repo_full_name,
                request.user.username,
            )

            return Response(
                {"error": "Failed to sync repository. Please try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )


def verify_github_signature(request_body, header_signature):
    """
    Verifies that the incoming webhook request genuinely came from GitHub.
    """
    if not header_signature:
        return False
    
    # Retrieve your secret token from Django settings (settings.GITHUB_WEBHOOK_SECRET)
    secret = getattr(settings, 'GITHUB_WEBHOOK_SECRET', '').encode('utf-8')
    if not secret:
        return False

    # GitHub sends the signature as 'sha256=<hex_digest>'
    try:
        sha_name, signature = header_signature.split("=", 1)
    except ValueError:
        return False

    if sha_name != "sha256" or not signature:
        return False

    # Compute our own HMAC using the raw request body and our secret
    mac = hmac.new(secret, msg=request_body, digestmod=hashlib.sha256)
    computed_signature = mac.hexdigest()

    # Use a timing-safe comparison to prevent timing attacks
    return hmac.compare_digest(computed_signature, signature)


@csrf_exempt
@require_POST
def github_webhook_view(request):
    """
    POST /api/github/webhook/
    Receives, verifies, validates, and queues GitHub webhook events.
    """

    # Verify the GitHub webhook signature using the raw request body.
    header_signature = request.META.get(
        "HTTP_X_HUB_SIGNATURE_256",
        "",
    )

    if not verify_github_signature(
        request.body,
        header_signature,
    ):
        return HttpResponseForbidden("Invalid signature")

    # GitHub provides a unique delivery ID for every webhook delivery.
    delivery_id = request.META.get(
        "HTTP_X_GITHUB_DELIVERY",
        "",
    ).strip()

    if not delivery_id:
        return JsonResponse(
            {
                "error": "Missing GitHub delivery ID."
            },
            status=400,
        )

    # Identify the GitHub event type.
    event_type = request.META.get(
        "HTTP_X_GITHUB_EVENT",
        "",
    ).strip()

    # This application currently processes only pull_request events.
    if event_type != "pull_request":
        return JsonResponse(
            {
                "error": "Unsupported GitHub event."
            },
            status=400,
        )

    # Parse the incoming JSON payload.
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse(
            {
                "error": "Invalid JSON."
            },
            status=400,
        )

    # The GitHub webhook payload must be a JSON object.
    if not isinstance(payload, dict):
        return JsonResponse(
            {
                "error": "Invalid webhook payload."
            },
            status=400,
        )

    # Extract the fields required by the Celery task.
    action = payload.get("action")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")

    # Validate the top-level payload structure.
    if not isinstance(action, str):
        return JsonResponse(
            {
                "error": "Missing or invalid webhook action."
            },
            status=400,
        )

    if not isinstance(repository, dict):
        return JsonResponse(
            {
                "error": "Missing or invalid repository data."
            },
            status=400,
        )

    if not isinstance(pull_request, dict):
        return JsonResponse(
            {
                "error": "Missing or invalid pull request data."
            },
            status=400,
        )

    # Validate repository identity.
    repo_full_name = repository.get("full_name")

    if not isinstance(repo_full_name, str) or not repo_full_name.strip():
        return JsonResponse(
            {
                "error": "Missing or invalid repository name."
            },
            status=400,
        )

    # Validate pull request number.
    pr_number = pull_request.get("number")

    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return JsonResponse(
            {
                "error": "Missing or invalid pull request number."
            },
            status=400,
        )

    if pr_number <= 0:
        return JsonResponse(
            {
                "error": "Invalid pull request number."
            },
            status=400,
        )

    # Validate the pull request head commit.
    head = pull_request.get("head")

    if not isinstance(head, dict):
        return JsonResponse(
            {
                "error": "Missing or invalid pull request head."
            },
            status=400,
        )

    commit_sha = head.get("sha")

    if not isinstance(commit_sha, str) or not commit_sha.strip():
        return JsonResponse(
            {
                "error": "Missing or invalid commit SHA."
            },
            status=400,
        )

    # Only process actions that can trigger an AI review.
    allowed_actions = {
        "opened",
        "synchronize",
        "reopened",
    }

    if action not in allowed_actions:
        return JsonResponse(
            {
                "status": "ignored",
                "event": event_type,
                "action": action,
            },
            status=200,
        )

    # Record the GitHub delivery ID.
    #
    # The unique database constraint prevents the same delivery
    # from being processed more than once.
    try:
        delivery, created = GitHubWebhookDelivery.objects.get_or_create(
            delivery_id=delivery_id,
            defaults={
                "event_type": event_type,
            },
        )
    except IntegrityError:
        return JsonResponse(
            {
                "status": "duplicate",
            },
            status=200,
        )

    if not created:
        return JsonResponse(
            {
                "status": "duplicate",
            },
            status=200,
        )

    # Queue the validated event for asynchronous processing.
    try:
        process_github_event.delay(
            event_type,
            payload,
        )
    except Exception:
        logger.exception(
            "Failed to queue GitHub webhook delivery."
        )

        # Remove the delivery record so GitHub can safely retry.
        delivery.delete()

        return JsonResponse(
            {
                "error": "Failed to queue webhook."
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "queued",
        },
        status=200,
    )