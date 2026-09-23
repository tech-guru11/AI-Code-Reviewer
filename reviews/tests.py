from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from unittest import mock
from rest_framework.test import APIClient

from reviews.models import EmailVerificationCode, UserProfile


def _code_for(user):
    return EmailVerificationCode.objects.filter(
        user=user
    ).order_by("-created_at").first()


class EmailVerificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password="strong-password-123",
        )
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_request_code_sends_email_and_stores_hashed_code(self):
        response = self.client.post(
            reverse("email-verify-request")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(
            "verification code",
            mail.outbox[0].subject,
        )
        record = _code_for(self.user)
        self.assertIsNotNone(record)

        plain_code = _plain_code(mail.outbox[0].body)
        self.assertIsNotNone(plain_code)
        self.assertNotEqual(record.code_hash, plain_code)

    def test_confirm_with_wrong_code_rejected(self):
        self.client.post(reverse("email-verify-request"))
        response = self.client.post(
            reverse("email-verify-confirm"),
            {"code": "000000"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["error_code"],
            "invalid_code",
        )
        profile = UserProfile.objects.filter(
            user=self.user
        ).first()
        self.assertTrue(
            profile is None or not profile.email_verified
        )

    def test_full_flow_verifies_email(self):
        response = self.client.post(
            reverse("email-verify-request")
        )
        self.assertEqual(response.status_code, 200)

        code = _plain_code(mail.outbox[0].body)
        self.assertIsNotNone(code)

        confirm = self.client.post(
            reverse("email-verify-confirm"),
            {"code": code},
        )
        self.assertEqual(confirm.status_code, 200)
        self.assertTrue(
            confirm.data["user"]["email_verified"]
        )
        self.assertTrue(
            UserProfile.objects.get(
                user=self.user
            ).email_verified
        )

    def test_confirm_without_active_code_rejected(self):
        response = self.client.post(
            reverse("email-verify-confirm"),
            {"code": "123456"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["error_code"],
            "code_expired",
        )


class GitHubConnectGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bob",
            email="bob@example.com",
            password="strong-password-123",
        )
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_connect_blocked_when_email_not_verified(self):
        response = self.client.get(
            reverse("github-connect")
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "email_not_verified")

    def test_connect_blocked_when_no_email(self):
        self.user.email = ""
        self.user.save()

        response = self.client.get(
            reverse("github-connect")
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "email_missing")

    def test_connect_redirects_when_email_verified(self):
        UserProfile.objects.get_or_create(
            user=self.user,
            defaults={"email_verified": True},
        )
        profile = UserProfile.objects.get(user=self.user)
        profile.email_verified = True
        profile.save()

        response = self.client.get(
            reverse("github-connect")
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "github.com/login/oauth/authorize",
            response.url,
        )

    def test_connect_requires_authentication(self):
        response = APIClient().get(reverse("github-connect"))
        self.assertEqual(response.status_code, 401)


class GitHubCallbackEmailCheckTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="carol",
            email="carol@example.com",
            password="strong-password-123",
        )
        profile = UserProfile.objects.get_or_create(
            user=self.user
        )[0]
        profile.email_verified = True
        profile.save()

        self.client = APIClient(enforce_csrf_checks=False)
        session = self.client.session
        session["github_oauth_state"] = "test-state"
        session["github_oauth_user_id"] = self.user.id
        session.save()

    def _mock_api(self, email, verified=True):
        token_resp = mock.Mock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "test-token",
            "token_type": "bearer",
        }

        github_user_resp = mock.Mock()
        github_user_resp.status_code = 200
        github_user_resp.json.return_value = {
            "id": 12345,
            "login": "carol-gh",
        }

        emails_resp = mock.Mock()
        emails_resp.status_code = 200
        emails_resp.json.return_value = [
            {
                "email": email,
                "primary": True,
                "verified": verified,
            }
        ]

        return mock.patch(
            "reviews.github_oauth_views.requests.post",
            return_value=token_resp,
        ), mock.patch(
            "reviews.github_oauth_views.requests.get",
            side_effect=[github_user_resp, emails_resp],
        )

    def test_callback_rejects_email_mismatch(self):
        patch_post, patch_get = self._mock_api(
            "someone-else@example.com"
        )
        with patch_post, patch_get:
            response = self.client.get(
                reverse("github-callback"),
                {"code": "abc", "state": "test-state"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "does not match",
            response.json()["error"],
        )

    def test_callback_rejects_unverified_github_email(self):
        patch_post, patch_get = self._mock_api(
            "carol@example.com",
            verified=False,
        )
        with patch_post, patch_get:
            response = self.client.get(
                reverse("github-callback"),
                {"code": "abc", "state": "test-state"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "not verified",
            response.json()["error"],
        )


def _plain_code(body):
    import re
    match = re.search(r"\b(\d{6})\b", body)
    return match.group(1) if match else None