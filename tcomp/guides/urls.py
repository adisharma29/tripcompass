from django.urls import path
from .views import (
    DestinationListView,
    DestinationDetailView,
    ExperienceListView,
    ExperienceDetailView,
    GeoJSONView,
    NearbyPlaceListView,
)

urlpatterns = [
    path('destinations/', DestinationListView.as_view(), name='destination-list'),
    path('destinations/<slug:slug>/', DestinationDetailView.as_view(), name='destination-detail'),
    path('destinations/<slug:slug>/experiences/', ExperienceListView.as_view(), name='experience-list'),
    path('destinations/<slug:slug>/experiences/<str:code>/', ExperienceDetailView.as_view(), name='experience-detail'),
    path('destinations/<slug:slug>/geojson/', GeoJSONView.as_view(), name='destination-geojson'),
    path('destinations/<slug:slug>/nearby-places/<str:code>/', NearbyPlaceListView.as_view(), name='nearby-places'),
]
