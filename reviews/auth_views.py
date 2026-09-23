import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.middleware.csrf import get_token
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone
from github_integration.models import GitHubConnection
from reviews.models import EmailVerificationCode, UserProfile
from rest_framework import status
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class LoginThrottle(AnonRateThrottle):
    scope = "login"

class RegisterThrottle(AnonRateThrottle):
    scope = "register"

class EmailVerifyRequestThrottle(UserRateThrottle):
    scope = "email_verify"

class EmailVerifyConfirmThrottle(UserRateThrottle):
    scope = "email_verify_confirm"

VERIFICATION_CODE_TTL_MINUTES = 10

def get_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def mask_email(email):
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        visible = local[0]
    else:
        visible = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{visible}@{domain}"


def user_data(user):
    try:
        github_connection = GitHubConnection.objects.get(
            user=user
        )

        github_connected = True
        github_username = github_connection.github_username

    except GitHubConnection.DoesNotExist:
        github_connected = False
        github_username = None

    profile = get_profile(user)

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "email_verified": profile.email_verified,
        "github_connected": github_connected,
        "github_username": github_username,
    }


class CSRFTokenView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            "csrfToken": get_token(request)
        })


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [RegisterThrottle]

    def post(self, request):
        username = request.data.get("username", "").strip()
        email = request.data.get("email", "").strip()
        password = request.data.get("password", "")
        password_confirm = request.data.get("password_confirm", "")

        if not username:
            return Response(
                {"error": "Username is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not password:
            return Response(
                {"error": "Password is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if password != password_confirm:
            return Response(
                {"error": "Passwords do not match."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if User.objects.filter(username=username).exists():
            return Response(
                {"error": "Username already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if email and User.objects.filter(email=email).exists():
            return Response(
                {"error": "Email is already registered."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(password, user=None)
        except ValidationError as error:
            return Response(
                {"error": error.messages},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
        )

        login(request, user)

        return Response(
            {
                "message": "Registration successful.",
                "user": user_data(user),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        username = request.data.get("username", "").strip()
        password = request.data.get("password", "")

        if not username or not password:
            return Response(
                {"error": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(
            request,
            username=username,
            password=password,
        )

        if user is None:
            return Response(
                {"error": "Invalid username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        login(request, user)

        return Response(
            {
                "message": "Login successful.",
                "user": user_data(user),
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)

        return Response(
            {"message": "Logout successful."},
            status=status.HTTP_200_OK,
        )


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {"user": user_data(request.user)},
            status=status.HTTP_200_OK,
        )


class EmailVerifyRequestView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmailVerifyRequestThrottle]

    def post(self, request):
        user = request.user

        if not user.email:
            return Response(
                {
                    "error": (
                        "No email address is set on your account. "
                        "Add an email before verifying."
                    ),
                    "error_code": "email_missing",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = "{:06d}".format(secrets.randbelow(1_000_000))
        code_hash = hashlib.sha256(code.encode()).hexdigest()

        EmailVerificationCode.objects.filter(user=user).delete()

        EmailVerificationCode.objects.create(
            user=user,
            code_hash=code_hash,
            expires_at=timezone.now() + timedelta(
                minutes=VERIFICATION_CODE_TTL_MINUTES
            ),
        )

        subject = "Your AI Code Reviewer verification code"
        message = (
            f"Hi {user.username},\n\n"
            f"Your verification code is: {code}\n\n"
            f"This code expires in {VERIFICATION_CODE_TTL_MINUTES} minutes.\n"
            "If you did not request this code, you can safely ignore it."
        )

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception:
            return Response(
                {
                    "error": (
                        "Could not send the verification email. "
                        "Please try again later."
                    ),
                    "error_code": "email_send_failed",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "message": (
                    f"A verification code was sent to {mask_email(user.email)}."
                ),
                "email": mask_email(user.email),
                "expires_in_minutes": VERIFICATION_CODE_TTL_MINUTES,
            },
            status=status.HTTP_200_OK,
        )


class EmailVerifyConfirmView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmailVerifyConfirmThrottle]

    def post(self, request):
        user = request.user
        code = request.data.get("code", "").strip()

        if not user.email:
            return Response(
                {
                    "error": "No email address is set on your account.",
                    "error_code": "email_missing",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not code:
            return Response(
                {
                    "error": "Verification code is required.",
                    "error_code": "code_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        latest = (
            EmailVerificationCode.objects
            .filter(user=user)
            .order_by("-created_at")
            .first()
        )

        if not latest or not latest.is_valid():
            return Response(
                {
                    "error": (
                        "No active verification code. "
                        "Request a new one."
                    ),
                    "error_code": "code_expired",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        submitted_hash = hashlib.sha256(code.encode()).hexdigest()

        if not hmac.compare_digest(
            latest.code_hash,
            submitted_hash,
        ):
            return Response(
                {
                    "error": "Incorrect verification code.",
                    "error_code": "invalid_code",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        latest.used = True
        latest.save()

        EmailVerificationCode.objects.filter(
            user=user
        ).exclude(pk=latest.pk).delete()

        profile = get_profile(user)
        profile.email_verified = True
        profile.save()

        return Response(
            {
                "message": "Email verified successfully.",
                "user": user_data(user),
            },
            status=status.HTTP_200_OK,
        )
