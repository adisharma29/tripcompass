from rest_framework import serializers
from .models import Destination, Mood, Experience, ExperienceImage, NearbyPlace


class MoodSerializer(serializers.ModelSerializer):
    experience_codes = serializers.SerializerMethodField()

    class Meta:
        model = Mood
        fields = [
            'slug', 'name', 'tagline', 'tip', 'support_line',
            'color', 'card_background', 'illustration', 'is_special', 'sort_order',
            'experience_codes',
        ]

    def get_experience_codes(self, obj):
        # Uses prefetched published_experiences (list) from DestinationDetailView
        if hasattr(obj, 'published_experiences'):
            return [e.code for e in obj.published_experiences]
        return list(
            obj.experiences.filter(is_published=True)
            .order_by('sort_order')
            .values_list('code', flat=True)
        )


class DestinationListSerializer(serializers.ModelSerializer):
    mood_count = serializers.IntegerField(read_only=True)
    experience_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Destination
        fields = [
            'slug', 'name', 'tagline',
            'center_lng', 'center_lat',
            'background_color', 'text_color',
            'mood_count', 'experience_count',
        ]


class DestinationDetailSerializer(serializers.ModelSerializer):
    moods = MoodSerializer(many=True, read_only=True)

    class Meta:
        model = Destination
        fields = [
            'slug', 'name', 'tagline', 'description',
            'center_lng', 'center_lat', 'default_zoom',
            'default_pitch', 'default_bearing', 'mapbox_style',
            'bounds_sw_lng', 'bounds_sw_lat',
            'bounds_ne_lng', 'bounds_ne_lat',
            'background_color', 'text_color',
            'moods',
        ]


class ExperienceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExperienceImage
        fields = ['image', 'alt_text', 'sort_order']


class ExperienceListSerializer(serializers.ModelSerializer):
    mood_slug = serializers.CharField(source='mood.slug', read_only=True, default='')
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = Experience
        fields = [
            'code', 'name', 'display_name', 'tagline',
            'experience_type', 'color',
            'duration', 'effort', 'distance',
            'mood_slug', 'thumbnail',
        ]

    def get_thumbnail(self, obj):
        first_image = obj.images.first()
        if first_image and first_image.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(first_image.image.url)
            return first_image.image.url
        return None


class ExperienceDetailSerializer(serializers.ModelSerializer):
    images = ExperienceImageSerializer(many=True, read_only=True)
    mood_slug = serializers.CharField(source='mood.slug', read_only=True, default='')
    related_experience_codes = serializers.SerializerMethodField()

    class Meta:
        model = Experience
        fields = [
            'code', 'name', 'display_name', 'slug', 'tagline',
            'experience_type', 'color',
            'duration', 'effort', 'distance', 'best_time',
            'about', 'what_you_get', 'why_we_chose_this',
            'golden_way', 'breakdown',
            'center_lng', 'center_lat', 'zoom',
            'mood_slug', 'images', 'related_experience_codes',
        ]

    def get_related_experience_codes(self, obj):
        return list(
            obj.related_experiences.filter(is_published=True)
            .values_list('code', flat=True)
        )


class NearbyPlaceSerializer(serializers.ModelSerializer):
    lat = serializers.SerializerMethodField()
    lng = serializers.SerializerMethodField()

    class Meta:
        model = NearbyPlace
        fields = [
            'google_place_id', 'name', 'place_type', 'primary_type',
            'rating', 'user_rating_count', 'address',
            'lat', 'lng',
        ]

    def get_lat(self, obj):
        return obj.location.y if obj.location else None

    def get_lng(self, obj):
        return obj.location.x if obj.location else None
