import uuid

from django.contrib.gis.db import models
from django.db import IntegrityError
from django.utils.text import slugify


def _make_slug(name, state_name, max_length):
    """Build a slug with uuid suffix, truncated to fit max_length."""
    suffix = uuid.uuid4().hex[:7]
    base = slugify(f"{name}-{state_name}")[:max_length - 8]
    return f"{base}-{suffix}"


class Country(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=3, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Countries"
        ordering = ['name']

    def __str__(self):
        return self.name


class State(models.Model):
    name = models.CharField(max_length=100)
    country = models.ForeignKey(Country, on_delete=models.CASCADE, related_name="states")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("name", "country")
        ordering = ['name']

    def __str__(self):
        return f"{self.name}, {self.country.name}"


class District(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="districts")
    poly = models.PolygonField(srid=4326, null=True, blank=True)
    multipoly = models.MultiPolygonField(srid=4326, null=True, blank=True)
    is_multipoly = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("name", "state")
        ordering = ['name']

    def __str__(self):
        return f"{self.name}, {self.state.name}"

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            for attempt in range(5):
                self.slug = _make_slug(self.name, self.state.name, 200)
                try:
                    return super().save(*args, **kwargs)
                except IntegrityError as e:
                    if 'slug' not in str(e).lower():
                        raise
                    if attempt == 4:
                        raise
            return
        super().save(*args, **kwargs)


class City(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=250, unique=True, blank=True)
    district = models.ForeignKey(
        District, on_delete=models.CASCADE, related_name="cities",
        null=True, blank=True
    )
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="cities")
    population = models.IntegerField(null=True, blank=True)
    poly = models.PolygonField(srid=4326, null=True, blank=True)
    multipoly = models.MultiPolygonField(srid=4326, null=True, blank=True)
    is_multipoly = models.BooleanField(default=False)
    centroid = models.PointField(srid=4326, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Cities"
        unique_together = ("name", "state")
        ordering = ['name']

    def __str__(self):
        return f"{self.name}, {self.state.name}"

    def save(self, *args, **kwargs):
        if (self.poly or self.multipoly) and not self.centroid:
            geom = self.multipoly if self.is_multipoly else self.poly
            if geom:
                self.centroid = geom.centroid

        if not self.slug and self.name:
            for attempt in range(5):
                self.slug = _make_slug(self.name, self.state.name, 250)
                try:
                    return super().save(*args, **kwargs)
                except IntegrityError as e:
                    if 'slug' not in str(e).lower():
                        raise
                    if attempt == 4:
                        raise
            return
        super().save(*args, **kwargs)
