from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin
from .models import (
    Destination, Mood, Experience, ExperienceImage,
    GeoFeature, NearbyPlace,
)


class MoodInline(admin.TabularInline):
    model = Mood
    extra = 0
    fields = ['slug', 'name', 'color', 'is_special', 'sort_order']


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_published', 'mood_count', 'experience_count']
    list_filter = ['is_published']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    inlines = [MoodInline]

    def mood_count(self, obj):
        return obj.moods.count()
    mood_count.short_description = 'Moods'

    def experience_count(self, obj):
        return obj.experiences.count()
    experience_count.short_description = 'Experiences'


@admin.register(Mood)
class MoodAdmin(admin.ModelAdmin):
    list_display = ['name', 'destination', 'slug', 'color', 'is_special', 'sort_order']
    list_filter = ['destination', 'is_special']
    search_fields = ['name']


class ExperienceImageInline(admin.TabularInline):
    model = ExperienceImage
    extra = 0
    fields = ['image', 'alt_text', 'sort_order']


@admin.register(Experience)
class ExperienceAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'destination', 'mood', 'effort', 'is_published']
    list_filter = ['destination', 'mood', 'effort', 'is_published']
    search_fields = ['code', 'name']
    inlines = [ExperienceImageInline]

    fieldsets = (
        ('Identity', {
            'fields': ('code', 'name', 'display_name', 'slug', 'destination', 'mood')
        }),
        ('Summary', {
            'fields': ('tagline', 'experience_type', 'color', 'duration', 'effort', 'distance', 'best_time')
        }),
        ('Content', {
            'fields': ('about', 'what_you_get', 'why_we_chose_this', 'golden_way', 'breakdown')
        }),
        ('Map', {
            'fields': ('center_lng', 'center_lat', 'zoom')
        }),
        ('Metadata', {
            'fields': ('spreadsheet_data', 'related_experiences', 'sort_order', 'is_published'),
            'classes': ('collapse',),
        }),
    )
    filter_horizontal = ['related_experiences']


@admin.register(ExperienceImage)
class ExperienceImageAdmin(admin.ModelAdmin):
    list_display = ['experience', 'alt_text', 'sort_order']
    list_filter = ['experience__destination']


@admin.register(GeoFeature)
class GeoFeatureAdmin(GISModelAdmin):
    list_display = ['name', 'feature_type', 'destination', 'experience', 'category']
    list_filter = ['feature_type', 'destination', 'category']
    search_fields = ['name']


@admin.register(NearbyPlace)
class NearbyPlaceAdmin(GISModelAdmin):
    list_display = ['name', 'place_type', 'primary_type', 'rating', 'experience']
    list_filter = ['place_type', 'destination']
    search_fields = ['name', 'google_place_id']
