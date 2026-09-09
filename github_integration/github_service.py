import os
from github import Github, GithubException, GithubIntegration
from .models import GitHubConnection
from .crypto import decrypt_github_token
from django.contrib.auth.models import User
from reviews.models import Repository, PullRequest, PullRequestFile, Commit

class GitHubAppService:

    def __init__(self):
        self.app_id = os.getenv("GITHUB_APP_ID")
        self.installation_id = os.getenv("GITHUB_INSTALLATION_ID")
        self.key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")

        with open(self.key_path, "r") as key_file:
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
    service = GitHubAppService()
    gh_client = service.get_client()

    try:
        # Get repository from GitHub
        gh_repo = gh_client.get_repo(repo_full_name)
    except GithubException as e:
        print(f"Error: Could not find or access repository '{repo_full_name}': {e}")
        return []

    # Get corresponding Django repository (ensure it exists first)
    repo, created = Repository.objects.get_or_create(
        owner=user,
        name=gh_repo.name
    )

    try:
        pull_requests = gh_repo.get_pulls(state="all")
        saved_prs = []

        for gh_pr in pull_requests:
            author = None
            if gh_pr.user:
                author, _ = User.objects.get_or_create(
                    username=gh_pr.user.login,
                    defaults={
                        "email": gh_pr.user.email or ""
                    }
                )

            pr, _ = PullRequest.objects.update_or_create(
                repository=repo,
                github_pr_number=gh_pr.number,
                defaults={
                    "title": gh_pr.title,
                    "author": author,
                    "source_branch": gh_pr.head.ref,
                    "target_branch": gh_pr.base.ref,
                    "status": gh_pr.state,
                }
            )
            saved_prs.append(pr)

        return saved_prs

    except GithubException as e:
        print(f"Error fetching pull requests for {repo_full_name}: {e}")
        return []

def sync_single_pull_request_to_db(repo_full_name: str, pr_number: int):
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

    # Find the Django repository using the GitHub URL
    repository = Repository.objects.get(
        github_url=gh_repo.html_url
    )

    # Get or create the GitHub user who opened the PR
    author = None

    if gh_pr.user:
        author, _ = User.objects.get_or_create(
            username=gh_pr.user.login,
            defaults={
                "email": gh_pr.user.email or ""
            }
        )

    # Create or update the Django PullRequest
    pull_request, created = PullRequest.objects.update_or_create(
        repository=repository,
        github_pr_number=gh_pr.number,
        defaults={
            "title": gh_pr.title,
            "author": author,
            "source_branch": gh_pr.head.ref,
            "target_branch": gh_pr.base.ref,
            "status": gh_pr.state,
        }
    )

    print(
        f"{'Created' if created else 'Updated'} "
        f"Django PullRequest: ID={pull_request.id} "
        f"PR=#{gh_pr.number}"
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
            repository__name=gh_repo.name,
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

    except (GithubException, PullRequest.DoesNotExist) as e:
        print(f"Error syncing files for PR #{pr_number} in {repo_full_name}: {e}")
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
            repository__name=gh_repo.name,
            github_pr_number=pr_number
        )

        commits_data = gh_pr.get_commits()
        saved_commits = []

        for commit_info in commits_data:
            sha = commit_info.sha
            commit_detail = commit_info.commit
            author_info = commit_detail.author

            commit_obj, _ = Commit.objects.update_or_create(
                sha=sha,
                defaults={
                    "repository": pr.repository,
                    "pull_request": pr,  # Connects commit to Pull Request
                    "author_name": author_info.name if author_info else "",
                    "author_email": author_info.email if author_info else "",
                    "message": commit_detail.message,
                    "committed_at": author_info.date if author_info else None,
                }
            )
            saved_commits.append(commit_obj)

        return saved_commits

    except (GithubException, PullRequest.DoesNotExist) as e:
        print(f"Error syncing commits for PR #{pr_number} in {repo_full_name}: {e}")
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

        print(
            f"GitHub comment posted successfully "
            f"to PR #{pr_number}"
        )

        return github_comment

    except GithubException as e:
        print(
            f"Failed to post GitHub comment "
            f"to PR #{pr_number}: {e}"
        )
        return None