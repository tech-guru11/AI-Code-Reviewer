import logging
import secrets
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect
from github_integration.models import GitHubConnection
from github_integration.crypto import encrypt_github_token
from reviews.auth_views import get_profile, mask_email


logger = logging.getLogger(__name__)


def github_connect(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Authentication required."},
            status=401,
        )

    if not request.user.email:
        return JsonResponse(
            {
                "error": (
                    "You must have an email address on your account "
                    "before connecting GitHub."
                ),
                "error_code": "email_missing",
            },
            status=403,
        )

    if not get_profile(request.user).email_verified:
        return JsonResponse(
            {
                "error": (
                    "Your email address must be verified before you can "
                    "connect a GitHub account. "
                    "Request a code and confirm it first."
                ),
                "error_code": "email_not_verified",
                "email": mask_email(request.user.email),
            },
            status=403,
        )

    state = secrets.token_urlsafe(32)

    request.session["github_oauth_state"] = state
    request.session["github_oauth_user_id"] = request.user.id

    client_id = settings.GITHUB_CLIENT_ID

    redirect_uri = settings.GITHUB_REDIRECT_URI

    scope = "read:user user:email repo"

    github_url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&state={state}"
    )

    return redirect(github_url)



def github_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state")
    github_error = request.GET.get("error")
    github_error_description = request.GET.get(
        "error_description"
    )

    saved_state = request.session.get("github_oauth_state")
    saved_user_id = request.session.get("github_oauth_user_id")

    if github_error:
        return JsonResponse(
            {
                "error": "GitHub OAuth authorization failed.",
                "github_error": github_error,
                "description": github_error_description,
            },
            status=400,
        )

    if not code:
        return JsonResponse(
            {
                "error": "GitHub authorization code was not provided.",
                "callback_parameters": list(request.GET.keys()),
            },
            status=400,
        )

    if (
        not state
        or not saved_state
        or not secrets.compare_digest(state, saved_state)
    ):
        return JsonResponse(
            {"error": "Invalid OAuth state."},
            status=400,
        )

    saved_user_id = request.session.pop(
        "github_oauth_user_id",
        None,
    )
    request.session.pop(
        "github_oauth_state",
        None,
    )

    if not saved_user_id:
        return JsonResponse(
            {
                "error": "GitHub OAuth session has expired. "
                "Please start the connection again."
            },
            status=401,
        )

    try:
        user = User.objects.get(
            id=saved_user_id,
            is_active=True,
        )
    except User.DoesNotExist:
        return JsonResponse(
            {"error": "Django user was not found."},
            status=401,
        )

    try:
        token_response = requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.GITHUB_REDIRECT_URI,
            },
            headers={
                "Accept": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException:
        logger.exception(
            "Failed to reach GitHub to exchange the OAuth code."
        )

        return JsonResponse(
            {
                "error": "Could not reach GitHub. Please try again."
            },
            status=502,
        )

    if token_response.status_code != 200:
        return JsonResponse(
            {
                "error": (
                    "Failed to exchange GitHub authorization code."
                )
            },
            status=400,
        )

    token_data = token_response.json()

    access_token = token_data.get("access_token")

    if not access_token:
        return JsonResponse(
            {
                "error": "GitHub did not return an access token.",
                "details": token_data.get(
                    "error_description"
                ),
            },
            status=400,
        )

    try:
        github_user_response = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )

        email_response = requests.get(
            "https://api.github.com/user/emails",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
    except requests.RequestException:
        logger.exception(
            "Failed to reach the GitHub API during OAuth callback."
        )

        return JsonResponse(
            {"error": "Could not reach GitHub. Please try again."},
            status=502,
        )

    if github_user_response.status_code != 200:
        return JsonResponse(
            {"error": "Failed to retrieve GitHub user."},
            status=400,
        )

    github_user = github_user_response.json()

    github_user_id = github_user.get("id")
    github_username = github_user.get("login")

    if not github_user_id or not github_username:
        return JsonResponse(
            {"error": "Invalid GitHub user information."},
            status=400,
        )

    account_email = (user.email or "").strip().lower()

    github_email = ""
    github_email_verified = False

    if email_response.status_code == 200:
        emails = email_response.json()

        if emails:
            primary_email = next(
                (item for item in emails if item.get("primary")),
                emails[0],
            )

            github_email = (
                primary_email.get("email") or ""
            ).strip().lower()

            github_email_verified = (
                primary_email.get("verified") is True
            )
        else:
            logger.warning(
                "GitHub returned an empty email list for the "
                "connected account."
            )
    else:
        # A GitHub App can only read /user/emails when its "Email
        # addresses: read-only" account permission was granted. When it
        # was not, GitHub answers /user/emails with 403 while /user
        # still succeeds, so fall back to the address on the user
        # payload rather than refusing a working connection.
        logger.warning(
            "GitHub /user/emails failed (status=%s, body=%s). "
            "Falling back to the public email on /user.",
            email_response.status_code,
            email_response.text[:200],
        )

        github_email = (
            github_user.get("email") or ""
        ).strip().lower()

        # GitHub only publishes the address on /user once it has been
        # verified, so treat a non-empty value as verified.
        github_email_verified = bool(github_email)

    if not github_email:
        return JsonResponse(
            {
                "error": (
                    "No email found on the GitHub account. "
                    "Add a verified email to your GitHub profile and "
                    'make sure the GitHub App has the "Email '
                    'addresses: read-only" account permission.'
                ),
            },
            status=400,
        )

    if not github_email_verified:
        return JsonResponse(
            {
                "error": (
                    "The GitHub account's primary email is not verified "
                    "on GitHub."
                ),
            },
            status=400,
        )

    if not account_email or github_email != account_email:
        return JsonResponse(
            {
                "error": (
                    "The GitHub account's verified primary email "
                    f"({mask_email(github_email)}) does not match the "
                    f"email on your account "
                    f"({mask_email(account_email)}). Connect the GitHub "
                    "account that uses your account email."
                ),
            },
            status=400,
        )

    existing_connection = GitHubConnection.objects.filter(
        github_user_id=github_user_id
    ).first()

    if existing_connection and existing_connection.user_id != user.id:
        frontend_url = settings.FRONTEND_URL
        return redirect(f"{frontend_url}?github=already_connected")

    GitHubConnection.objects.update_or_create(
        user=user,
        defaults={
            "github_user_id": github_user_id,
            "github_username": github_username,
            "access_token": encrypt_github_token(access_token),
        },
    )

    frontend_url = settings.FRONTEND_URL

    return redirect(
        f"{frontend_url}?github=connected"
    )
