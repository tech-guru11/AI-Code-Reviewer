from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest import mock
from rest_framework.test import APIClient

from github_integration.tasks import normalize_score
from reviews.models import (
    EmailVerificationCode,
    Finding,
    PullRequest,
    Repository,
    Review,
    UserProfile,
)


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

    def test_dev_code_returned_only_under_console_backend(self):
        """
        The code is echoed back so local dev can display it, but only
        when nothing is actually being delivered. A real mail backend or
        DEBUG=False must never expose it.
        """
        with override_settings(
            DEBUG=True,
            EMAIL_BACKEND=(
                "django.core.mail.backends.console.EmailBackend"
            ),
        ):
            with mock.patch("reviews.auth_views.send_mail"):
                response = self.client.post(
                    reverse("email-verify-request")
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn("dev_code", response.data)

        with override_settings(
            DEBUG=True,
            EMAIL_BACKEND=(
                "django.core.mail.backends.smtp.EmailBackend"
            ),
        ):
            with mock.patch("reviews.auth_views.send_mail"):
                response = self.client.post(
                    reverse("email-verify-request")
                )

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("dev_code", response.data)

        with override_settings(
            DEBUG=False,
            EMAIL_BACKEND=(
                "django.core.mail.backends.console.EmailBackend"
            ),
        ):
            with mock.patch("reviews.auth_views.send_mail"):
                response = self.client.post(
                    reverse("email-verify-request")
                )

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("dev_code", response.data)

    def test_dev_code_confirms_successfully(self):
        with override_settings(
            DEBUG=True,
            EMAIL_BACKEND=(
                "django.core.mail.backends.console.EmailBackend"
            ),
        ):
            with mock.patch("reviews.auth_views.send_mail"):
                response = self.client.post(
                    reverse("email-verify-request")
                )

            code = response.data["dev_code"]

        confirm = self.client.post(
            reverse("email-verify-confirm"),
            {"code": code},
        )

        self.assertEqual(confirm.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.profile.email_verified)

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


class ReviewIsolationTests(TestCase):
    """
    Reviews belong to the owner of the repository, so a user must never
    be able to list or read another user's reviews.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="strong-password-123",
        )
        self.intruder = User.objects.create_user(
            username="intruder",
            email="intruder@example.com",
            password="strong-password-123",
        )

        self.owner_repo = Repository.objects.create(
            owner=self.owner,
            name="owner-repo",
            github_url="https://github.com/owner/owner-repo",
        )
        self.intruder_repo = Repository.objects.create(
            owner=self.intruder,
            name="intruder-repo",
            github_url="https://github.com/intruder/intruder-repo",
        )

        self.owner_pr = PullRequest.objects.create(
            repository=self.owner_repo,
            title="Owner PR",
            github_pr_number=1,
            source_branch="feature",
            target_branch="main",
        )
        self.intruder_pr = PullRequest.objects.create(
            repository=self.intruder_repo,
            title="Intruder PR",
            github_pr_number=2,
            source_branch="feature",
            target_branch="main",
        )

        self.owner_review = Review.objects.create(
            pull_request=self.owner_pr,
            commit_sha="a" * 40,
            status="completed",
            summary="Owner summary",
            score=8,
        )
        self.intruder_review = Review.objects.create(
            pull_request=self.intruder_pr,
            commit_sha="b" * 40,
            status="completed",
            summary="Intruder summary",
            score=3,
        )

        Finding.objects.create(
            review=self.intruder_review,
            file_path="secret.py",
            severity="critical",
            title="Intruder finding",
            description="Should stay private.",
        )

        self.client = APIClient()

    def test_review_list_only_returns_own_reviews(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("review-list"))

        self.assertEqual(response.status_code, 200)

        returned = [row["id"] for row in response.data["results"]] \
            if isinstance(response.data, dict) \
            else [row["id"] for row in response.data]

        self.assertIn(self.owner_review.id, returned)
        self.assertNotIn(self.intruder_review.id, returned)

    def test_review_detail_hides_other_users_reviews(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "review-detail",
                args=[self.intruder_review.id],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_review_detail_allows_owner(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("review-detail", args=[self.owner_review.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["summary"],
            "Owner summary",
        )

    def test_pull_request_review_get_hides_other_users_pull_requests(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse(
                "pull-request-review",
                args=[self.intruder_pr.id],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_pull_request_review_get_returns_own_findings(self):
        Finding.objects.create(
            review=self.owner_review,
            file_path="mine.py",
            severity="low",
            title="Owner finding",
            description="Fine.",
        )

        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pull-request-review", args=[self.owner_pr.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [f["file_path"] for f in response.data["findings"]],
            ["mine.py"],
        )

    def test_pull_request_list_only_returns_own_pull_requests(self):
        self.client.force_login(self.owner)

        with mock.patch(
            "reviews.serializers.get_oauth_github_client",
            return_value=mock.Mock(),
        ):
            with mock.patch(
                "reviews.serializers.PullRequestSerializer."
                "get_latest_commit_sha",
                return_value=None,
            ):
                response = self.client.get(
                    reverse("pull-request-list")
                )

        self.assertEqual(response.status_code, 200)

        returned = [row["id"] for row in response.data["results"]] \
            if isinstance(response.data, dict) \
            else [row["id"] for row in response.data]

        self.assertEqual(returned, [self.owner_pr.id])


class NormalizeScoreTests(TestCase):
    """
    Review.score is an IntegerField, so a fractional AI score has to be
    coerced before it is written to the database.
    """

    def test_review_score_accepts_ai_output(self):
        pull_request = PullRequest.objects.create(
            repository=Repository.objects.create(
                owner=User.objects.create_user(
                    username="scorer",
                    email="scorer@example.com",
                    password="strong-password-123",
                ),
                name="scored-repo",
                github_url="https://github.com/scorer/scored-repo",
            ),
            title="Scored PR",
            github_pr_number=7,
            source_branch="feature",
            target_branch="main",
        )

        review = Review.objects.create(
            pull_request=pull_request,
            commit_sha="c" * 40,
            status="completed",
        )
        review.score = normalize_score(8.6)
        review.full_clean(exclude=["started_at", "completed_at"])
        review.save()

        review.refresh_from_db()
        self.assertEqual(review.score, 9)