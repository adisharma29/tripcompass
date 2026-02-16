import json

from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .models import Destination, Experience, GeoFeature, NearbyPlace
from .serializers import (
    DestinationListSerializer,
    DestinationDetailSerializer,
    ExperienceListSerializer,
    ExperienceDetailSerializer,
    NearbyPlaceSerializer,
)


class DestinationListView(generics.ListAPIView):
    serializer_class = DestinationListSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return Destination.objects.filter(is_published=True).annotate(
            mood_count=Count('moods', distinct=True),
            experience_count=Count(
                'experiences',
                filter=Q(experiences__is_published=True),
                distinct=True,
            ),
        ).order_by('sort_order', 'name')


class DestinationDetailView(generics.RetrieveAPIView):
    serializer_class = DestinationDetailSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        return Destination.objects.filter(is_published=True).prefetch_related(
            'moods',
            Prefetch(
                'moods__experiences',
                queryset=Experience.objects.filter(is_published=True).order_by('sort_order'),
                to_attr='published_experiences',
            ),
        )


class ExperienceListView(generics.ListAPIView):
    serializer_class = ExperienceListSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        slug = self.kwargs['slug']
        return Experience.objects.filter(
            destination__slug=slug,
            is_published=True,
        ).select_related('mood').prefetch_related('images')


class ExperienceDetailView(generics.RetrieveAPIView):
    serializer_class = ExperienceDetailSerializer
    permission_classes = [AllowAny]
    lookup_field = 'code'

    def get_queryset(self):
        slug = self.kwargs['slug']
        return Experience.objects.filter(
            destination__slug=slug,
            is_published=True,
        ).select_related('mood').prefetch_related('images', 'related_experiences')


class GeoJSONView(APIView):
    permission_classes = [AllowAny]

    @method_decorator(cache_page(3600))
    def get(self, request, slug):
        features = GeoFeature.objects.filter(
            destination__slug=slug,
            destination__is_published=True,
        ).filter(
            Q(experience__isnull=True) | Q(experience__is_published=True)
        ).select_related('experience')

        geojson_features = []
        for feat in features:
            if feat.feature_type == 'route' and feat.route:
                geometry = json.loads(feat.route.geojson)
            elif feat.feature_type == 'zone' and feat.zone:
                geometry = json.loads(feat.zone.geojson)
            elif feat.feature_type == 'poi' and feat.point:
                geometry = json.loads(feat.point.geojson)
            else:
                continue

            properties = {
                'name': feat.name,
                'description': feat.description,
                'feature_type': feat.feature_type,
                'category': feat.category,
            }

            if feat.experience:
                properties['experience_code'] = feat.experience.code

            if feat.color:
                properties['color'] = feat.color
            if feat.fill_opacity is not None:
                properties['fillOpacity'] = feat.fill_opacity
            if feat.poi_type:
                properties['poiType'] = feat.poi_type
            if feat.folder:
                properties['folder'] = feat.folder
            if feat.folder_category:
                properties['folderCategory'] = feat.folder_category

            geojson_features.append({
                'type': 'Feature',
                'geometry': geometry,
                'properties': properties,
            })

        return JsonResponse({
            'type': 'FeatureCollection',
            'features': geojson_features,
        })


class NearbyPlaceListView(generics.ListAPIView):
    serializer_class = NearbyPlaceSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        slug = self.kwargs['slug']
        code = self.kwargs['code']
        return NearbyPlace.objects.filter(
            destination__slug=slug,
            destination__is_published=True,
            experience__code=code,
            experience__is_published=True,
        )
