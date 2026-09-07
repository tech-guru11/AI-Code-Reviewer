from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

class Repository(models.Model):
    owner = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name="repositories"
    )
    name = models.CharField(max_length=255)
    github_url = models.URLField()
    description = models.TextField(blank=True)
    language = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class PullRequest(models.Model):
    repository = models.ForeignKey(
        Repository, 
        on_delete=models.CASCADE, 
        related_name="pull_requests"
    )
    title = models.CharField(max_length=255)
    github_pr_number = models.IntegerField()
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pull_requests",
    )
    source_branch = models.CharField(max_length=255)
    target_branch = models.CharField(max_length=255)
    status = models.CharField(max_length=50, default="open")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.repository.name} - PR #{self.github_pr_number}"

class Review(models.Model):
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="reviews"
    )

    commit_sha = models.CharField(
        max_length=40,
        blank=True,
        null=True
    )

    status = models.CharField(max_length=50, default="pending")
    summary = models.TextField(blank=True)
    score = models.IntegerField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pull_request", "commit_sha"],
                condition=Q(commit_sha__isnull=False),
                name="unique_review_per_pull_request_commit",
            ),
        ]

    def __str__(self):
        return f"Review for {self.pull_request}"


class Finding(models.Model):
    SEVERITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    ]

    CATEGORY_CHOICES = [
        ("bug", "Bug"),
        ("security", "Security"),
        ("performance", "Performance"),
        ("style", "Code Style"),
        ("quality", "Code Quality"),
    ]

    review = models.ForeignKey(
        Review, 
        on_delete=models.CASCADE, 
        related_name="findings"
    )
    file_path = models.CharField(max_length=500)
    line_number = models.IntegerField(null=True, blank=True)
    severity = models.CharField(
        max_length=20, 
        choices=SEVERITY_CHOICES, 
        default="medium"
    )
    category = models.CharField(
        max_length=50, 
        choices=CATEGORY_CHOICES, 
        default="quality"
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    suggestion = models.TextField(blank=True)
    code_snippet = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.severity.upper()} - {self.title}"


class PullRequestFile(models.Model):
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="changed_files"
    )
    filename = models.CharField(max_length=500)
    status = models.CharField(max_length=50, blank=True)
    additions = models.IntegerField(default=0)
    deletions = models.IntegerField(default=0)
    changes = models.IntegerField(default=0)
    patch = models.TextField(blank=True, null=True)  # <-- Added blank=True, null=True
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.filename} ({self.status}) in {self.pull_request}"


class Commit(models.Model):
    """Stores Git commits associated with a repository and optionally linked to a Pull Request."""
    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="commits"
    )
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commits"
    )
    sha = models.CharField(max_length=40, unique=True)  # Git commit hash
    author_name = models.CharField(max_length=255, blank=True)
    author_email = models.EmailField(blank=True)
    message = models.TextField()
    committed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.sha[:7]} - {self.message[:50]}"




class PullRequestReview(models.Model):
    repo_full_name = models.CharField(max_length=255)
    pr_number = models.IntegerField()
    review_summary = models.TextField()
    raw_ai_response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.repo_full_name} - PR #{self.pr_number}"