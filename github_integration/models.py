from django.db import models
from django.contrib.auth.models import User


class PullRequestReview(models.Model):
    repo_full_name = models.CharField(max_length=255)
    pr_number = models.IntegerField()
    review_summary = models.TextField()
    raw_ai_response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.repo_full_name} - PR #{self.pr_number}"


class GitHubConnection(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="github_connection",
    )

    github_user_id = models.BigIntegerField(
        unique=True
    )

    github_username = models.CharField(
        max_length=255
    )

    access_token = models.TextField()

    connected_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    def __str__(self):
        return f"{self.user.username} -> @{self.github_username}"