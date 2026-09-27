from rest_framework import serializers
from .models import Repository, PullRequest, Review, Finding
from github_integration.github_service import get_oauth_github_client
class RepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Repository
        fields = [
            'id',
            'owner',
            'name',
            'github_url',
            'description',
            'language',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'owner',
            'created_at',
            'updated_at',
        ]


class SyncRepositorySerializer(serializers.Serializer):
    repo_full_name = serializers.CharField(max_length=255)


class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        fields = [
            'id',
            'file_path',
            'line_number',
            'severity',
            'category',
            'title',
            'description',
            'suggestion',
            'code_snippet',
        ]


class ReviewDetailSerializer(serializers.ModelSerializer):
    findings = FindingSerializer(many=True, read_only=True)
    pull_request_title = serializers.ReadOnlyField(
        source='pull_request.title'
    )

    class Meta:
        model = Review
        fields = [
            'id',
            'pull_request',
            'pull_request_title',
            'status',
            'summary',
            'score',
            'started_at',
            'completed_at',
            'created_at',
            'findings',
        ]


class ReviewListSerializer(serializers.ModelSerializer):
    pull_request_title = serializers.ReadOnlyField(
        source='pull_request.title'
    )

    finding_count = serializers.SerializerMethodField()

    critical_count = serializers.SerializerMethodField()
    high_count = serializers.SerializerMethodField()
    medium_count = serializers.SerializerMethodField()
    low_count = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            'id',
            'pull_request',
            'pull_request_title',
            'commit_sha',
            'status',
            'score',
            'finding_count',
            'critical_count',
            'high_count',
            'medium_count',
            'low_count',
            'created_at',
        ]

    def get_finding_count(self, obj):
        return obj.findings.count()

    def get_critical_count(self, obj):
        return obj.findings.filter(severity='critical').count()

    def get_high_count(self, obj):
        return obj.findings.filter(severity='high').count()

    def get_medium_count(self, obj):
        return obj.findings.filter(severity='medium').count()

    def get_low_count(self, obj):
        return obj.findings.filter(severity='low').count()

class PullRequestSerializer(serializers.ModelSerializer):
    latest_commit_sha = serializers.SerializerMethodField()

    class Meta:
        model = PullRequest
        fields = [
            'id',
            'repository',
            'title',
            'github_pr_number',
            'author',
            'source_branch',
            'target_branch',
            'status',
            'created_at',
            'updated_at',
            'latest_commit_sha',
        ]

    def get_latest_commit_sha(self, obj):
        # This runs once per row, so the client is built a single time and
        # reused for the rest of the page.
        client = self._get_github_client()

        if client is None:
            return None

        try:
            repo_full_name = (
                obj.repository.github_url
                .replace("https://github.com/", "")
                .rstrip("/")
            )

            github_repo = client.get_repo(repo_full_name)
            github_pr = github_repo.get_pull(obj.github_pr_number)

            return github_pr.head.sha

        except Exception:
            return None

    def _get_github_client(self):
        """
        Build the requesting user's GitHub client once per serializer.

        The head SHA must be read with the requesting user's own token so
        the lookup is limited to the repositories that user can access.
        """
        if getattr(self, "_github_client", None) is not None:
            return self._github_client

        request = self.context.get("request")
        user = getattr(request, "user", None)

        client = None

        if user is not None and user.is_authenticated:
            try:
                client = get_oauth_github_client(user)
            except Exception:
                client = None

        self._github_client = client

        return client