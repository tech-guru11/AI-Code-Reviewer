from rest_framework import serializers
from .models import Repository, PullRequest, Review, Finding


class RepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Repository
        fields = [
            'id', 'owner', 'name', 'github_url', 
            'description', 'language', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

class SyncRepositorySerializer(serializers.Serializer):
    repo_full_name = serializers.CharField(
        max_length=255
    )

    
class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        fields = [
            'id', 'file_path', 'line_number', 'severity', 
            'category', 'title', 'description', 'suggestion', 'code_snippet'
        ]


class ReviewDetailSerializer(serializers.ModelSerializer):
    # Includes nested findings for detailed review view
    findings = FindingSerializer(many=True, read_only=True)
    pull_request_title = serializers.ReadOnlyField(source='pull_request.title')

    class Meta:
        model = Review
        fields = [
            'id', 'pull_request', 'pull_request_title', 'status', 
            'summary', 'score', 'started_at', 'completed_at', 
            'created_at', 'findings'
        ]


class ReviewListSerializer(serializers.ModelSerializer):
    # Lightweight serializer for list view
    pull_request_title = serializers.ReadOnlyField(source='pull_request.title')

    class Meta:
        model = Review
        fields = ['id', 'pull_request', 'pull_request_title', 'status', 'score', 'created_at']



class PullRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = PullRequest
        fields = "__all__"



