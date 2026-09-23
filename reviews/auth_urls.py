from django.urls import path

from .auth_views import (
    CSRFTokenView,
    RegisterView,
    LoginView,
    LogoutView,
    CurrentUserView,
    EmailVerifyRequestView,
    EmailVerifyConfirmView,
)

from .github_oauth_views import (
    github_connect,
    github_callback,
)


urlpatterns = [
    path("csrf/", CSRFTokenView.as_view(), name="csrf"),
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", CurrentUserView.as_view(), name="current-user"),

    path(
        "email/verify/request/",
        EmailVerifyRequestView.as_view(),
        name="email-verify-request",
    ),

    path(
        "email/verify/confirm/",
        EmailVerifyConfirmView.as_view(),
        name="email-verify-confirm",
    ),

    path(
        "github/connect/",
        github_connect,
        name="github-connect",
    ),

    path(
        "github/callback/",
        github_callback,
        name="github-callback",
    ),
]