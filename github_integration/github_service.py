import logging
import os

from github import Github, GithubException, GithubIntegration
from django.utils import timezone
from .models import GitHubConnection
from .crypto import decrypt_github_token
from django.contrib.auth.models import User
from reviews.models import Repository, PullRequest, PullRequestFile, Commit

logger = logging.getLogger(__name__)


class GitHubAppService:

    def __init__(self):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.installation_id = os.getenv("GITHUB_INSTALLATION_ID")

        # Production: private key comes directly from an environment variable.
        # Local development: fall back to the private-key file.
        self.private_key = os.getenv("GITHUB_PRIVATE_KEY")

        if not self.private_key:
            key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")

            if not key_path:
                raise ValueError(
                    "GitHub private key is not configured."
                )

            with open(key_path, "r") as key_file:
                self.private_key = key_file.read()

        self.integration = GithubIntegration(
            integration_id=self.app_id,
            private_key=self.private_key,
        )

    def get_client(self) -> Github:
        access_token = self.integration.get_access_token(self.installation_id).token
        return Github(login_or_token=access_token)

def get_oauth_github_client(user: User) -> Github:
    """
    Creates a GitHub API client using the OAuth token
    belonging to the authenticated Django user.
    """
    try:
        connection = GitHubConnection.objects.get(user=user)
    except GitHubConnection.DoesNotExist:
        raise ValueError(
            "GitHub account is not connected. Please connect GitHub first."
        )

    if not connection.access_token:
        raise ValueError(
            "GitHub access token is missing. Please reconnect GitHub."
        )

    return Github(
        login_or_token=decrypt_github_token(
            connection.access_token
        )
    )

def get_user_github_repositories(user: User):
    """
    Returns repositories accessible to the authenticated
    user's connected GitHub account.
    """
    gh_client = get_oauth_github_client(user)

    gh_user = gh_client.get_user()

    repositories = []

    for gh_repo in gh_user.get_repos(
        visibility="all",
        affiliation="owner,collaborator,organization_member",
        sort="updated",
    ):
        repositories.append({
            "id": gh_repo.id,
            "name": gh_repo.name,
            "full_name": gh_repo.full_name,
            "github_url": gh_repo.html_url,
            "description": gh_repo.description or "",
            "language": gh_repo.language or "",
            "private": gh_repo.private,
            "default_branch": gh_repo.default_branch,
        })

    return repositories

def resolve_github_author(gh_user):
    """
    Map a GitHub user onto the local Django account that owns them.

    Only accounts that completed the GitHub OAuth connection are matched.
    Creating a placeholder user from a raw GitHub login would let an
    unverified GitHub account silently claim a local username, and would
    block that username from ever being registered.
    """
    if gh_user is None or not gh_user.login:
        return None

    try:
        connection = GitHubConnection.objects.select_related(
            "user"
        ).get(github_username=gh_user.login)
    except GitHubConnection.DoesNotExist:
        return None

    return connection.user


def sync_repository_to_db(repo_full_name: str, user: User) -> Repository:
    """
    Retrieves repository details from GitHub using the
    authenticated user's OAuth token and saves/updates it
    in the Repository table.
    """
    gh_client = get_oauth_github_client(user)

    gh_repo = gh_client.get_repo(repo_full_name)

    repo, created = Repository.objects.update_or_create(
        owner=user,
        github_url=gh_repo.html_url,
        defaults={
            "name": gh_repo.name,
            "description": gh_repo.description or "",
            "language": gh_repo.language or "",
        },
    )

    return repo


def sync_pull_requests_to_db(repo_full_name: str, user: User):
    """
    Retrieves pull requests from GitHub and saves/updates
    them in the Django PullRequest table.
    """
    gh_client = get_oauth_github_client(user)

    try:
        # Get repository from GitHub
        gh_repo = gh_client.get_repo(repo_full_name)
    except GithubException as e:
        logger.error(
            "Could not find or access repository '%s': %s",
            repo_full_name,
            e,
        )
        return []

    # Get corresponding Django repository (ensure it exists first)
    repo, created = Repository.objects.update_or_create(
        owner=user,
        github_url=gh_repo.html_url,
        defaults={
            "name": gh_repo.name,
            "description": gh_repo.description or "",
            "language": gh_repo.language or "",
        },
    )

    try:
        pull_requests = gh_repo.get_pulls(state="all")
        saved_prs = []

        for gh_pr in pull_requests:
            pr, _ = PullRequest.objects.update_or_create(
                repository=repo,
                github_pr_number=gh_pr.number,
                defaults={
                    "title": gh_pr.title,
                    "author": resolve_github_author(gh_pr.user),
                    "source_branch": gh_pr.head.ref,
                    "target_branch": gh_pr.base.ref,
                    "status": gh_pr.state,
                }
            )
            saved_prs.append(pr)

        return saved_prs

    except GithubException as e:
        logger.error(
            "Error fetching pull requests for %s: %s",
            repo_full_name,
            e,
        )
        return []

def get_registered_repository(
    repo_full_name: str,
    user: User = None,
) -> Repository:
    """
    Resolve the Repository row that a webhook event refers to.

    A GitHub repository can be registered by several users, so github_url
    alone is not unique and a plain get() would raise
    MultipleObjectsReturned. When the caller knows the owner the lookup is
    scoped to them; otherwise the registration whose owner connected that
    GitHub account wins, falling back to the oldest registration.
    """
    matches = Repository.objects.filter(
        github_url=f"https://github.com/{repo_full_name}"
    )

    if user is not None:
        matches = matches.filter(owner=user)

    candidates = list(
        matches.select_related("owner", "owner__github_connection")
    )

    if not candidates:
        raise Repository.DoesNotExist(
            f"Repository '{repo_full_name}' is not registered."
        )

    if len(candidates) == 1:
        return candidates[0]

    owner_login = repo_full_name.split("/", 1)[0]

    for candidate in candidates:
        connection = getattr(
            candidate.owner, "github_connection", None
        )

        if connection and connection.github_username == owner_login:
            return candidate

    logger.warning(
        "Repository '%s' is registered by %d users; "
        "using the oldest registration.",
        repo_full_name,
        len(candidates),
    )

    return min(candidates, key=lambda repo: repo.created_at)


def sync_single_pull_request_to_db(
    repo_full_name: str,
    pr_number: int,
    user: User = None,
):
    """
    Retrieve one Pull Request from GitHub and create/update
    the corresponding Django PullRequest record.
    """

    service = GitHubAppService()
    gh_client = service.get_client()

    # Get GitHub repository
    gh_repo = gh_client.get_repo(repo_full_name)

    # Get GitHub Pull Request
    gh_pr = gh_repo.get_pull(pr_number)

    # Find the Django repository that this pull request belongs to
    repository = get_registered_repository(
        repo_full_name,
        user=user,
    )

    # Create or update the Django PullRequest
    pull_request, created = PullRequest.objects.update_or_create(
        repository=repository,
        github_pr_number=gh_pr.number,
        defaults={
            "title": gh_pr.title,
            "author": resolve_github_author(gh_pr.user),
            "source_branch": gh_pr.head.ref,
            "target_branch": gh_pr.base.ref,
            "status": gh_pr.state,
        }
    )

    logger.info(
        "%s Django PullRequest: ID=%s PR=#%s",
        "Created" if created else "Updated",
        pull_request.id,
        gh_pr.number,
    )

    return pull_request

def sync_pull_request_files(repo_full_name: str, pr_number: int):
    """
    Phase E — Changed files:
    Retrieves files changed by a PR via PyGithub, saves them,
    and stores their diff/patch information safely.
    """
    service = GitHubAppService()
    gh_client = service.get_client()

    try:
        gh_repo = gh_client.get_repo(repo_full_name)
        gh_pr = gh_repo.get_pull(pr_number)

        pr = PullRequest.objects.get(
            repository__github_url=gh_repo.html_url,
            github_pr_number=pr_number
        )

        files_data = gh_pr.get_files()
        saved_files = []

        for file_info in files_data:
            # Safely fallback to an empty string if patch is None
            patch_content = getattr(file_info, "patch", None) or ""

            pr_file, _ = PullRequestFile.objects.update_or_create(
                pull_request=pr,
                filename=file_info.filename,
                defaults={
                    "status": file_info.status,
                    "additions": file_info.additions,
                    "deletions": file_info.deletions,
                    "changes": file_info.changes,
                    "patch": patch_content,
                }
            )
            saved_files.append(pr_file)

        return saved_files

    except (
        GithubException,
        PullRequest.DoesNotExist,
        PullRequest.MultipleObjectsReturned,
    ) as e:
        logger.error(
            "Error syncing files for PR #%s in %s: %s",
            pr_number,
            repo_full_name,
            e,
        )
        return []

def sync_pull_request_commits(repo_full_name: str, pr_number: int):
    """
    Phase F — Commits:
    Retrieves commits from a PR via PyGithub, saves them,
    and connects them to the Pull Request.
    """
    service = GitHubAppService()
    gh_client = service.get_client()

    try:
        gh_repo = gh_client.get_repo(repo_full_name)
        gh_pr = gh_repo.get_pull(pr_number)

        pr = PullRequest.objects.get(
            repository__github_url=gh_repo.html_url,
            github_pr_number=pr_number
        )

        commits_data = gh_pr.get_commits()
        saved_commits = []

        for commit_info in commits_data:
            sha = commit_info.sha
            commit_detail = commit_info.commit
            author_info = commit_detail.author
            committer_info = commit_detail.committer

            # committed_at is a required column, so fall back to the
            # committer date and finally to the sync time.
            if author_info and author_info.date:
                committed_at = author_info.date
            elif committer_info and committer_info.date:
                committed_at = committer_info.date
            else:
                committed_at = timezone.now()

            commit_obj, _ = Commit.objects.update_or_create(
                sha=sha,
                defaults={
                    "repository": pr.repository,
                    "pull_request": pr,  # Connects commit to Pull Request
                    "author_name": author_info.name if author_info else "",
                    "author_email": author_info.email if author_info else "",
                    "message": commit_detail.message,
                    "committed_at": committed_at,
                }
            )
            saved_commits.append(commit_obj)

        return saved_commits

    except (
        GithubException,
        PullRequest.DoesNotExist,
        PullRequest.MultipleObjectsReturned,
    ) as e:
        logger.error(
            "Error syncing commits for PR #%s in %s: %s",
            pr_number,
            repo_full_name,
            e,
        )
        return []

def post_pull_request_comment(
    repo_full_name: str,
    pr_number: int,
    comment: str
):
    """
    Posts a comment to a GitHub Pull Request.
    """

    service = GitHubAppService()
    gh_client = service.get_client()

    try:
        # Get the GitHub repository
        gh_repo = gh_client.get_repo(repo_full_name)

        # Get the Pull Request
        gh_pr = gh_repo.get_pull(pr_number)

        # Convert PR to an Issue object
        issue = gh_pr.as_issue()

        # Create the GitHub comment
        github_comment = issue.create_comment(comment)

        logger.info(
            "GitHub comment posted successfully to PR #%s",
            pr_number
        )

        return github_comment

    except GithubException as e:
        logger.error(
            "Failed to post GitHub comment to PR #%s: %s",
            pr_number,
            e,
        )
        return None
