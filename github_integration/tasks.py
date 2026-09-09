import os
import json
import logging

from celery import shared_task
from openai import OpenAI, files
from django.utils import timezone
from github import Github, GithubException
from .models import PullRequestReview
from reviews.models import Repository, PullRequest, Review, Finding
from .github_service import (
    GitHubAppService,
    sync_pull_request_files,
    sync_pull_request_commits,
    sync_single_pull_request_to_db,
)

logger = logging.getLogger(__name__)
# Initialize the Groq client
client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY"),
)
@shared_task
def process_github_event(event_type, payload):
    """
    Process GitHub webhook events.

    For pull_request events, trigger the full AI code review.
    """

    if event_type != "pull_request":
        logger.info(f"Ignoring GitHub event: {event_type}")

        return {
            "status": "ignored",
            "event": event_type,
        }

    action = payload.get("action")
    pr_data = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})

    commit_sha = (
        pr_data.get("head", {}).get("sha")
    )

    repo_full_name = repo_data.get("full_name")
    pr_number = pr_data.get("number")
    logger.info(
    f"Commit SHA: {commit_sha}"
)

    logger.info(
        f"GitHub Pull Request event received: "
        f"PR #{pr_number}, action={action}, "
        f"repository={repo_full_name}"
    )

    # Only review these PR actions
    if action not in ["opened", "synchronize", "reopened"]:
        logger.info(
            f"Ignoring pull_request action: {action}"
        )

        return {
            "status": "ignored",
            "event": event_type,
            "action": action,
        }

    try:
        # Find the Django PullRequest record
        repository = Repository.objects.get(
             github_url=f"https://github.com/{repo_full_name}"
    )

        logger.info(
            f"Repository found in database: "
            f"{repository.name}"
        )
        pull_request = sync_single_pull_request_to_db(
            repo_full_name,
            pr_number,
        )

        logger.info(
            f"Django PullRequest synchronized: "
            f"ID={pull_request.id}"
        )

        # Check whether this exact commit has already been reviewed
        existing_review = Review.objects.filter(
            pull_request=pull_request,
            commit_sha=commit_sha,
        ).first()

        if existing_review:
            logger.info(
                f"Review already exists for commit "
                f"{commit_sha}. Skipping duplicate review."
            )

            return {
                "status": "already_reviewed",
                "event": event_type,
                "action": action,
                "repository": repo_full_name,
                "pr_number": pr_number,
                "pull_request_id": pull_request.id,
                "review_id": existing_review.id,
                "commit_sha": commit_sha,
            }

        # Start the AI review pipeline
        task = process_manual_review.delay(
            pull_request.id,
            commit_sha
        )

        logger.info(
            f"Manual AI review queued. "
            f"Celery task ID: {task.id}"
        )
        return {
            "status": "review_queued",
            "event": event_type,
            "action": action,
            "repository": repo_full_name,
            "pr_number": pr_number,
            "pull_request_id": pull_request.id,
            "task_id": task.id,
        }

    except Exception:
        logger.exception(
            "Failed processing GitHub pull_request event."
        )

        return {
            "status": "failed",
            "error": "Failed to process GitHub pull request event.",
        }
@shared_task
def process_manual_review(
    pull_request_id,
    commit_sha=None,
    review_id=None,
):
    logger.info(
        f"Starting manual review for PullRequest ID: {pull_request_id}"
    )

    try:
        # 1. Get Pull Request from Django
        pull_request = PullRequest.objects.select_related(
            "repository"
        ).get(id=pull_request_id)

        # 2. Prevent duplicate reviews for the same commit

        if review_id:
            # The API already created the Review record.
            # Celery should process that exact review.
            review = Review.objects.get(
                id=review_id,
                pull_request=pull_request,
            )

            logger.info(
                f"Using existing Review ID: {review.id}"
            )

        else:
            # Backwards compatibility for webhook-triggered reviews
            # and any other code that still calls this task with only
            # pull_request_id and commit_sha.

            if commit_sha:
                existing_review = Review.objects.filter(
                    pull_request=pull_request,
                    commit_sha=commit_sha,
                    status="completed",
                ).first()

                if existing_review:
                    logger.info(
                        f"Review already exists for commit {commit_sha}"
                    )

                    return {
                        "pull_request_id": pull_request_id,
                        "commit_sha": commit_sha,
                        "status": "already_reviewed",
                        "review_id": existing_review.id,
                    }

            review = Review.objects.create(
                pull_request=pull_request,
                commit_sha=commit_sha,
                status="processing",
                started_at=timezone.now(),
            )

            logger.info(
                f"Created new Review ID: {review.id}"
            )

        repository = pull_request.repository

        logger.info(f"Repository: {repository.name}")
        logger.info(
            f"GitHub PR Number: {pull_request.github_pr_number}"
        )

                # 2. Get GitHub client using existing GitHub App
        service = GitHubAppService()
        github_client = service.get_client()

        repo_full_name = (
            repository.github_url
            .replace("https://github.com/", "")
            .rstrip("/")
        )

        logger.info(
            f"Connecting to GitHub repository: {repo_full_name}"
        )

        try:
            github_repo = github_client.get_repo(repo_full_name)

            logger.info(
                f"Connected to GitHub repository: "
                f"{github_repo.full_name}"
            )

            github_pr = github_repo.get_pull(
                pull_request.github_pr_number
            )

        except Exception as github_error:
            logger.exception(
                "GitHub API request failed for PullRequest ID %s.",
                pull_request_id,
            )

            review.status = "failed"
            review.completed_at = timezone.now()
            review.save()

            return {
                "pull_request_id": pull_request_id,
                "status": "failed",
                "error": "Failed to retrieve the GitHub pull request.",
            }

        logger.info(
            f"Retrieved GitHub PR: #{github_pr.number}"
        )

        logger.info(
            f"PR title: {github_pr.title}"
        )

        logger.info(
            f"Source branch: {github_pr.head.ref}"
        )

        logger.info(
            f"Target branch: {github_pr.base.ref}"
        )
                # 5. Get changed files
        try:
            files = github_pr.get_files()
            logger.info(
                f"GitHub reports {files.totalCount} changed files."
            )
        except Exception as files_error:
            logger.exception(
                "Failed to retrieve changed files for PullRequest ID %s.",
                pull_request_id,
            )

            review.status = "failed"
            review.completed_at = timezone.now()
            review.save()

            return {
                "pull_request_id": pull_request_id,
                "status": "failed",
                "error": "Failed to retrieve changed files from GitHub.",
            }

        changed_files = []

        for file in files:
            logger.debug("Changed file: %s", file.filename)
            logger.debug("File status: %s", file.status)
            logger.debug("File additions: %s", file.additions)
            logger.debug("File deletions: %s", file.deletions)
            logger.debug("Patch available: %s", bool(file.patch))
            logger.debug("File extension: %s", file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "none")

            code_extensions = {
                ".py",
                ".js",
                ".jsx",
                ".ts",
                ".tsx",
                ".java",
                ".c",
                ".cpp",
                ".h",
                ".hpp",
                ".html",
                ".css",
                ".php",
                ".go",
                ".rs",
                ".rb",
                ".swift",
                ".kt",
            }

            extension = ""

            if "." in file.filename:
                extension = "." + file.filename.split(".")[-1].lower()

            if extension not in code_extensions:
                logger.info(
                    f"Skipping non-code file: {file.filename}"
                )
                continue


            changed_files.append({
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "changes": file.changes,
                "patch": file.patch or "",
            })

        logger.info(
            f"Total changed files: {len(changed_files)}"
        )
                # 6. Build code review input
        code_for_review = ""

        for changed_file in changed_files:
            code_for_review += f"""
File: {changed_file['filename']}

Status: {changed_file['status']}
Additions: {changed_file['additions']}
Deletions: {changed_file['deletions']}

Patch:
{changed_file['patch']}

----------------------------------------
"""

        logger.info("Prepared code for AI review.")

                # 7. Send code to Groq AI
        logger.info("Sending code to Groq AI...")

        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                   "content": """
You are an expert software security code reviewer.

Review the provided GitHub Pull Request code changes.

Look for:
- Security vulnerabilities
- Bugs
- Poor coding practices
- Performance problems
- Maintainability problems

Return ONLY valid JSON using this exact structure:

{
    "summary": "Short summary of the review",
    "score": 0,
    "issues": [
        {
            "file": "filename",
            "line": 1,
            "severity": "HIGH",
            "category": "Security",
            "problem": "Description of the problem",
            "suggestion": "How to fix it",
            "code_snippet": "Relevant code from the patch"
        }
    ]
}

Rules:

1. The score must be between 0 and 10.
2. The severity must be one of:
   LOW, MEDIUM, HIGH, CRITICAL.
3. The category must be one of:
   Bug, Security, Performance, Code Style, Code Quality.
4. For every issue, include the relevant code in the
   "code_snippet" field.
5. The code_snippet must come directly from the provided
   Pull Request patch.
6. Include the smallest relevant code section necessary
   to demonstrate the problem.
7. The line number should correspond to the changed code
   where possible.
8. If there are no problems, return an empty issues array.
9. Do not include Markdown.
10. Return valid JSON only.
"""
                },
                {
                    "role": "user",
                    "content": code_for_review
                }
            ],
            response_format={"type": "json_object"}
        )

        logger.info("Groq request completed.")

                # 8. Parse AI response
        ai_response = response.choices[0].message.content

        parsed_review = json.loads(ai_response)

        logger.info(
            "Groq AI review parsed successfully for Review ID %s.",
            review.id,
        )
                        # 9. Save Review to database
        review.status = "completed"
        review.summary = parsed_review.get(
            "summary",
            "No summary provided."
        )
        review.score = parsed_review.get("score")
        review.completed_at = timezone.now()
        review.save()

        logger.info(
            f"Review saved to database. Review ID: {review.id}"
        )
                # 10. Save findings

        for issue in parsed_review.get("issues", []):

            severity = issue.get(
                "severity",
                "medium"
            ).lower()

            category = issue.get(
                "category",
                "quality"
            ).lower()

            # Make sure severity matches Django choices
            valid_severities = {
                "low",
                "medium",
                "high",
                "critical",
            }

            if severity not in valid_severities:
                severity = "medium"

            # Make sure category matches Django choices
            valid_categories = {
                "bug",
                "security",
                "performance",
                "style",
                "quality",
            }

            if category not in valid_categories:
                category = "quality"

            Finding.objects.create(
                review=review,
                file_path=issue.get(
                    "file",
                    "Unknown"
                ),
                line_number=issue.get(
                    "line"
                ),
                severity=severity,
                category=category,
                title=issue.get(
                    "problem",
                    "Issue detected"
                ),
                description=issue.get(
                    "problem",
                    "No description provided."
                ),
                suggestion=issue.get(
                    "suggestion",
                    ""
                ),
                code_snippet=issue.get(
                    "code_snippet",
                    ""
                ),
            )

            logger.info(
                f"Finding saved: "
                f"{severity.upper()} - "
                f"{issue.get('problem')}"
            )





        # 11. Build GitHub PR comment
        logger.info("Preparing GitHub PR comment...")

        comment_body = "## 🤖 AI Code Review\n\n"

        comment_body += (
            f"**Score:** "
            f"{parsed_review.get('score', 'N/A')}/10\n\n"
        )

        comment_body += (
            f"**Summary:** "
            f"{parsed_review.get('summary', 'No summary provided.')}\n\n"
        )

        issues = parsed_review.get("issues", [])

        if not issues:
            comment_body += "### ✅ No issues found\n\n"
            comment_body += (
                "The AI reviewer did not detect any problems "
                "in the changed code."
            )

        else:
            comment_body += (
                f"### Findings ({len(issues)})\n\n"
            )

            for issue in issues:

                severity = issue.get(
                    "severity",
                    "MEDIUM"
                ).upper()

                if severity == "CRITICAL":
                    icon = "🚨"
                elif severity == "HIGH":
                    icon = "🔴"
                elif severity == "MEDIUM":
                    icon = "🟠"
                else:
                    icon = "🟢"

                comment_body += (
                    f"{icon} **{severity} — "
                    f"{issue.get('problem', 'Issue detected')}**\n\n"
                )

                comment_body += (
                    f"- **File:** "
                    f"`{issue.get('file', 'Unknown')}`\n"
                )

                comment_body += (
                    f"- **Line:** "
                    f"{issue.get('line', 'N/A')}\n"
                )

                comment_body += (
                    f"- **Category:** "
                    f"{issue.get('category', 'General')}\n"
                )

                if issue.get("code_snippet"):
                    comment_body += (
                        "\n**Code:**\n"
                        "```python\n"
                        f"{issue.get('code_snippet')}\n"
                        "```\n"
                    )

                comment_body += (
                    f"\n**Suggestion:** "
                    f"{issue.get('suggestion', 'Review this code.')}\n\n"
                )

        comment_body += (
            "\n---\n"
            "*Automated review generated by AI Code Reviewer.*"
        )

        logger.info("GitHub comment prepared.")

        # 12. Post comment to GitHub
        try:
            logger.info(
                f"Posting AI review comment to PR "
                f"#{github_pr.number}..."
            )

            github_comment = github_pr.create_issue_comment(
                comment_body
            )

            logger.info(
                "GitHub comment created successfully!"
            )

            logger.info(
                f"Comment ID: {github_comment.id}"
            )

            logger.info(
                f"Comment URL: {github_comment.html_url}"
            )

        except Exception:
            logger.exception(
                "Failed to post AI review comment to PR #%s.",
                github_pr.number,
            )

            logger.info(
                "The AI review was still saved successfully "
                "to the database."
            )

        logger.debug(
            "AI review summary generated for Review ID %s.",
            review.id,
        )

        logger.info(
            "AI review completed for Review ID %s with score %s/10.",
            review.id,
            parsed_review.get("score"),
        )

        logger.info(
            "AI review found %s issue(s) for Review ID %s.",
            len(parsed_review.get("issues", [])),
            review.id,
        )

        return {
            "pull_request_id": pull_request.id,
            "repository": github_repo.full_name,
            "github_pr_number": github_pr.number,
            "commit_sha": commit_sha,
            "title": github_pr.title,
            "source_branch": github_pr.head.ref,
            "target_branch": github_pr.base.ref,
            "changed_files": len(changed_files),
            "review_id": review.id,
            "review": parsed_review,
            "status": "ai_review_completed",
}
    except PullRequest.DoesNotExist:
        logger.info(
            f"[ERROR] PullRequest {pull_request_id} does not exist"
        )

        return {
            "pull_request_id": pull_request_id,
            "status": "failed",
            "error": "PullRequest not found",
        }

    except Exception:
        logger.exception(
            "Manual review failed for PullRequest ID %s.",
            pull_request_id,
        )

        if "review" in locals() and review:
            review.status = "failed"
            review.completed_at = timezone.now()
            review.save(
                update_fields=[
                    "status",
                    "completed_at",
                ]
            )

            logger.info(
                f"Review ID {review.id} marked as failed."
            )

        return {
            "pull_request_id": pull_request_id,
            "commit_sha": commit_sha,
            "review_id": (
                review.id
                if "review" in locals() and review
                else None
            ),
            "status": "failed",
            "error": "AI code review failed. Please try again.",
        }