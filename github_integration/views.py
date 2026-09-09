from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from reviews.serializers import RepositorySerializer, SyncRepositorySerializer
from .github_service import sync_repository_to_db
from .tasks import process_github_event 

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


 # Import your Celery task

@csrf_exempt
@require_POST
def github_webhook_view(request):
    """
    POST /api/github/webhook/
    Receives, verifies, and hands off real-time event payloads from GitHub to Celery.
    """
    # 1. Verify GitHub Webhook Secret Signature using raw request body
    header_signature = request.META.get('HTTP_X_HUB_SIGNATURE_256', '')
    if not verify_github_signature(request.body, header_signature):
        return HttpResponseForbidden("Invalid signature")

    # 2. Parse the incoming JSON event payload from GitHub
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # 3. Identify what type of event GitHub sent
    event_type = request.META.get('HTTP_X_GITHUB_EVENT', '')

    # 4. Hand off the heavy lifting to Celery/Redis asynchronously
    process_github_event.delay(event_type, payload)

    # 5. Return an immediate 200 OK so GitHub knows the webhook was received
    return JsonResponse({"status": "queued"}, status=200)