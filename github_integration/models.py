from django.db import models

class PullRequestReview(models.Model):
    repo_full_name = models.CharField(max_length=255)
    pr_number = models.IntegerField()
    review_summary = models.TextField()
    raw_ai_response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.repo_full_name} - PR #{self.pr_number}"