import secrets

import requests

from django.conf import settings
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect

from github_integration.models import GitHubConnection
from github_integration.crypto import encrypt_github_token


def github_connect(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Authentication required."},
            status=401,
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

    github_user_response = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
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

    GitHubConnection.objects.update_or_create(
        user=user,
        defaults={
            "github_user_id": github_user_id,
            "github_username": github_username,
            "access_token": encrypt_github_token(access_token),
        },
    )

    request.session.pop("github_oauth_state", None)
    request.session.pop("github_oauth_user_id", None)

    frontend_url = settings.FRONTEND_URL

    return redirect(
        f"{frontend_url}?github=connected"
    )
