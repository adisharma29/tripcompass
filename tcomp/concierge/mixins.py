from django.shortcuts import get_object_or_404

from .models import Hotel


class HotelScopedMixin:
    """Ensures all queries are filtered to the hotel in the URL.
    Prevents cross-hotel data leakage."""

    def get_hotel(self):
        if not hasattr(self, '_hotel'):
            self._hotel = get_object_or_404(
                Hotel, slug=self.kwargs['hotel_slug'], is_active=True
            )
        return self._hotel

    def get_queryset(self):
        qs = super().get_queryset()
        hotel = self.get_hotel()
        if hasattr(qs.model, 'hotel'):
            return qs.filter(hotel=hotel)
        if hasattr(qs.model, 'department'):
            return qs.filter(department__hotel=hotel)
        return qs

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        model = serializer.Meta.model
        has_hotel_fk = any(field.name == 'hotel' for field in model._meta.fields)
        if has_hotel_fk:
            serializer.save(hotel=hotel)
        else:
            serializer.save()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['hotel'] = self.get_hotel()
        return ctx
