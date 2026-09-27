import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from .github_service import get_registered_repository, resolve_github_author
from .models import GitHubConnection
from .tasks import normalize_score, process_manual_review
from reviews.models import PullRequest, Repository, Review


class ResolveGitHubAuthorTests(TestCase):
    """
    A GitHub login must only map onto a local account that actually
    completed the GitHub OAuth connection.
    """

    def test_connected_account_is_matched(self):
        user = User.objects.create_user(
            username="linked",
            email="linked@example.com",
            password="strong-password-123",
        )
        GitHubConnection.objects.create(
            user=user,
            github_user_id=4242,
            github_username="octocat",
            access_token="fernet:x",
        )

        self.assertEqual(
            resolve_github_author(mock.Mock(login="octocat")),
            user,
        )

    def test_unconnected_login_does_not_create_a_user(self):
        self.assertIsNone(
            resolve_github_author(mock.Mock(login="stranger"))
        )
        self.assertFalse(
            User.objects.filter(username="stranger").exists()
        )

    def test_github_login_cannot_claim_an_existing_local_account(self):
        User.objects.create_user(
            username="octocat",
            email="local-octocat@example.com",
            password="strong-password-123",
        )

        self.assertIsNone(
            resolve_github_author(mock.Mock(login="octocat"))
        )
        self.assertEqual(
            User.objects.filter(username="octocat").count(),
            1,
        )

    def test_missing_github_user_is_handled(self):
        self.assertIsNone(resolve_github_author(None))
        self.assertIsNone(
            resolve_github_author(mock.Mock(login=""))
        )


class NormalizeScoreTests(TestCase):
    """
    Review.score is an IntegerField, so fractional AI scores have to be
    coerced before they are stored.
    """

    def test_fractional_score_is_rounded(self):
        self.assertEqual(normalize_score(7.5), 8)
        self.assertEqual(normalize_score(7.4), 7)
        self.assertEqual(normalize_score("9"), 9)

    def test_score_is_clamped(self):
        self.assertEqual(normalize_score(11), 10)
        self.assertEqual(normalize_score(-3), 0)

    def test_missing_or_invalid_score_becomes_none(self):
        self.assertIsNone(normalize_score(None))
        self.assertIsNone(normalize_score("not-a-score"))


class GetRegisteredRepositoryTests(TestCase):
    """
    A GitHub repository can be registered by more than one user, so the
    lookup has to resolve to a single deterministic Repository row.
    """

    URL = "https://github.com/octocat/hello-world"

    def _user(self, username, email):
        return User.objects.create_user(
            username=username,
            email=email,
            password="strong-password-123",
        )

    def _repo(self, owner):
        return Repository.objects.create(
            owner=owner,
            name="hello-world",
            github_url=self.URL,
        )

    def test_unregistered_repository_raises(self):
        with self.assertRaises(Repository.DoesNotExist):
            get_registered_repository("octocat/hello-world")

    def test_single_registration_is_returned(self):
        owner = self._user("solo", "solo@example.com")
        repo = self._repo(owner)

        self.assertEqual(
            get_registered_repository("octocat/hello-world"),
            repo,
        )

    def test_owner_argument_scopes_the_lookup(self):
        first = self._user("first", "first@example.com")
        second = self._user("second", "second@example.com")
        first_repo = self._repo(first)
        second_repo = self._repo(second)

        self.assertEqual(
            get_registered_repository(
                "octocat/hello-world",
                user=second,
            ),
            second_repo,
        )
        self.assertEqual(
            get_registered_repository(
                "octocat/hello-world",
                user=first,
            ),
            first_repo,
        )

    def test_owner_argument_without_match_raises(self):
        self._user("first", "first@example.com")
        self._repo(self._user("second", "second@example.com"))

        with self.assertRaises(Repository.DoesNotExist):
            get_registered_repository(
                "octocat/hello-world",
                user=User.objects.get(username="first"),
            )

    def test_matching_github_login_wins_over_other_registrations(self):
        matching_user = self._user("matching", "matching@example.com")
        matching_repo = self._repo(matching_user)

        GitHubConnection.objects.create(
            user=matching_user,
            github_user_id=1,
            github_username="octocat",
            access_token="fernet:x",
        )

        # Registered first, so it is the older row, but the GitHub login
        # match should still be preferred over the unrelated user below.
        self._repo(self._user("unrelated", "unrelated@example.com"))

        self.assertEqual(
            get_registered_repository("octocat/hello-world"),
            matching_repo,
        )

    def test_without_login_match_oldest_registration_wins(self):
        oldest_user = self._user("oldest", "oldest@example.com")
        oldest_repo = self._repo(oldest_user)
        self._repo(self._user("newer", "newer@example.com"))

        self.assertEqual(
            get_registered_repository("octocat/hello-world"),
            oldest_repo,
        )


class ProcessManualReviewTests(TestCase):
    """
    End-to-end coverage of the AI review pipeline with GitHub and the
    Groq API mocked out.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="pipeline",
            email="pipeline@example.com",
            password="strong-password-123",
        )
        self.repo = Repository.objects.create(
            owner=self.user,
            name="hello-world",
            github_url="https://github.com/octocat/hello-world",
        )
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            title="Add feature",
            github_pr_number=5,
            source_branch="feature",
            target_branch="main",
        )

    def _github_mocks(self, create_issue_comment):
        gh_file = mock.Mock(
            filename="app.py",
            status="modified",
            additions=2,
            deletions=1,
            changes=3,
            patch="@@ -1 +1 @@\n+import os",
        )
        gh_file.filename = "app.py"

        gh_pr = mock.Mock(
            number=5,
            title="Add feature",
            head=mock.Mock(ref="feature", sha="b" * 40),
            base=mock.Mock(ref="main"),
        )
        gh_pr.get_files.return_value = [gh_file]
        gh_pr.create_issue_comment.side_effect = create_issue_comment

        gh_repo = mock.Mock(full_name="octocat/hello-world")
        gh_repo.get_pull.return_value = gh_pr

        gh_client = mock.Mock()
        gh_client.get_repo.return_value = gh_repo

        return gh_client, gh_pr

    def _ai_mock(self, payload):
        message = mock.Mock(content=json.dumps(payload))
        return mock.Mock(choices=[mock.Mock(message=message)])

    def _run(self, score, issue_count=1, review=None):
        gh_client, gh_pr = self._github_mocks(
            lambda body: mock.Mock(id=1, html_url="u")
        )
        payload = {
            "summary": "Looks risky.",
            "score": score,
            "issues": [
                {
                    "file": "app.py",
                    "line": 1,
                    "severity": "HIGH",
                    "category": "Security",
                    "problem": "Uses os.system",
                    "suggestion": "Use subprocess",
                    "code_snippet": "import os",
                }
            ][:issue_count],
        }

        with mock.patch(
            "github_integration.tasks.GitHubAppService"
        ) as service, mock.patch(
            "github_integration.tasks.get_ai_client"
        ) as ai_client:
            service.return_value.get_client.return_value = gh_client
            ai_client.return_value.chat.completions.create.return_value = \
                self._ai_mock(payload)

            result = process_manual_review(
                self.pr.id,
                "b" * 40,
                review.id if review else None,
            )

        return result

    def test_fractional_ai_score_is_stored_as_integer(self):
        review = Review.objects.create(
            pull_request=self.pr,
            commit_sha="b" * 40,
            status="processing",
        )

        self._run(7.5, review=review)

        review.refresh_from_db()
        self.assertEqual(review.status, "completed")
        self.assertEqual(review.score, 8)
        # Full clean is what would blow up on MariaDB/PostgreSQL.
        review.full_clean(exclude=["started_at", "completed_at"])

    def test_findings_are_created(self):
        review = Review.objects.create(
            pull_request=self.pr,
            commit_sha="b" * 40,
            status="processing",
        )

        self._run(9.0, review=review)

        self.assertEqual(review.findings.count(), 1)
        finding = review.findings.first()
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.category, "security")

    def test_rerun_does_not_duplicate_findings(self):
        review = Review.objects.create(
            pull_request=self.pr,
            commit_sha="b" * 40,
            status="processing",
        )

        self._run(9.0, review=review)
        self._run(9.0, review=review)
        self._run(9.0, review=review)

        self.assertEqual(review.findings.count(), 1)

    def test_review_created_when_no_id_given(self):
        result = self._run(6.0)

        review = Review.objects.get(id=result["review_id"])
        self.assertEqual(review.status, "completed")
        self.assertEqual(review.score, 6)
        self.assertEqual(review.findings.count(), 1)
