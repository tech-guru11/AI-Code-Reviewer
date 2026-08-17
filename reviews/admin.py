from django.contrib import admin
from .models import Repository, PullRequest, Review, Finding

admin.site.register(Repository)
admin.site.register(PullRequest)
admin.site.register(Review)
admin.site.register(Finding)