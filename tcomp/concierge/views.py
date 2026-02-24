import hashlib
import hmac
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Count, Max, Prefetch
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .filters import ServiceRequestFilter
from .mixins import HotelScopedMixin
from .models import (
    BookingEmailTemplate, ContentStatus, Department, Event, Experience,
    ExperienceImage, GuestStay, Hotel, HotelInfoSection, HotelMembership,
    Notification, NotificationRoute, PushSubscription, QRCode,
    ServiceRequest, RequestActivity,
)
from .permissions import (
    CanAccessRequestObject, CanAccessRequestObjectByLookup,
    IsActiveGuest, IsAdminOrAbove, IsStaffOrAbove,
    IsStayOwner, IsSuperAdmin,
)
from .serializers import (
    BookingEmailTemplateSerializer, DashboardStatsSerializer,
    DepartmentPublicSerializer, DepartmentSerializer,
    EventPublicSerializer, EventSerializer, ExperienceImageSerializer,
    ExperiencePublicSerializer, ExperienceSerializer, TopDealSerializer,
    GuestStaySerializer, GuestStayUpdateSerializer,
    HotelInfoSectionSerializer, HotelPublicSerializer,
    HotelSettingsSerializer, MemberCreateSerializer,
    MemberSerializer, NotificationRouteSerializer, NotificationSerializer,
    PushSubscriptionSerializer, QRCodeSerializer,
    ServiceRequestCreateSerializer, ServiceRequestDetailSerializer,
    ServiceRequestListSerializer, ServiceRequestUpdateSerializer,
)
from .notifications import NotificationEvent, dispatch_notification
from .services import (
    check_room_rate_limit, check_stay_rate_limit,
    compute_response_due_at, generate_qr,
    get_dashboard_stats, handle_wa_delivery_event,
    is_department_after_hours,
    publish_request_event, stream_request_events,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------

class HotelPublicDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = HotelPublicSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'hotel_slug'
    queryset = Hotel.objects.filter(is_active=True).prefetch_related(
        Prefetch(
            'departments',
            queryset=Department.objects.filter(status=ContentStatus.PUBLISHED, is_ops=False),
        ),
        Prefetch(
            'departments__experiences',
            queryset=Experience.objects.filter(status=ContentStatus.PUBLISHED),
        ),
        'departments__experiences__gallery_images',
        Prefetch(
            'info_sections',
            queryset=HotelInfoSection.objects.filter(is_visible=True).order_by('display_order', 'id'),
        ),
    )


class DepartmentPublicList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = DepartmentPublicSerializer

    def get_queryset(self):
        return Department.objects.filter(
            hotel__slug=self.kwargs['hotel_slug'],
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
            is_ops=False,
        ).prefetch_related(
            Prefetch(
                'experiences',
                queryset=Experience.objects.filter(status=ContentStatus.PUBLISHED),
            ),
            'experiences__gallery_images',
        )


class DepartmentPublicDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = DepartmentPublicSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'dept_slug'

    def get_queryset(self):
        return Department.objects.filter(
            hotel__slug=self.kwargs['hotel_slug'],
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
            is_ops=False,
        ).prefetch_related(
            Prefetch(
                'experiences',
                queryset=Experience.objects.filter(status=ContentStatus.PUBLISHED),
            ),
            'experiences__gallery_images',
        )


class ExperiencePublicDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = ExperiencePublicSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'exp_slug'

    def get_queryset(self):
        return Experience.objects.filter(
            department__hotel__slug=self.kwargs['hotel_slug'],
            department__slug=self.kwargs['dept_slug'],
            department__hotel__is_active=True,
            department__is_ops=False,
            status=ContentStatus.PUBLISHED,
        ).prefetch_related('gallery_images')


class EventPublicList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = EventPublicSerializer

    def get_queryset(self):
        now = timezone.now()
        qs = Event.objects.filter(
            hotel__slug=self.kwargs['hotel_slug'],
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
        ).select_related(
            'hotel', 'department', 'experience__department', 'hotel__fallback_department',
        )

        if self.request.query_params.get('featured') == 'true':
            qs = qs.filter(is_featured=True)

        # Default upcoming filter: exclude ended one-time events.
        # Pass ?all=true to bypass (for past events UI if ever needed).
        if self.request.query_params.get('all') != 'true':
            qs = qs.exclude(
                is_recurring=False,
                event_end__isnull=False,
                event_end__lt=now,
            )

        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        # Sort by next_occurrence so recurring events that started long ago
        # appear in chronological order alongside one-time events.
        far_future = timezone.now() + timedelta(days=3650)
        events = sorted(
            queryset,
            key=lambda e: e.get_next_occurrence() or far_future,
        )

        page = self.paginate_queryset(events)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(events, many=True)
        return Response(serializer.data)


class EventPublicDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = EventPublicSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'event_slug'

    def get_queryset(self):
        return Event.objects.filter(
            hotel__slug=self.kwargs['hotel_slug'],
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
        ).select_related(
            'hotel', 'department', 'experience__department', 'hotel__fallback_department',
        )


class TopDealsList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = TopDealSerializer
    pagination_class = None

    def get_queryset(self):
        now = timezone.now()
        return Experience.objects.filter(
            department__hotel__slug=self.kwargs['hotel_slug'],
            department__hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
            is_top_deal=True,
            department__status=ContentStatus.PUBLISHED,
            department__is_ops=False,
        ).filter(
            models.Q(deal_ends_at__isnull=True) | models.Q(deal_ends_at__gt=now),
        ).select_related(
            'department',
        ).prefetch_related(
            'gallery_images',
        ).order_by(
            models.F('deal_ends_at').asc(nulls_last=True), 'name',
        )


# ---------------------------------------------------------------------------
# Guest views
# ---------------------------------------------------------------------------

class ServiceRequestCreate(HotelScopedMixin, generics.CreateAPIView):
    permission_classes = [IsActiveGuest]
    serializer_class = ServiceRequestCreateSerializer
    queryset = ServiceRequest.objects.none()

    def create(self, request, *args, **kwargs):
        hotel = self.get_hotel()
        user = request.user

        # Get active stay
        now = timezone.now()
        stay = GuestStay.objects.filter(
            guest=user, hotel=hotel, is_active=True, expires_at__gt=now,
        ).order_by('-created_at').first()

        if not stay:
            return Response(
                {'detail': 'No active stay found.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not stay.room_number:
            return Response(
                {'detail': 'Please set your room number first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Rate limits
        if not check_stay_rate_limit(stay):
            return Response(
                {'detail': 'Too many requests. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if not check_room_rate_limit(hotel, stay.room_number):
            return Response(
                {'detail': 'Too many requests from this room. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Handle guest_name
        guest_name = serializer.validated_data.pop('guest_name', '')
        if guest_name and not user.first_name:
            parts = guest_name.split(' ', 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ''
            user.save(update_fields=['first_name', 'last_name'])

        department = serializer.validated_data.get('department') or (
            serializer.validated_data.get('experience').department
            if serializer.validated_data.get('experience') else None
        )
        after_hours = is_department_after_hours(department) if department else False
        response_due = compute_response_due_at(hotel)

        req = serializer.save(
            hotel=hotel,
            guest_stay=stay,
            after_hours=after_hours,
            response_due_at=response_due,
        )

        # Activity log
        RequestActivity.objects.create(
            request=req,
            action=RequestActivity.Action.CREATED,
        )

        # Notifications
        dispatch_notification(NotificationEvent(
            event_type='request.created',
            hotel=hotel,
            department=req.department,
            request=req,
            event_obj=req.event,
        ))

        # SSE
        publish_request_event(hotel, 'request.created', req)

        # After-hours handling
        if req.after_hours and hotel.fallback_department:
            dispatch_notification(NotificationEvent(
                event_type='after_hours_fallback',
                hotel=hotel,
                department=hotel.fallback_department,
                request=req,
                event_obj=req.event,
                extra={'original_department_name': req.department.name},
            ))

        detail_serializer = ServiceRequestDetailSerializer(req)
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED)


class GuestStayUpdate(HotelScopedMixin, generics.UpdateAPIView):
    permission_classes = [IsActiveGuest, IsStayOwner]
    serializer_class = GuestStayUpdateSerializer
    queryset = GuestStay.objects.all()
    lookup_field = 'pk'
    lookup_url_kwarg = 'stay_id'
    http_method_names = ['patch']


class MyStaysList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = GuestStaySerializer

    def get_queryset(self):
        return GuestStay.objects.filter(guest=self.request.user).order_by('-created_at')


class MyRequestsList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ServiceRequestListSerializer

    def list(self, request, *args, **kwargs):
        if request.user.user_type == 'GUEST' and not request.query_params.get('hotel'):
            return Response(
                {'detail': 'hotel query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = ServiceRequest.objects.filter(
            guest_stay__guest=self.request.user,
        ).select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
        ).order_by('-created_at')
        hotel_slug = self.request.query_params.get('hotel')
        if hotel_slug:
            qs = qs.filter(guest_stay__hotel__slug=hotel_slug)
        return qs


# ---------------------------------------------------------------------------
# Staff views
# ---------------------------------------------------------------------------

class ServiceRequestList(HotelScopedMixin, generics.ListAPIView):
    queryset = ServiceRequest.objects.all()
    permission_classes = [IsStaffOrAbove]
    serializer_class = ServiceRequestListSerializer
    filterset_class = ServiceRequestFilter
    ordering_fields = ['created_at', 'updated_at', 'status']

    def get_queryset(self):
        qs = super().get_queryset().select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
        )
        membership = getattr(self.request, 'membership', None)
        if membership and membership.role == HotelMembership.Role.STAFF:
            qs = qs.filter(department=membership.department)
        return qs


class ServiceRequestDetail(HotelScopedMixin, generics.RetrieveUpdateAPIView):
    permission_classes = [IsStaffOrAbove, CanAccessRequestObject]
    http_method_names = ['get', 'patch']

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return ServiceRequestUpdateSerializer
        return ServiceRequestDetailSerializer

    def get_queryset(self):
        return ServiceRequest.objects.filter(
            hotel=self.get_hotel(),
        ).select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
        ).prefetch_related('activities__actor')

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # Log VIEWED event (does NOT change status)
        RequestActivity.objects.create(
            request=instance,
            actor=request.user,
            action=RequestActivity.Action.VIEWED,
        )
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def perform_update(self, serializer):
        old_status = serializer.instance.status
        new_status = serializer.validated_data.get('status')

        instance = serializer.save()

        if new_status:
            if new_status == ServiceRequest.Status.CONFIRMED:
                instance.confirmed_at = timezone.now()
                instance.save(update_fields=['confirmed_at'])

            action = (
                RequestActivity.Action.CONFIRMED
                if new_status == ServiceRequest.Status.CONFIRMED
                else RequestActivity.Action.CLOSED
            )
            RequestActivity.objects.create(
                request=instance,
                actor=self.request.user,
                action=action,
                details={
                    'status_from': old_status,
                    'status_to': new_status,
                },
            )

            publish_request_event(instance.hotel, 'request.updated', instance)

        # Staff notes activity
        if serializer.validated_data.get('staff_notes'):
            RequestActivity.objects.create(
                request=instance,
                actor=self.request.user,
                action=RequestActivity.Action.NOTE_ADDED,
                details={
                    'note_length': len(serializer.validated_data['staff_notes']),
                },
            )


class ServiceRequestDetailByPublicId(HotelScopedMixin, generics.RetrieveAPIView):
    permission_classes = [IsStaffOrAbove, CanAccessRequestObject]
    serializer_class = ServiceRequestDetailSerializer
    lookup_field = 'public_id'

    def get_queryset(self):
        return ServiceRequest.objects.filter(
            hotel=self.get_hotel(),
        ).select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
        ).prefetch_related('activities__actor')


class ServiceRequestAcknowledge(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove, CanAccessRequestObject]

    def post(self, request, pk, **kwargs):
        from django.db import transaction

        hotel = self.get_hotel()

        with transaction.atomic():
            try:
                req = ServiceRequest.objects.select_for_update().get(
                    pk=pk, hotel=hotel,
                )
            except ServiceRequest.DoesNotExist:
                return Response(status=status.HTTP_404_NOT_FOUND)

            # Check object permission
            self.check_object_permissions(request, req)

            if req.status == ServiceRequest.Status.ACKNOWLEDGED:
                # Idempotent — already acknowledged
                return Response({'detail': 'Already acknowledged.'})

            if req.status != ServiceRequest.Status.CREATED:
                return Response(
                    {'detail': f'Cannot acknowledge request in {req.status} state.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            req.status = ServiceRequest.Status.ACKNOWLEDGED
            req.acknowledged_at = timezone.now()
            req.save(update_fields=['status', 'acknowledged_at', 'updated_at'])

            RequestActivity.objects.create(
                request=req,
                actor=request.user,
                action=RequestActivity.Action.ACKNOWLEDGED,
                details={
                    'status_from': 'CREATED',
                    'status_to': 'ACKNOWLEDGED',
                },
            )

        publish_request_event(hotel, 'request.updated', req)

        return Response(ServiceRequestDetailSerializer(req).data)


class ServiceRequestTakeOwnership(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove, CanAccessRequestObject]

    def post(self, request, pk, **kwargs):
        hotel = self.get_hotel()
        try:
            req = ServiceRequest.objects.get(pk=pk, hotel=hotel)
        except ServiceRequest.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        self.check_object_permissions(request, req)

        # Idempotent — skip if already owned by this user
        if req.assigned_to_id != request.user.id:
            req.assigned_to = request.user
            req.save(update_fields=['assigned_to', 'updated_at'])

            RequestActivity.objects.create(
                request=req,
                actor=request.user,
                action=RequestActivity.Action.OWNERSHIP_TAKEN,
                details={'assigned_to_id': request.user.id},
            )

            publish_request_event(hotel, 'request.updated', req)

        return Response(ServiceRequestDetailSerializer(req).data)


class RequestNoteCreate(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove, CanAccessRequestObject]

    def post(self, request, pk, **kwargs):
        hotel = self.get_hotel()
        try:
            req = ServiceRequest.objects.get(pk=pk, hotel=hotel)
        except ServiceRequest.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        self.check_object_permissions(request, req)

        note = request.data.get('note', '').strip()
        if not note:
            return Response(
                {'detail': 'Note cannot be empty.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Append to staff_notes
        if req.staff_notes:
            req.staff_notes += f'\n\n---\n{note}'
        else:
            req.staff_notes = note
        req.save(update_fields=['staff_notes', 'updated_at'])

        RequestActivity.objects.create(
            request=req,
            actor=request.user,
            action=RequestActivity.Action.NOTE_ADDED,
            details={'note_length': len(note)},
        )

        return Response({'detail': 'Note added.'}, status=status.HTTP_201_CREATED)


class GuestStayRevoke(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove]

    def post(self, request, stay_id, **kwargs):
        hotel = self.get_hotel()
        try:
            stay = GuestStay.objects.get(pk=stay_id, hotel=hotel)
        except GuestStay.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        stay.is_active = False
        stay.save(update_fields=['is_active'])
        return Response({'detail': 'Stay revoked.'})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardStats(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove]

    def get(self, request, **kwargs):
        hotel = self.get_hotel()
        membership = getattr(request, 'membership', None)
        department = None
        if membership and membership.role == HotelMembership.Role.STAFF:
            department = membership.department

        stats = get_dashboard_stats(hotel, department)
        return Response(stats)


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

class RequestSSEStream(HotelScopedMixin, APIView):
    permission_classes = [IsStaffOrAbove]

    def perform_content_negotiation(self, request, force=False):
        """Bypass DRF content negotiation — this view returns a StreamingHttpResponse directly."""
        return (self.renderer_classes[0](), self.renderer_classes[0].media_type)

    def get(self, request, **kwargs):
        hotel = self.get_hotel()
        user = request.user

        response = StreamingHttpResponse(
            stream_request_events(hotel, user),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response


# ---------------------------------------------------------------------------
# Admin views
# ---------------------------------------------------------------------------

class DepartmentViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAdminOrAbove]
    serializer_class = DepartmentSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'dept_slug'

    def get_queryset(self):
        return Department.objects.filter(
            hotel=self.get_hotel(),
        ).prefetch_related('experiences')


class ExperienceViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAdminOrAbove]
    serializer_class = ExperienceSerializer

    def get_queryset(self):
        return Experience.objects.filter(
            department__hotel=self.get_hotel(),
            department__slug=self.kwargs.get('dept_slug'),
        ).prefetch_related('gallery_images')


class EventAdminViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    serializer_class = EventSerializer
    pagination_class = None  # Hotels have few events; avoids reorder/display_order collisions

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsStaffOrAbove()]
        return [IsAdminOrAbove()]

    def get_queryset(self):
        qs = Event.objects.filter(hotel=self.get_hotel())
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs


# ---------------------------------------------------------------------------
# Bulk reorder
# ---------------------------------------------------------------------------

class DepartmentBulkReorder(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def patch(self, request, **kwargs):
        ordered_ids = request.data.get('order', [])
        if not ordered_ids or not isinstance(ordered_ids, list):
            return Response(
                {'detail': 'order must be a non-empty list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ordered_ids) != len(set(ordered_ids)):
            return Response(
                {'detail': 'Duplicate IDs in order list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        with transaction.atomic():
            departments = Department.objects.filter(hotel=hotel, id__in=ordered_ids)
            if departments.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more IDs do not belong to this hotel.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            dept_map = {d.id: d for d in departments}
            for idx, dept_id in enumerate(ordered_ids):
                dept_map[dept_id].display_order = idx
            Department.objects.bulk_update(dept_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} departments reordered.'})


class ExperienceBulkReorder(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def patch(self, request, **kwargs):
        ordered_ids = request.data.get('order', [])
        if not ordered_ids or not isinstance(ordered_ids, list):
            return Response(
                {'detail': 'order must be a non-empty list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ordered_ids) != len(set(ordered_ids)):
            return Response(
                {'detail': 'Duplicate IDs in order list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        dept_slug = self.kwargs['dept_slug']
        with transaction.atomic():
            experiences = Experience.objects.filter(
                department__hotel=hotel,
                department__slug=dept_slug,
                id__in=ordered_ids,
            )
            if experiences.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more IDs do not belong to this department.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            exp_map = {e.id: e for e in experiences}
            for idx, exp_id in enumerate(ordered_ids):
                exp_map[exp_id].display_order = idx
            Experience.objects.bulk_update(exp_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} experiences reordered.'})


class EventBulkReorder(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def patch(self, request, **kwargs):
        ordered_ids = request.data.get('order', [])
        if not ordered_ids or not isinstance(ordered_ids, list):
            return Response(
                {'detail': 'order must be a non-empty list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ordered_ids) != len(set(ordered_ids)):
            return Response(
                {'detail': 'Duplicate IDs in order list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        with transaction.atomic():
            events = Event.objects.filter(hotel=hotel, id__in=ordered_ids)
            if events.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more IDs do not belong to this hotel.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            event_map = {e.id: e for e in events}
            for idx, event_id in enumerate(ordered_ids):
                event_map[event_id].display_order = idx
            Event.objects.bulk_update(event_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} events reordered.'})


# ---------------------------------------------------------------------------
# Hotel info sections
# ---------------------------------------------------------------------------

class HotelInfoSectionViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAdminOrAbove]
    serializer_class = HotelInfoSectionSerializer
    pagination_class = None

    def get_queryset(self):
        return HotelInfoSection.objects.filter(hotel=self.get_hotel())

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        max_order = HotelInfoSection.objects.filter(
            hotel=hotel,
        ).aggregate(m=Max('display_order'))['m']
        serializer.save(hotel=hotel, display_order=(max_order or 0) + 1)


class InfoSectionBulkReorder(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def patch(self, request, **kwargs):
        ordered_ids = request.data.get('order', [])
        if not ordered_ids or not isinstance(ordered_ids, list):
            return Response(
                {'detail': 'order must be a non-empty list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ordered_ids) != len(set(ordered_ids)):
            return Response(
                {'detail': 'Duplicate IDs in order list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        with transaction.atomic():
            sections = HotelInfoSection.objects.filter(hotel=hotel)
            total_count = sections.count()
            if len(ordered_ids) != total_count:
                return Response(
                    {'detail': 'order must include all info section IDs for this hotel.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            matched = sections.filter(id__in=ordered_ids)
            if matched.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more IDs do not belong to this hotel.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            section_map = {s.id: s for s in matched}
            for idx, section_id in enumerate(ordered_ids):
                section_map[section_id].display_order = idx
            HotelInfoSection.objects.bulk_update(section_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} info sections reordered.'})


# ---------------------------------------------------------------------------
# Content image upload (for rich text editors — model-less)
# Files are not reference-tracked in DB. Orphaned images are cleaned up
# weekly by cleanup_orphaned_content_images_task (see tasks.py).
# ---------------------------------------------------------------------------

class ContentImageUpload(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def post(self, request, **kwargs):
        import uuid
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        file = request.FILES.get('image')
        if not file:
            return Response(
                {'detail': 'No image provided.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            from .validators import validate_image_upload
            buf, fmt = validate_image_upload(file)
        except Exception as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ext = 'png' if fmt == 'png' else 'jpg'
        filename = f'{uuid.uuid4().hex[:12]}.{ext}'
        hotel = self.get_hotel()
        path = default_storage.save(
            f'content/{hotel.slug}/{filename}',
            ContentFile(buf.getvalue()),
        )
        image_url = default_storage.url(path)
        if image_url.startswith('/'):
            image_url = request.build_absolute_uri(image_url)
        return Response({'url': image_url})


# ---------------------------------------------------------------------------
# Experience gallery images
# ---------------------------------------------------------------------------

class ExperienceImageUpload(HotelScopedMixin, generics.CreateAPIView):
    permission_classes = [IsAdminOrAbove]
    serializer_class = ExperienceImageSerializer

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        exp = get_object_or_404(
            Experience,
            pk=self.kwargs['exp_id'],
            department__hotel=hotel,
            department__slug=self.kwargs['dept_slug'],
        )
        max_order = exp.gallery_images.aggregate(m=Max('display_order'))['m'] or 0
        serializer.save(experience=exp, display_order=max_order + 1)


class ExperienceImageDelete(HotelScopedMixin, generics.DestroyAPIView):
    permission_classes = [IsAdminOrAbove]

    def get_queryset(self):
        return ExperienceImage.objects.filter(
            experience__department__hotel=self.get_hotel(),
            experience__department__slug=self.kwargs.get('dept_slug'),
        )


class ExperienceImageReorder(HotelScopedMixin, APIView):
    permission_classes = [IsAdminOrAbove]

    def patch(self, request, **kwargs):
        ordered_ids = request.data.get('order', [])
        if not ordered_ids or not isinstance(ordered_ids, list):
            return Response(
                {'detail': 'order must be a non-empty list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(ordered_ids) != len(set(ordered_ids)):
            return Response(
                {'detail': 'Duplicate IDs in order list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        with transaction.atomic():
            images = ExperienceImage.objects.filter(
                experience__department__hotel=hotel,
                experience__department__slug=self.kwargs['dept_slug'],
                experience__pk=self.kwargs['exp_id'],
                id__in=ordered_ids,
            )
            if images.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more image IDs are invalid.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            img_map = {img.id: img for img in images}
            for idx, img_id in enumerate(ordered_ids):
                img_map[img_id].display_order = idx
            ExperienceImage.objects.bulk_update(img_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} images reordered.'})


class QRCodeViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAdminOrAbove]
    serializer_class = QRCodeSerializer
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        return QRCode.objects.filter(
            hotel=self.get_hotel(),
        ).annotate(
            stay_count=Count('guest_stays'),
        )

    def _reject_booking_placement(self, serializer):
        if serializer.validated_data.get('placement') == 'BOOKING':
            raise serializers.ValidationError(
                {'placement': 'BOOKING QR codes are managed from the Booking Email page.'}
            )

    def perform_create(self, serializer):
        self._reject_booking_placement(serializer)
        hotel = self.get_hotel()
        qr = generate_qr(
            hotel=hotel,
            label=serializer.validated_data['label'],
            placement=serializer.validated_data['placement'],
            department=serializer.validated_data.get('department'),
            created_by=self.request.user,
        )
        serializer.instance = qr

    def perform_update(self, serializer):
        self._reject_booking_placement(serializer)
        # Also block editing existing BOOKING QR codes via generic endpoint
        if serializer.instance.placement == 'BOOKING':
            raise serializers.ValidationError(
                {'placement': 'BOOKING QR codes are managed from the Booking Email page.'}
            )
        serializer.save()


class NotificationRouteViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    """CRUD for notification routing rules (WhatsApp/Email contacts per department)."""
    permission_classes = [IsAdminOrAbove]
    serializer_class = NotificationRouteSerializer
    pagination_class = None  # Small set per hotel, no pagination needed
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        qs = NotificationRoute.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('department', 'experience', 'member__user').order_by('department__name', 'channel', 'id')
        dept_id = self.request.query_params.get('department')
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (ValueError, TypeError):
                qs = qs.none()
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['hotel'] = self.get_hotel()
        return ctx

    def perform_create(self, serializer):
        serializer.save(
            hotel=self.get_hotel(),
            created_by=self.request.user,
        )


class BookingEmailTemplateView(HotelScopedMixin, APIView):
    """GET: auto-create template + BOOKING QR. PATCH: update template fields."""
    permission_classes = [IsAdminOrAbove]

    def _get_or_create_template(self, hotel, user):
        """Atomic auto-create of template + BOOKING QR. Idempotent."""
        with transaction.atomic():
            template, _created = BookingEmailTemplate.objects.select_for_update().get_or_create(
                hotel=hotel,
                defaults={
                    'subject': f'Welcome to {hotel.name} \u2014 Your Digital Concierge',
                    'heading': 'Your Digital Concierge Awaits',
                    'body': (
                        "We're excited to welcome you! Scan the QR code below "
                        "or tap the button to explore everything we have to offer "
                        "\u2014 from dining and spa experiences to room service requests."
                    ),
                    'features': [
                        'Browse curated experiences',
                        'Make service requests',
                        'Get real-time updates',
                    ],
                    'cta_text': 'Explore Our Services',
                    'footer_text': '',
                },
            )
            if not template.qr_code:
                existing_qr = QRCode.objects.filter(
                    hotel=hotel, placement='BOOKING',
                ).first()
                if existing_qr:
                    template.qr_code = existing_qr
                else:
                    try:
                        template.qr_code = generate_qr(
                            hotel=hotel,
                            label='Booking Email',
                            placement='BOOKING',
                            department=None,
                            created_by=user,
                        )
                    except IntegrityError:
                        template.qr_code = QRCode.objects.get(
                            hotel=hotel, placement='BOOKING',
                        )
                template.save(update_fields=['qr_code'])
        return template

    def get(self, request, hotel_slug):
        hotel = self.get_hotel()
        template = self._get_or_create_template(hotel, request.user)
        serializer = BookingEmailTemplateSerializer(template, context={'request': request})
        return Response(serializer.data)

    def patch(self, request, hotel_slug):
        hotel = self.get_hotel()
        template = self._get_or_create_template(hotel, request.user)
        serializer = BookingEmailTemplateSerializer(
            template, data=request.data, partial=True, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# SuperAdmin views
# ---------------------------------------------------------------------------

class MemberList(HotelScopedMixin, generics.ListCreateAPIView):
    permission_classes = [IsSuperAdmin]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return MemberCreateSerializer
        return MemberSerializer

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user', 'department', 'hotel')

    def create(self, request, *args, **kwargs):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        hotel = self.get_hotel()

        # Find existing user by email or phone, or create new
        from django.db import IntegrityError
        user = None

        # Look up by email first (staff typically have email)
        if data['email']:
            try:
                user = User.objects.get(email=data['email'])
            except User.DoesNotExist:
                pass

        # Fall back to phone lookup (guest accounts have email='')
        if user is None and data['phone']:
            try:
                user = User.objects.get(phone=data['phone'])
            except User.DoesNotExist:
                pass

        if user is not None:
            # Promote to staff and backfill missing fields
            changed_fields = []
            if user.user_type == 'GUEST':
                user.user_type = 'STAFF'
                changed_fields.append('user_type')
            if not user.phone and data['phone']:
                user.phone = data['phone']
                changed_fields.append('phone')
            if not user.email and data['email']:
                user.email = data['email']
                changed_fields.append('email')
            if changed_fields:
                try:
                    user.save(update_fields=changed_fields)
                except IntegrityError:
                    return Response(
                        {'detail': 'A user with this email or phone already exists.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            # Invited staff — no password set. They log in via phone+OTP.
            user = User(
                email=data['email'],
                phone=data['phone'],
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
                user_type='STAFF',
            )
            user.set_unusable_password()
            try:
                user.save()
            except IntegrityError:
                return Response(
                    {'detail': 'A user with this email or phone already exists.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Create membership
        membership, created = HotelMembership.objects.get_or_create(
            user=user, hotel=hotel,
            defaults={
                'role': data['role'],
                'department': data.get('department'),
            },
        )
        if not created:
            return Response(
                {'detail': 'User already has a membership for this hotel.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Send invitation notifications (background, best-effort)
        from django.db import transaction as db_transaction
        def _enqueue_invite():
            try:
                from .tasks import send_staff_invite_notification_task
                send_staff_invite_notification_task.delay(user.id, hotel.id, data['role'])
            except Exception:
                logger.warning('Failed to enqueue staff invite notification (broker down?)', exc_info=True)
        db_transaction.on_commit(_enqueue_invite)

        return Response(
            MemberSerializer(membership).data,
            status=status.HTTP_201_CREATED,
        )


class MemberDetail(HotelScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperAdmin]
    serializer_class = MemberSerializer

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user', 'department', 'hotel')

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class HotelSettingsUpdate(HotelScopedMixin, generics.RetrieveUpdateAPIView):
    permission_classes = [IsSuperAdmin]
    serializer_class = HotelSettingsSerializer
    http_method_names = ['get', 'patch']

    def get_object(self):
        return self.get_hotel()


# ---------------------------------------------------------------------------
# User-scoped views
# ---------------------------------------------------------------------------

class MyHotelsList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = HotelPublicSerializer

    def get_queryset(self):
        return Hotel.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
            is_active=True,
        ).distinct()


class NotificationList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user)
        is_read = self.request.query_params.get('is_read')
        if is_read is not None:
            qs = qs.filter(is_read=is_read.lower() in ('true', '1'))
        return qs.order_by('-created_at')


class NotificationMarkRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = request.data.get('ids', [])
        if ids:
            Notification.objects.filter(
                user=request.user, id__in=ids,
            ).update(is_read=True)
        else:
            Notification.objects.filter(
                user=request.user, is_read=False,
            ).update(is_read=True)
        return Response({'detail': 'Marked as read.'})


class PushSubscriptionCreate(generics.CreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PushSubscriptionSerializer

    def perform_create(self, serializer):
        # Upsert by endpoint to prevent duplicate rows from repeated
        # registrations on the same device while preserving multi-device subs.
        # Lock the user row to serialize all push sub operations for this
        # user — select_for_update on PushSubscription alone is a no-op
        # when no matching row exists yet, so we lock the parent instead.
        from django.contrib.auth import get_user_model
        User = get_user_model()

        endpoint = serializer.validated_data['subscription_info']['endpoint']
        with transaction.atomic():
            # Lock user row — serializes concurrent push registrations
            User.objects.select_for_update().filter(
                pk=self.request.user.pk,
            ).first()

            dupes = list(
                PushSubscription.objects.filter(
                    user=self.request.user,
                    subscription_info__endpoint=endpoint,
                ).order_by('pk')
            )

            if dupes:
                # Keep the first, update it, delete any extras
                existing = dupes[0]
                existing.subscription_info = serializer.validated_data[
                    'subscription_info'
                ]
                existing.is_active = True
                existing.save(
                    update_fields=['subscription_info', 'is_active'],
                )
                if len(dupes) > 1:
                    PushSubscription.objects.filter(
                        pk__in=[d.pk for d in dupes[1:]],
                    ).delete()
                # Point serializer.instance at the model so DRF
                # returns the full object (including id) in the response.
                serializer.instance = existing
                return
            serializer.save(user=self.request.user)


class PushSubscriptionDelete(generics.DestroyAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return PushSubscription.objects.filter(user=self.request.user)


class PushSubscriptionBulkDelete(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        PushSubscription.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MyRequestDetail(generics.RetrieveAPIView):
    """Push deep-link resolver. Looks up request by public_id,
    derives hotel from request.hotel (not URL slug)."""
    permission_classes = [IsAuthenticated, CanAccessRequestObjectByLookup]
    serializer_class = ServiceRequestDetailSerializer
    lookup_field = 'public_id'

    def get_queryset(self):
        return ServiceRequest.objects.select_related(
            'department', 'experience', 'event', 'guest_stay__guest', 'hotel',
        ).prefetch_related('activities__actor')

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        data = response.data
        data['hotel_slug'] = self.get_object().hotel.slug
        return response


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

class GupshupWAWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # Validate shared-secret header (Gupshup "Include Headers" key-value pair)
        secret = settings.GUPSHUP_WA_WEBHOOK_SECRET
        if not secret:
            logger.error('GUPSHUP_WA_WEBHOOK_SECRET not configured; rejecting webhook')
            return Response(status=status.HTTP_403_FORBIDDEN)

        token = request.headers.get('X-Webhook-Secret', '')
        if not hmac.compare_digest(token, secret):
            logger.warning('Invalid Gupshup webhook secret header')
            return Response(status=status.HTTP_403_FORBIDDEN)

        payload = request.data

        # 1. Existing OTP delivery tracking (matches by gupshup_message_id on OTPCode)
        try:
            handle_wa_delivery_event(payload)
        except Exception:
            logger.exception('Error processing Gupshup webhook (OTP handler)')

        # 2. Notification channel handlers (inbound messages + delivery status)
        from .notifications.webhook import handle_inbound_message, handle_message_event
        event_type = payload.get('type', '')
        try:
            if event_type == 'message':
                handle_inbound_message(payload)
            elif event_type == 'message-event':
                handle_message_event(payload)
        except Exception:
            logger.exception('Error processing Gupshup webhook (notification handler)')

        return Response(status=status.HTTP_200_OK)
