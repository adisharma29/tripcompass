from django.contrib.gis.db import models
from django.utils.text import slugify


class Destination(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    tagline = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)

    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='children'
    )
    # State is the minimum location anchor; city/district provide optional specificity
    state = models.ForeignKey(
        'location.State', on_delete=models.PROTECT, related_name='destinations'
    )
    district = models.ForeignKey(
        'location.District', on_delete=models.SET_NULL, null=True, blank=True
    )
    city = models.ForeignKey(
        'location.City', on_delete=models.SET_NULL, null=True, blank=True
    )

    # Map defaults
    center_lng = models.FloatField(default=0)
    center_lat = models.FloatField(default=0)
    default_zoom = models.FloatField(default=13)
    default_pitch = models.FloatField(default=0)
    default_bearing = models.FloatField(default=0)
    mapbox_style = models.CharField(max_length=200, blank=True)

    # Map bounds (SW corner → NE corner)
    bounds_sw_lng = models.FloatField(null=True, blank=True)
    bounds_sw_lat = models.FloatField(null=True, blank=True)
    bounds_ne_lng = models.FloatField(null=True, blank=True)
    bounds_ne_lat = models.FloatField(null=True, blank=True)

    # Theme
    background_color = models.CharField(max_length=9, default='#FFE9CF')
    text_color = models.CharField(max_length=9, default='#434431')

    is_published = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['is_published', 'sort_order', 'name']),
        ]

    def __str__(self):
        return self.name


class Mood(models.Model):
    destination = models.ForeignKey(
        Destination, on_delete=models.CASCADE, related_name='moods'
    )
    slug = models.SlugField(max_length=100)
    name = models.CharField(max_length=100)
    tagline = models.CharField(max_length=300, blank=True)
    tip = models.TextField(blank=True)
    support_line = models.CharField(max_length=300, blank=True)
    color = models.CharField(max_length=9, default='#000000')
    card_background = models.CharField(max_length=60, blank=True, help_text='CSS color for mood card background, e.g. rgba(61,122,104,0.25)')
    illustration = models.ImageField(upload_to='mood_illustrations/', blank=True)
    is_special = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('destination', 'slug')
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.destination.name})"


class Experience(models.Model):
    EFFORT_CHOICES = [
        ('easy', 'Easy'),
        ('gentle', 'Gentle'),
        ('fair', 'Fair'),
        ('moderate', 'Moderate'),
        ('challenging', 'Challenging'),
    ]

    destination = models.ForeignKey(
        Destination, on_delete=models.CASCADE, related_name='experiences'
    )
    mood = models.ForeignKey(
        Mood, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='experiences'
    )

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200, blank=True)
    slug = models.SlugField(max_length=200, blank=True)

    tagline = models.CharField(max_length=500, blank=True)
    experience_type = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=9, default='#000000')

    duration = models.CharField(max_length=100, blank=True)
    effort = models.CharField(max_length=20, choices=EFFORT_CHOICES, blank=True)
    distance = models.CharField(max_length=50, blank=True)
    best_time = models.CharField(max_length=100, blank=True)

    about = models.JSONField(default=list, blank=True)
    what_you_get = models.JSONField(default=list, blank=True)
    why_we_chose_this = models.TextField(blank=True)
    golden_way = models.TextField(blank=True)
    breakdown = models.JSONField(default=dict, blank=True)

    spreadsheet_data = models.JSONField(default=dict, blank=True)

    center_lng = models.FloatField(default=0)
    center_lat = models.FloatField(default=0)
    zoom = models.FloatField(default=14)

    related_experiences = models.ManyToManyField('self', blank=True, symmetrical=True)

    sort_order = models.IntegerField(default=0)
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['destination', 'is_published', 'sort_order']),
        ]

    def __str__(self):
        return f"{self.code}: {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if not self.display_name:
            self.display_name = self.name
        super().save(*args, **kwargs)


class ExperienceImage(models.Model):
    experience = models.ForeignKey(
        Experience, on_delete=models.CASCADE, related_name='images'
    )
    image = models.ImageField(upload_to='experience_images/')
    alt_text = models.CharField(max_length=300, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.experience.code} - Image {self.sort_order}"


class GeoFeature(models.Model):
    FEATURE_TYPE_CHOICES = [
        ('route', 'Route'),
        ('zone', 'Zone'),
        ('poi', 'Point of Interest'),
    ]

    destination = models.ForeignKey(
        Destination, on_delete=models.CASCADE, related_name='geo_features'
    )
    experience = models.ForeignKey(
        Experience, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='geo_features'
    )

    name = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    feature_type = models.CharField(max_length=10, choices=FEATURE_TYPE_CHOICES)

    route = models.LineStringField(srid=4326, null=True, blank=True)
    zone = models.PolygonField(srid=4326, null=True, blank=True)
    point = models.PointField(srid=4326, null=True, blank=True)

    # Zone styling
    color = models.CharField(max_length=9, blank=True)
    fill_opacity = models.FloatField(null=True, blank=True)

    # POI metadata
    category = models.CharField(max_length=50, blank=True)
    poi_type = models.CharField(max_length=50, blank=True)
    folder = models.CharField(max_length=200, blank=True)
    folder_category = models.CharField(max_length=100, blank=True)

    kml_source = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['feature_type', 'name']

    def __str__(self):
        return f"{self.get_feature_type_display()}: {self.name}"


class NearbyPlace(models.Model):
    destination = models.ForeignKey(
        Destination, on_delete=models.CASCADE, related_name='nearby_places'
    )
    experience = models.ForeignKey(
        Experience, on_delete=models.CASCADE, related_name='nearby_places'
    )

    google_place_id = models.CharField(max_length=200)
    name = models.CharField(max_length=300)
    place_type = models.CharField(max_length=50, blank=True)
    primary_type = models.CharField(max_length=100, blank=True)
    rating = models.FloatField(null=True, blank=True)
    user_rating_count = models.IntegerField(null=True, blank=True)
    address = models.TextField(blank=True)
    location = models.PointField(srid=4326)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('experience', 'google_place_id')
        ordering = ['-rating']

    def __str__(self):
        return f"{self.name} (near {self.experience.code})"
