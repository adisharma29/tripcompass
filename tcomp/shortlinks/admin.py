from django.contrib import admin

from .models import ShortLink


@admin.register(ShortLink)
class ShortLinkAdmin(admin.ModelAdmin):
    list_display = ('code', 'target_url_short', 'click_count', 'is_active', 'expires_at', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('code', 'target_url')
    readonly_fields = ('code', 'click_count', 'created_at')

    def target_url_short(self, obj):
        return obj.target_url[:80]

    target_url_short.short_description = 'Target URL'
