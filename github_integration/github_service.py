import os
from github import Github, GithubIntegration
from django.contrib.auth.models import User
from reviews.models import Repository, PullRequest

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


def sync_repository_to_db(repo_full_name: str, user: User) -> Repository:
    """
    Retrieves repository details from GitHub API via PyGithub
    and saves/updates it in the reviews.models.Repository table.
    """
    service = GitHubAppService()
    gh_client = service.get_client()

    gh_repo = gh_client.get_repo(repo_full_name)

    repo, created = Repository.objects.update_or_create(
        owner=user,
        name=gh_repo.name,
        defaults={
            "github_url": gh_repo.html_url,
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

    # Get repository from GitHub
    gh_repo = gh_client.get_repo(repo_full_name)

    # Get corresponding Django repository
    repo = Repository.objects.get(
        owner=user,
        name=gh_repo.name
    )

    pull_requests = gh_repo.get_pulls(
        state="all"
    )

    saved_prs = []

    for gh_pr in pull_requests:

        # Find or create the author
        author = None

        if gh_pr.user:
            author, _ = User.objects.get_or_create(
                username=gh_pr.user.login,
                defaults={
                    "email": gh_pr.user.email or ""
                }
            )

        pr, created = PullRequest.objects.update_or_create(
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