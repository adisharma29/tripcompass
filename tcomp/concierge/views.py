import hashlib
import hmac
import json
import logging
import re
import secrets
import zoneinfo
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, models, transaction
from django.db.models import Count, F, Max, Prefetch
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .filters import ServiceRequestFilter
from .mixins import HotelScopedMixin
from .models import (
    BookingEmailTemplate, ContentStatus, Department, DepartmentImage,
    DeliveryRecord, Event, EventImage, Experience,
    ExperienceImage, GuestInvite, GuestStay, Hotel, HotelInfoSection,
    HotelMembership, Notification, NotificationRoute, PushSubscription,
    QRCode, QRScanDaily, Rating, RatingPrompt, ServiceRequest, RequestActivity,
    SpecialRequestOffering, SpecialRequestOfferingImage,
)
from .permissions import (
    CanAccessRequestObject, CanAccessRequestObjectByLookup,
    IsActiveGuest, IsAdminOrAbove, IsStaffOrAbove,
    IsStayOwner, IsSuperAdmin,
)
from .serializers import (
    BookingEmailTemplateSerializer, DashboardStatsSerializer,
    DepartmentImageSerializer, DepartmentPublicSerializer, DepartmentSerializer,
    EventImageSerializer, EventPublicSerializer, EventSerializer,
    ExperienceImageSerializer,
    ExperiencePublicSerializer, ExperienceSerializer, TopDealSerializer,
    GuestInviteSerializer, GuestStaySerializer, GuestStayUpdateSerializer,
    HotelInfoSectionSerializer, HotelPublicSerializer,
    HotelSettingsSerializer, MemberCreateSerializer,
    MemberSelfSerializer, MemberSerializer,
    MergeMemberSerializer, TransferMemberSerializer,
    NotificationRouteSerializer, NotificationSerializer,
    PushSubscriptionSerializer, QRCodeSerializer,
    SendInviteSerializer,
    ServiceRequestCreateSerializer, ServiceRequestDetailSerializer,
    ServiceRequestListSerializer, ServiceRequestUpdateSerializer,
    SpecialRequestOfferingSerializer, SpecialRequestOfferingPublicSerializer,
    SpecialRequestOfferingImageSerializer,
    RatingSerializer, RatingPromptSerializer, StayPromptSerializer,
    SubmitRatingSerializer, AdminRatingSerializer, RatingSummarySerializer,
    SendSurveySerializer,
)
from .notifications import NotificationEvent, dispatch_notification
from .services import (
    check_invite_rate_limit_hotel, check_invite_rate_limit_phone,
    check_invite_rate_limit_staff, check_invite_resend_rate_limit,
    check_room_rate_limit, check_stay_rate_limit,
    compute_response_due_at, generate_qr,
    get_dashboard_stats, get_template, handle_wa_delivery_event,
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
        'departments__gallery_images',
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
            'gallery_images',
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
            'gallery_images',
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
        ).prefetch_related('gallery_images')

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
        # get_next_occurrence() requires Python-side rrule evaluation — can't be
        # done in SQL. Cap the working set to prevent abuse on large event lists.
        MAX_EVENTS = 200
        far_future = timezone.now() + timedelta(days=3650)
        events = sorted(
            queryset[:MAX_EVENTS],
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
        ).prefetch_related('gallery_images')


# ---------------------------------------------------------------------------
# Special Request Offerings — Public
# ---------------------------------------------------------------------------

class SpecialRequestOfferingPublicList(APIView):
    """Public list returning offerings grouped by category."""
    permission_classes = [AllowAny]

    def get(self, request, hotel_slug):
        qs = SpecialRequestOffering.objects.filter(
            hotel__slug=hotel_slug,
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
        ).prefetch_related('gallery_images').order_by('display_order', 'name')

        utilitarian = []
        personalization = []
        for offering in qs:
            data = SpecialRequestOfferingPublicSerializer(offering, context={'request': request}).data
            if offering.category == SpecialRequestOffering.Category.UTILITARIAN:
                utilitarian.append(data)
            else:
                personalization.append(data)

        return Response({
            'utilitarian': utilitarian,
            'personalization': personalization,
        })


class SpecialRequestOfferingPublicDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = SpecialRequestOfferingPublicSerializer
    lookup_field = 'slug'
    lookup_url_kwarg = 'offering_slug'

    def get_queryset(self):
        return SpecialRequestOffering.objects.filter(
            hotel__slug=self.kwargs['hotel_slug'],
            hotel__is_active=True,
            status=ContentStatus.PUBLISHED,
        ).prefetch_related('gallery_images')


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
# QR scan tracking (public, pre-auth)
# ---------------------------------------------------------------------------

def _get_hotel_tz(hotel):
    """Return the hotel's ZoneInfo, falling back to UTC on bad/empty values."""
    try:
        return zoneinfo.ZoneInfo(hotel.timezone) if hotel.timezone else zoneinfo.ZoneInfo('UTC')
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        return zoneinfo.ZoneInfo('UTC')


def _record_scan(qr, vid, hotel):
    """Dedup via Redis and atomically increment the daily aggregate row."""
    hotel_tz = _get_hotel_tz(hotel)
    now_local = timezone.now().astimezone(hotel_tz)
    today = now_local.date()
    hour = now_local.hour

    hour_key = f'qrscan:{qr.id}:{vid}:{today}:{hour}'
    day_key = f'qrscan:uv:{qr.id}:{vid}:{today}'

    is_new_visitor = True
    try:
        if cache.get(hour_key):
            return
        cache.set(hour_key, 1, timeout=3600)

        is_new_visitor = not cache.get(day_key)
        if is_new_visitor:
            cache.set(day_key, 1, timeout=86400)
    except Exception:
        # Redis down — fall through to DB write without dedup.
        is_new_visitor = True

    try:
        obj, _created = QRScanDaily.objects.get_or_create(
            qr_code=qr, date=today,
        )
    except IntegrityError:
        obj = QRScanDaily.objects.get(qr_code=qr, date=today)

    QRScanDaily.objects.filter(pk=obj.pk).update(
        scan_count=F('scan_count') + 1,
        unique_visitors=F('unique_visitors') + (1 if is_new_visitor else 0),
    )


class QRScanThrottle(AnonRateThrottle):
    rate = '300/min'
    scope = 'qr_scan'


class QRScanSerializer(serializers.Serializer):
    """Accepts both JSON and form-encoded bodies from sendBeacon."""
    code = serializers.CharField(max_length=40)
    vid = serializers.CharField(max_length=64, required=False, default='')


class QRScanView(APIView):
    """Record a raw QR code scan. Public, rate-limited, best-effort."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [QRScanThrottle]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, hotel_slug):
        ser = QRScanSerializer(data=request.data)
        if not ser.is_valid():
            return Response(status=status.HTTP_204_NO_CONTENT)
        code = ser.validated_data['code'].strip()
        vid = ser.validated_data['vid'].strip()
        if not code:
            return Response(status=status.HTTP_204_NO_CONTENT)

        if not vid:
            vid = f'ip:{self._get_client_ip(request)}'

        try:
            qr = QRCode.objects.select_related('hotel').get(
                code=code, hotel__slug=hotel_slug, is_active=True,
            )
        except QRCode.DoesNotExist:
            return Response(status=status.HTTP_204_NO_CONTENT)

        _record_scan(qr, vid, qr.hotel)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _get_client_ip(request):
        return (
            request.META.get('HTTP_CF_CONNECTING_IP')
            or request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', '')
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

        # Rate limits (stay-level always applies)
        if not check_stay_rate_limit(stay):
            return Response(
                {'detail': 'Too many requests. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Room number: required unless the offering explicitly opts out
        offering = serializer.validated_data.get('special_request_offering')
        room_required = True
        if offering and not offering.requires_room_number:
            room_required = False
        if room_required and not stay.room_number:
            return Response(
                {'detail': 'Please set your room number first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if stay.room_number and not check_room_rate_limit(hotel, stay.room_number):
            return Response(
                {'detail': 'Too many requests from this room. Please try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

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
            offering_obj=req.special_request_offering,
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
                offering_obj=req.special_request_offering,
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
        from django.db.models import OuterRef, Subquery
        qs = ServiceRequest.objects.filter(
            guest_stay__guest=self.request.user,
        ).select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
            'special_request_offering',
        ).annotate(
            _rating_score=Subquery(
                Rating.objects.filter(
                    service_request=OuterRef('pk'),
                ).values('score')[:1]
            ),
        ).order_by('-created_at')
        hotel_slug = self.request.query_params.get('hotel')
        if hotel_slug:
            qs = qs.filter(guest_stay__hotel__slug=hotel_slug)
        status_filter = self.request.query_params.getlist('status')
        if status_filter:
            qs = qs.filter(status__in=status_filter)
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
        from django.db.models import OuterRef, Subquery
        qs = super().get_queryset().select_related(
            'department', 'experience', 'event', 'guest_stay__guest',
            'special_request_offering',
        ).annotate(
            _rating_score=Subquery(
                Rating.objects.filter(
                    service_request=OuterRef('pk'),
                ).values('score')[:1]
            ),
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
            'special_request_offering',
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
                now = timezone.now()
                instance.confirmed_at = now
                update_fields = ['confirmed_at']
                # Set rating eligibility if ratings enabled
                hotel = instance.hotel
                if hotel.ratings_enabled and instance.guest_stay_id:
                    delay = timedelta(hours=hotel.rating_delay_hours)
                    instance.rating_prompt_eligible_at = now + delay
                    update_fields.append('rating_prompt_eligible_at')
                instance.save(update_fields=update_fields)

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
            'special_request_offering',
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
        serializer = DashboardStatsSerializer(stats)
        return Response(serializer.data)


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
        ).prefetch_related('experiences__gallery_images', 'gallery_images')


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
        qs = Event.objects.filter(
            hotel=self.get_hotel(),
        ).select_related(
            'department', 'experience__department', 'hotel__fallback_department',
        ).prefetch_related('gallery_images')
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


# ---------------------------------------------------------------------------
# Department gallery images
# ---------------------------------------------------------------------------

class DepartmentImageUpload(HotelScopedMixin, generics.CreateAPIView):
    permission_classes = [IsAdminOrAbove]
    serializer_class = DepartmentImageSerializer

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        dept = get_object_or_404(
            Department,
            hotel=hotel,
            slug=self.kwargs['dept_slug'],
        )
        max_order = dept.gallery_images.aggregate(m=Max('display_order'))['m'] or 0
        serializer.save(department=dept, display_order=max_order + 1)


class DepartmentImageDelete(HotelScopedMixin, generics.DestroyAPIView):
    permission_classes = [IsAdminOrAbove]

    def get_queryset(self):
        return DepartmentImage.objects.filter(
            department__hotel=self.get_hotel(),
        )


class DepartmentImageReorder(HotelScopedMixin, APIView):
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
            images = DepartmentImage.objects.filter(
                department__hotel=hotel,
                department__slug=self.kwargs['dept_slug'],
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
            DepartmentImage.objects.bulk_update(img_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} images reordered.'})


# ---------------------------------------------------------------------------
# Event gallery images
# ---------------------------------------------------------------------------

class EventImageUpload(HotelScopedMixin, generics.CreateAPIView):
    permission_classes = [IsAdminOrAbove]
    serializer_class = EventImageSerializer

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        event = get_object_or_404(
            Event,
            pk=self.kwargs['event_id'],
            hotel=hotel,
        )
        max_order = event.gallery_images.aggregate(m=Max('display_order'))['m'] or 0
        serializer.save(event=event, display_order=max_order + 1)


class EventImageDelete(HotelScopedMixin, generics.DestroyAPIView):
    permission_classes = [IsAdminOrAbove]

    def get_queryset(self):
        return EventImage.objects.filter(
            event__hotel=self.get_hotel(),
        )


class EventImageReorder(HotelScopedMixin, APIView):
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
            images = EventImage.objects.filter(
                event__hotel=hotel,
                event__pk=self.kwargs['event_id'],
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
            EventImage.objects.bulk_update(img_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} images reordered.'})


# ---------------------------------------------------------------------------
# Special Request Offerings — Admin
# ---------------------------------------------------------------------------

class SpecialRequestOfferingViewSet(HotelScopedMixin, viewsets.ModelViewSet):
    serializer_class = SpecialRequestOfferingSerializer
    pagination_class = None

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsStaffOrAbove()]
        return [IsAdminOrAbove()]

    def get_queryset(self):
        qs = SpecialRequestOffering.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('department').prefetch_related('gallery_images')
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['hotel'] = self.get_hotel()
        return ctx

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        max_order = SpecialRequestOffering.objects.filter(
            hotel=hotel,
            category=serializer.validated_data['category'],
        ).aggregate(m=Max('display_order'))['m']
        serializer.save(hotel=hotel, display_order=(max_order or 0) + 1)


class SpecialRequestOfferingBulkReorder(HotelScopedMixin, APIView):
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

        category = request.query_params.get('category')
        if category not in ('UTILITARIAN', 'PERSONALIZATION'):
            return Response(
                {'detail': 'category query param must be UTILITARIAN or PERSONALIZATION.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hotel = self.get_hotel()
        with transaction.atomic():
            offerings = SpecialRequestOffering.objects.filter(
                hotel=hotel, category=category, id__in=ordered_ids,
            )
            if offerings.count() != len(ordered_ids):
                return Response(
                    {'detail': 'One or more IDs do not belong to this hotel/category.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            off_map = {o.id: o for o in offerings}
            for idx, off_id in enumerate(ordered_ids):
                off_map[off_id].display_order = idx
            SpecialRequestOffering.objects.bulk_update(off_map.values(), ['display_order'])

        return Response({'detail': f'{len(ordered_ids)} offerings reordered.'})


class SpecialRequestOfferingImageUpload(HotelScopedMixin, generics.CreateAPIView):
    permission_classes = [IsAdminOrAbove]
    serializer_class = SpecialRequestOfferingImageSerializer

    def perform_create(self, serializer):
        hotel = self.get_hotel()
        offering = get_object_or_404(
            SpecialRequestOffering,
            pk=self.kwargs['offering_id'],
            hotel=hotel,
        )
        max_order = offering.gallery_images.aggregate(m=Max('display_order'))['m'] or 0
        serializer.save(offering=offering, display_order=max_order + 1)


class SpecialRequestOfferingImageDelete(HotelScopedMixin, generics.DestroyAPIView):
    permission_classes = [IsAdminOrAbove]

    def get_queryset(self):
        return SpecialRequestOfferingImage.objects.filter(
            offering__hotel=self.get_hotel(),
        )


class SpecialRequestOfferingImageReorder(HotelScopedMixin, APIView):
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
            images = SpecialRequestOfferingImage.objects.filter(
                offering__hotel=hotel,
                offering__pk=self.kwargs['offering_id'],
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
            SpecialRequestOfferingImage.objects.bulk_update(img_map.values(), ['display_order'])

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
        ).select_related(
            'department', 'experience', 'event', 'special_request_offering', 'member__user',
        ).order_by('channel', 'id')
        dept_id = self.request.query_params.get('department')
        event_id = self.request.query_params.get('event')
        offering_id = self.request.query_params.get('special_request_offering')
        # Mutual exclusion: cannot filter by multiple scopes
        scope_count = sum(1 for v in (dept_id, event_id, offering_id) if v)
        if scope_count > 1:
            raise serializers.ValidationError('Cannot filter by more than one scope (department, event, special_request_offering).')
        if dept_id:
            try:
                qs = qs.filter(department_id=int(dept_id))
            except (ValueError, TypeError):
                qs = qs.none()
        if event_id:
            try:
                qs = qs.filter(event_id=int(event_id))
            except (ValueError, TypeError):
                qs = qs.none()
        if offering_id:
            try:
                qs = qs.filter(special_request_offering_id=int(offering_id))
            except (ValueError, TypeError):
                qs = qs.none()
        member_id = self.request.query_params.get('member')
        if member_id:
            try:
                qs = qs.filter(member_id=int(member_id))
            except (ValueError, TypeError):
                qs = qs.none()
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['hotel'] = self.get_hotel()
        return ctx

    def perform_create(self, serializer):
        self._save_with_conflict_handling(
            serializer,
            hotel=self.get_hotel(),
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        self._save_with_conflict_handling(serializer)

    def _save_with_conflict_handling(self, serializer, **kwargs):
        """Save, converting model ValidationError / IntegrityError to DRF 400.

        With Meta.validators = [] on the serializer, uniqueness is enforced
        at the DB level.  Model.save() → full_clean() raises Django
        ValidationError; a race or constraint violation raises IntegrityError.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            serializer.save(**kwargs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else exc.messages,
            )
        except IntegrityError:
            raise serializers.ValidationError(
                'A route with this target, channel, and scope already exists.',
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
    # Admin can list members (for member picker in notification routing).
    # Only SuperAdmin can create (invite) new members.
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAdminOrAbove()]
        return [IsSuperAdmin()]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return MemberCreateSerializer
        return MemberSerializer

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user', 'department', 'hotel').annotate(
            route_count=Count('notification_routes', distinct=True),
            assignment_count=Count(
                'user__assigned_requests',
                filter=models.Q(user__assigned_requests__hotel=models.F('hotel')),
                distinct=True,
            ),
        )

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
        temp_pw = None

        # Look up by email first (staff typically have email)
        if data['email']:
            try:
                user = User.objects.get(email__iexact=data['email'])
            except User.MultipleObjectsReturned:
                user = User.objects.filter(email__iexact=data['email']).order_by('date_joined').first()
            except User.DoesNotExist:
                pass

        # Fall back to phone lookup (guest accounts have email='')
        if user is None and data['phone']:
            normalized = re.sub(r'\D', '', data['phone'])
            try:
                user = User.objects.get(phone=normalized)
            except User.DoesNotExist:
                # Pre-backfill compatibility: try +prefixed format
                try:
                    user = User.objects.get(phone=f'+{normalized}')
                except User.DoesNotExist:
                    pass

        if user is not None:
            # Reactivate, promote to staff, and backfill missing fields
            changed_fields = []
            if not user.is_active:
                user.is_active = True
                changed_fields.append('is_active')
            if user.user_type == 'GUEST':
                user.user_type = 'STAFF'
                changed_fields.append('user_type')
            if not user.phone and data['phone']:
                user.phone = data['phone']
                changed_fields.append('phone')
            if not user.email and data['email']:
                user.email = data['email']
                changed_fields.append('email')
            # Email-only reused user with no usable password — set temp password
            if data['email'] and not (user.phone or data['phone']) and not user.has_usable_password():
                temp_pw = secrets.token_urlsafe(12)  # noqa: S105
                user.set_password(temp_pw)
                changed_fields.append('password')
                logger.info('Email-only invite (reused): temp password generated for %s', user.email)
            if changed_fields:
                try:
                    user.save(update_fields=changed_fields)
                except IntegrityError:
                    return Response(
                        {'detail': 'A user with this email or phone already exists.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            user = User(
                email=data['email'],
                phone=data['phone'],
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
                user_type='STAFF',
            )
            if data['email'] and not data['phone']:
                # Email-only invite: set temp password as break-glass fallback.
                # Primary onboarding is via set-password link in the invite email.
                temp_pw = secrets.token_urlsafe(12)  # noqa: S105
                user.set_password(temp_pw)
                logger.info('Email-only invite: temp password generated for %s', data['email'])
            else:
                # Phone users log in via OTP — no password needed.
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

        resp_data = MemberSerializer(membership).data
        if temp_pw:
            resp_data['temp_password'] = temp_pw
        return Response(resp_data, status=status.HTTP_201_CREATED)


class MemberDetail(HotelScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsSuperAdmin]
    serializer_class = MemberSerializer

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user', 'department', 'hotel').annotate(
            route_count=Count('notification_routes', distinct=True),
            assignment_count=Count(
                'user__assigned_requests',
                filter=models.Q(user__assigned_requests__hotel=models.F('hotel')),
                distinct=True,
            ),
        )

    def perform_update(self, serializer):
        instance = serializer.instance
        new_role = serializer.validated_data.get('role', instance.role)
        new_active = serializer.validated_data.get('is_active', instance.is_active)

        # Guard: demoting or deactivating the last active SUPERADMIN
        if instance.role == 'SUPERADMIN' and (new_role != 'SUPERADMIN' or not new_active):
            remaining = HotelMembership.objects.filter(
                hotel=instance.hotel, role='SUPERADMIN', is_active=True,
            ).exclude(pk=instance.pk).count()
            if remaining == 0:
                from rest_framework.exceptions import ValidationError
                raise ValidationError(
                    {'detail': 'Cannot demote or deactivate the last active superadmin.'}
                )

        serializer.save()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        # Guard: cannot delete self
        if instance.user == request.user:
            return Response(
                {'detail': 'Cannot delete your own membership.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Guard: last active SUPERADMIN
        if instance.role == 'SUPERADMIN':
            remaining = HotelMembership.objects.filter(
                hotel=instance.hotel, role='SUPERADMIN', is_active=True,
            ).exclude(pk=instance.pk).count()
            if remaining == 0:
                return Response(
                    {'detail': 'Cannot delete the last active superadmin.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        # Guard: block if member still has linked data
        has_routes = instance.notification_routes.exists()
        has_assignments = ServiceRequest.objects.filter(
            assigned_to=instance.user, hotel=instance.hotel,
        ).exists()
        if has_routes or has_assignments:
            return Response(
                {'detail': 'Transfer data first or deactivate instead.',
                 'has_routes': has_routes,
                 'has_assignments': has_assignments},
                status=status.HTTP_409_CONFLICT,
            )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _transfer_member_data(source, target, actor, reason):
    """Shared transfer logic for transfer-and-remove + merge.
    Returns (transferred_routes, skipped_routes, transferred_requests).
    """
    # 1. Transfer notification routes with dedupe
    routes = NotificationRoute.objects.filter(member=source)
    transferred_routes = 0
    skipped_routes = 0
    for route in routes:
        new_target = target.user.phone if route.channel == 'WHATSAPP' else target.user.email
        new_label = f'{target.user.first_name} {target.user.last_name}'.strip() or target.user.email
        # Check for collision — exclude self to prevent self-match
        conflict = NotificationRoute.objects.filter(
            department=route.department,
            experience=route.experience,
            event=route.event,
            special_request_offering=route.special_request_offering,
            channel=route.channel,
            target=new_target,
        ).exclude(pk=route.pk).exists()
        if conflict:
            route.delete()
            skipped_routes += 1
        else:
            route.member = target
            route.target = new_target
            route.label = new_label
            route.save()
            transferred_routes += 1

    # 2. Reassign requests + create audit entries
    requests_qs = ServiceRequest.objects.filter(
        assigned_to=source.user, hotel=source.hotel,
    )
    transferred_requests = requests_qs.count()
    for req in requests_qs:
        RequestActivity.objects.create(
            request=req,
            actor=actor,
            action='REASSIGNED',
            details={
                'from_user_id': source.user.id,
                'from_name': str(source.user),
                'to_user_id': target.user.id,
                'to_name': str(target.user),
                'reason': reason,
            },
        )
    requests_qs.update(assigned_to=target.user)

    return transferred_routes, skipped_routes, transferred_requests


class MemberTransfer(HotelScopedMixin, generics.GenericAPIView):
    """POST /members/{id}/transfer/ — atomic transfer + hard-delete source."""
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user')

    def post(self, request, *args, **kwargs):
        source = self.get_object()
        # Guard: cannot transfer self
        if source.user == request.user:
            return Response({'detail': 'Cannot transfer your own membership.'}, status=400)
        # Guard: last active SUPERADMIN
        if source.role == 'SUPERADMIN':
            remaining = HotelMembership.objects.filter(
                hotel=source.hotel, role='SUPERADMIN', is_active=True,
            ).exclude(pk=source.pk).count()
            if remaining == 0:
                return Response(
                    {'detail': 'Cannot remove the last active superadmin.'},
                    status=400,
                )
        ser = TransferMemberSerializer(
            data=request.data,
            context={'hotel': source.hotel, 'source': source},
        )
        ser.is_valid(raise_exception=True)
        target = ser.validated_data['target_member']

        # Pre-validate route compatibility
        routes = NotificationRoute.objects.filter(member=source)
        incompatible = []
        for route in routes:
            if route.channel == 'WHATSAPP' and not target.user.phone:
                incompatible.append(f"WhatsApp route '{route.label}' — target has no phone")
            elif route.channel == 'EMAIL' and not target.user.email:
                incompatible.append(f"Email route '{route.label}' — target has no email")
        if incompatible:
            return Response(
                {'detail': 'Target member lacks required contact info.',
                 'incompatible_routes': incompatible},
                status=400,
            )

        with transaction.atomic():
            tr, sk, rq = _transfer_member_data(source, target, request.user, 'member_transfer')
            source.delete()

        return Response({
            'transferred_routes': tr,
            'skipped_routes': sk,
            'transferred_requests': rq,
        })


class MemberMerge(HotelScopedMixin, generics.GenericAPIView):
    """POST /members/merge/ — merge two members into one."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, *args, **kwargs):
        hotel = self.get_hotel()
        ser = MergeMemberSerializer(
            data=request.data,
            context={'hotel': hotel, 'request': request},
        )
        ser.is_valid(raise_exception=True)
        keep = ser.validated_data['keep_member']
        remove = ser.validated_data['remove_member']

        # Pre-validate route compatibility
        routes = NotificationRoute.objects.filter(member=remove)
        incompatible = []
        for route in routes:
            if route.channel == 'WHATSAPP' and not keep.user.phone:
                incompatible.append(f"WhatsApp route '{route.label}' — keep member has no phone")
            elif route.channel == 'EMAIL' and not keep.user.email:
                incompatible.append(f"Email route '{route.label}' — keep member has no email")

        with transaction.atomic():
            # Backfill contact info (never overwrite existing)
            keep_user = keep.user
            remove_user = remove.user
            if not keep_user.email and remove_user.email:
                keep_user.email = remove_user.email
            if not keep_user.phone and remove_user.phone:
                keep_user.phone = remove_user.phone
            if not keep_user.first_name and remove_user.first_name:
                keep_user.first_name = remove_user.first_name
            if not keep_user.last_name and remove_user.last_name:
                keep_user.last_name = remove_user.last_name
            keep_user.save()

            # Re-check compatibility after backfill (contact may have been filled)
            if incompatible:
                # Re-evaluate: after backfill, check again
                incompatible = []
                for route in routes:
                    if route.channel == 'WHATSAPP' and not keep_user.phone:
                        incompatible.append(f"WhatsApp route '{route.label}' — keep member has no phone")
                    elif route.channel == 'EMAIL' and not keep_user.email:
                        incompatible.append(f"Email route '{route.label}' — keep member has no email")
                if incompatible:
                    raise serializers.ValidationError({
                        'detail': 'Keep member lacks required contact info even after backfill.',
                        'incompatible_routes': incompatible,
                    })

            tr, sk, rq = _transfer_member_data(remove, keep, request.user, 'member_merge')
            remove.delete()

            # Deactivate orphaned user (STAFF only, no guest stays)
            if (
                remove_user.user_type == 'STAFF'
                and not remove_user.hotel_memberships.exists()
                and not remove_user.stays.filter(is_active=True).exists()
            ):
                remove_user.is_active = False
                remove_user.save(update_fields=['is_active'])

        return Response({
            'transferred_routes': tr,
            'skipped_routes': sk,
            'transferred_requests': rq,
        })


class MemberResendInvite(HotelScopedMixin, generics.GenericAPIView):
    """POST /members/{id}/resend-invite/ — re-send invitation notification."""
    permission_classes = [IsSuperAdmin]

    def get_queryset(self):
        return HotelMembership.objects.filter(
            hotel=self.get_hotel(),
        ).select_related('user')

    def post(self, request, *args, **kwargs):
        from datetime import timedelta
        member = self.get_object()
        if not member.is_active:
            return Response(
                {'detail': 'Cannot resend invite to inactive member.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Rate limit: 1 per 5 minutes per member
        if member.last_invite_sent_at:
            cooldown = timedelta(minutes=5)
            if timezone.now() - member.last_invite_sent_at < cooldown:
                remaining = cooldown - (timezone.now() - member.last_invite_sent_at)
                return Response(
                    {'detail': f'Please wait {int(remaining.total_seconds())} seconds before resending.'},
                    status=429,
                )
        resp_data = {'detail': 'Invite sent.'}

        # Email-only user who has never logged in: (re)generate temp password
        # so superadmin has a manual fallback if email delivery fails.
        # Must happen BEFORE enqueuing the task so the invite email token
        # is minted from the new password hash (avoids race invalidation).
        user = member.user
        if user.email and not user.phone and user.last_login is None:
            temp_pw = secrets.token_urlsafe(12)
            user.set_password(temp_pw)
            user.save(update_fields=['password'])
            resp_data['temp_password'] = temp_pw

        from .tasks import send_staff_invite_notification_task
        try:
            send_staff_invite_notification_task.delay(
                member.user.id, member.hotel.id, member.role,
            )
        except Exception:
            logger.exception('Failed to enqueue invite task for member %s', member.pk)
            # Still return temp_password so superadmin has the manual fallback.

        member.last_invite_sent_at = timezone.now()
        member.save(update_fields=['last_invite_sent_at'])
        return Response(resp_data)


class MemberSelfView(HotelScopedMixin, generics.RetrieveUpdateAPIView):
    """ADMIN+ can view/edit their own contact info (email, phone, name)."""
    permission_classes = [IsAdminOrAbove]
    serializer_class = MemberSelfSerializer

    def get_object(self):
        hotel = self.get_hotel()
        try:
            return HotelMembership.objects.select_related(
                'user', 'department', 'hotel',
            ).annotate(
                route_count=Count('notification_routes', distinct=True),
                assignment_count=Count(
                    'user__assigned_requests',
                    filter=models.Q(user__assigned_requests__hotel=models.F('hotel')),
                    distinct=True,
                ),
            ).get(user=self.request.user, hotel=hotel)
        except HotelMembership.DoesNotExist:
            raise NotFound('No membership found for this hotel.')


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
        ).select_related(
            'destination', 'fallback_department',
        ).prefetch_related(
            'departments__experiences__gallery_images',
            'departments__gallery_images',
            'info_sections',
        ).distinct()


class NotificationList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user).select_related('request')
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
            'special_request_offering',
        ).prefetch_related('activities__actor')

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        data = response.data
        data['hotel_slug'] = self.get_object().hotel.slug
        return response


# ---------------------------------------------------------------------------
# Guest Invite — Admin endpoints
# ---------------------------------------------------------------------------

class GuestInviteListCreate(HotelScopedMixin, generics.ListCreateAPIView):
    """
    GET  — List sent invites (paginated, filterable by status/delivery_status)
    POST — Send a new WhatsApp invite
    """
    permission_classes = [IsAdminOrAbove]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SendInviteSerializer
        return GuestInviteSerializer

    def get_queryset(self):
        from django.db.models import OuterRef, Subquery

        hotel = self.get_hotel()
        qs = (
            GuestInvite.objects
            .filter(hotel=hotel)
            .select_related('sent_by')
            .order_by('-created_at')
        )

        # Annotate latest delivery status + error via Subquery
        latest_delivery_base = (
            DeliveryRecord.objects
            .filter(guest_invite=OuterRef('pk'))
            .order_by('-created_at')
        )
        qs = qs.annotate(
            delivery_status=Subquery(latest_delivery_base.values('status')[:1]),
            delivery_error=Subquery(latest_delivery_base.values('error_message')[:1]),
        )

        # Filters
        invite_status = self.request.query_params.get('status')
        if invite_status:
            qs = qs.filter(status=invite_status)
        delivery_status_filter = self.request.query_params.get('delivery_status')
        if delivery_status_filter:
            qs = qs.filter(delivery_status=delivery_status_filter)

        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        hotel = self.get_hotel()

        # Safety: DRF pagination wraps results in a dict ({"count":…, "results":…}).
        # If pagination is disabled, response.data is a list — don't augment.
        if not isinstance(response.data, dict):
            return response

        # Feature flags — lets ADMIN users know state without needing SUPERADMIN settings endpoint
        response.data['guest_invite_enabled'] = hotel.guest_invite_enabled
        response.data['whatsapp_notifications_enabled'] = hotel.whatsapp_notifications_enabled

        # Embed wa_template in list response for frontend preview
        wa_template = get_template(hotel, 'GUEST_INVITE')
        if wa_template and wa_template.gupshup_template_id:
            response.data['wa_template'] = {
                'body_text': wa_template.body_text,
                'footer_text': wa_template.footer_text,
                'buttons': wa_template.buttons,
                'variables': wa_template.variables,
            }
        else:
            response.data['wa_template'] = None
        return response

    def create(self, request, *args, **kwargs):
        from .notifications.tasks import send_guest_invite_whatsapp

        hotel = self.get_hotel()

        # Guard: features must be enabled
        if not hotel.guest_invite_enabled or not hotel.whatsapp_notifications_enabled:
            return Response(
                {'detail': 'WhatsApp guest invites are not enabled for this hotel.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Preflight: template must be configured
        wa_template = get_template(hotel, 'GUEST_INVITE')
        if not wa_template or not wa_template.gupshup_template_id:
            return Response(
                {'detail': 'WhatsApp invite template not configured.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        serializer = SendInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone = serializer.validated_data['phone']
        guest_name = serializer.validated_data['guest_name']
        room_number = serializer.validated_data.get('room_number', '')

        # Rate limiting
        if not check_invite_rate_limit_staff(request.user.id):
            return Response(
                {'detail': 'Please wait a few seconds before sending another invite.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={'Retry-After': '10'},
            )
        if not check_invite_rate_limit_phone(phone):
            return Response(
                {'detail': 'Too many invites to this number. Try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={'Retry-After': '3600'},
            )
        if not check_invite_rate_limit_hotel(hotel.id):
            return Response(
                {'detail': 'Hotel invite limit reached. Try again later.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={'Retry-After': '3600'},
            )

        try:
            with transaction.atomic():
                # Expire stale PENDING invites for this phone
                GuestInvite.objects.filter(
                    hotel=hotel, guest_phone=phone, status='PENDING',
                    expires_at__lt=timezone.now(),
                ).update(status='EXPIRED')

                expiry = timezone.now() + timedelta(
                    hours=settings.GUEST_INVITE_EXPIRY_HOURS,
                )
                invite = GuestInvite.objects.create(
                    hotel=hotel,
                    sent_by=request.user,
                    guest_phone=phone,
                    guest_name=guest_name,
                    room_number=room_number,
                    expires_at=expiry,
                )
                delivery = DeliveryRecord.objects.create(
                    hotel=hotel,
                    guest_invite=invite,
                    channel='WHATSAPP',
                    target=phone,
                    event_type='guest_invite',
                    message_type='TEMPLATE',
                    status=DeliveryRecord.Status.QUEUED,
                )

                # Enqueue after commit to prevent orphan tasks on rollback
                delivery_id = delivery.id
                transaction.on_commit(
                    lambda: send_guest_invite_whatsapp.delay(delivery_id)
                )
        except IntegrityError:
            # Check if a duplicate active invite actually exists
            duplicate = GuestInvite.objects.filter(
                hotel=hotel, guest_phone=phone, status='PENDING',
            ).exists()
            if duplicate:
                return Response(
                    {'detail': 'An invite was already sent to this number.'},
                    status=status.HTTP_409_CONFLICT,
                )
            raise  # Not a duplicate — re-raise

        out = GuestInviteSerializer(invite).data
        return Response(out, status=status.HTTP_201_CREATED)


class GuestInviteResend(HotelScopedMixin, APIView):
    """POST — Resend a PENDING invite (extends expiry, new delivery)."""
    permission_classes = [IsAdminOrAbove]

    def post(self, request, hotel_slug, pk):
        from .notifications.tasks import send_guest_invite_whatsapp

        hotel = self.get_hotel()

        # Guard: features must be enabled
        if not hotel.guest_invite_enabled or not hotel.whatsapp_notifications_enabled:
            return Response(
                {'detail': 'WhatsApp guest invites are not enabled for this hotel.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Preflight: template must be configured
        wa_template = get_template(hotel, 'GUEST_INVITE')
        if not wa_template or not wa_template.gupshup_template_id:
            return Response(
                {'detail': 'WhatsApp invite template not configured.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic():
            try:
                invite = (
                    GuestInvite.objects
                    .select_for_update()
                    .get(pk=pk, hotel=hotel)
                )
            except GuestInvite.DoesNotExist:
                raise NotFound

            if invite.status != 'PENDING':
                return Response(
                    {'detail': 'Only pending invites can be resent.'},
                    status=status.HTTP_409_CONFLICT,
                )

            # Rate limiting: staff debounce prevents spam-clicking resend.
            # Uses a separate cache key from create to avoid cross-contamination.
            # Checked after status validation so invalid resends get 409 not 429.
            if not check_invite_resend_rate_limit(request.user.id):
                return Response(
                    {'detail': 'Please wait a few seconds before resending.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={'Retry-After': '10'},
                )

            # Extend expiry but do NOT bump token_version. Old signed links
            # remain valid so the guest isn't locked out if the new async
            # send fails. Revocation (status=EXPIRED) is the correct way
            # to invalidate all links, not resend.
            invite.expires_at = timezone.now() + timedelta(
                hours=settings.GUEST_INVITE_EXPIRY_HOURS,
            )
            invite.save(update_fields=['expires_at'])

            delivery = DeliveryRecord.objects.create(
                hotel=hotel,
                guest_invite=invite,
                channel='WHATSAPP',
                target=invite.guest_phone,
                event_type='guest_invite',
                message_type='TEMPLATE',
                status=DeliveryRecord.Status.QUEUED,
            )

            delivery_id = delivery.id
            transaction.on_commit(
                lambda: send_guest_invite_whatsapp.delay(delivery_id)
            )

        out = GuestInviteSerializer(invite).data
        return Response(out)


class GuestInviteRevoke(HotelScopedMixin, APIView):
    """DELETE — Revoke a PENDING invite (marks as EXPIRED)."""
    permission_classes = [IsAdminOrAbove]

    def delete(self, request, hotel_slug, pk):
        hotel = self.get_hotel()

        with transaction.atomic():
            try:
                invite = (
                    GuestInvite.objects
                    .select_for_update()
                    .get(pk=pk, hotel=hotel)
                )
            except GuestInvite.DoesNotExist:
                raise NotFound

            if invite.status != 'PENDING':
                return Response(
                    {'detail': 'Only pending invites can be revoked.'},
                    status=status.HTTP_409_CONFLICT,
                )

            invite.status = 'EXPIRED'
            invite.save(update_fields=['status'])

        return Response(status=status.HTTP_204_NO_CONTENT)


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


# ---------------------------------------------------------------------------
# Guest Ratings — Guest-Facing Views
# ---------------------------------------------------------------------------

# Guest-visible statuses (internal states filtered out)
_GUEST_VISIBLE_STATUSES = {'SENT', 'COMPLETED', 'DISMISSED', 'EXPIRED'}
# Actionable statuses — the only ones a guest can still act on
_GUEST_ACTIONABLE_STATUSES = {'SENT'}


class MyRatingPromptList(APIView):
    """GET /me/rating-prompts/?hotel={slug}&status=&count_only="""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        hotel_slug = request.query_params.get('hotel')
        if not hotel_slug:
            return Response(
                {'detail': 'hotel query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Default to actionable prompts only; allow explicit status override
        # for history views.
        status_filter = request.query_params.get('status')
        if status_filter and status_filter in _GUEST_VISIBLE_STATUSES:
            filter_statuses = {status_filter}
        else:
            filter_statuses = _GUEST_ACTIONABLE_STATUSES

        base_qs = RatingPrompt.objects.filter(
            guest=request.user,
            hotel__slug=hotel_slug,
            status__in=filter_statuses,
        )

        count_only = request.query_params.get('count_only', '').lower() == 'true'
        if count_only:
            count = base_qs.filter(status='SENT').count()
            return Response({'count': count})

        request_prompts = base_qs.filter(
            prompt_type='REQUEST',
        ).select_related(
            'service_request__department',
            'service_request__experience',
            'service_request__event',
            'service_request__special_request_offering',
        ).order_by('-eligible_at')

        # Only one stay prompt surfaced per the singular response contract.
        # If multiple exist, the most recently eligible one wins.
        stay_prompt = base_qs.filter(
            prompt_type='STAY',
        ).order_by('-eligible_at').first()

        return Response({
            'request_prompts': RatingPromptSerializer(request_prompts, many=True).data,
            'stay_prompt': StayPromptSerializer(stay_prompt).data if stay_prompt else None,
        })


class RatePrompt(APIView):
    """POST /me/rating-prompts/{prompt_id}/rate/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, prompt_id):
        ser = SubmitRatingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        from .services import submit_rating
        try:
            rating, created = submit_rating(
                prompt_id=prompt_id,
                guest=request.user,
                score=ser.validated_data['score'],
                feedback=ser.validated_data.get('feedback', ''),
            )
        except serializers.ValidationError as e:
            return Response(
                {'detail': e.detail if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            RatingSerializer(rating).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class DismissPrompt(APIView):
    """POST /me/rating-prompts/{prompt_id}/dismiss/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, prompt_id):
        with transaction.atomic():
            try:
                prompt = RatingPrompt.objects.select_for_update().get(
                    id=prompt_id,
                    guest=request.user,
                    status='SENT',
                )
            except RatingPrompt.DoesNotExist:
                return Response(
                    {'detail': 'Rating prompt not found or already handled.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            prompt.status = 'DISMISSED'
            prompt.dismissed_at = timezone.now()
            prompt.save(update_fields=['status', 'dismissed_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)


class ReviewClicked(APIView):
    """POST /me/ratings/{rating_id}/review-clicked/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, rating_id):
        try:
            rating = Rating.objects.get(
                id=rating_id,
                guest=request.user,
            )
        except Rating.DoesNotExist:
            return Response(
                {'detail': 'Rating not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not rating.review_clicked_at:
            rating.review_clicked_at = timezone.now()
            rating.save(update_fields=['review_clicked_at'])

        return Response(status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Guest Ratings — Admin Views
# ---------------------------------------------------------------------------

class AdminSendSurvey(HotelScopedMixin, APIView):
    """POST /hotels/{slug}/admin/stays/{stay_id}/send-survey/"""
    permission_classes = [IsAdminOrAbove]

    def post(self, request, hotel_slug, stay_id):
        hotel = self.get_hotel()
        if not hotel.ratings_enabled:
            return Response(
                {'detail': 'Ratings are not enabled for this hotel.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            guest_stay = GuestStay.objects.get(
                id=stay_id, hotel=hotel,
            )
        except GuestStay.DoesNotExist:
            return Response(
                {'detail': 'Stay not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        from .services import send_stay_survey
        try:
            send_stay_survey(
                hotel=hotel,
                guest_stay=guest_stay,
                triggered_by=request.user,
            )
        except serializers.ValidationError as e:
            return Response(
                {'detail': e.detail if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {'detail': 'Checkout survey sent.'},
            status=status.HTTP_201_CREATED,
        )


class AdminRatingList(HotelScopedMixin, generics.ListAPIView):
    """GET /hotels/{slug}/admin/ratings/"""
    permission_classes = [IsAdminOrAbove]
    serializer_class = AdminRatingSerializer

    def get_queryset(self):
        hotel = self.get_hotel()
        qs = Rating.objects.filter(
            hotel=hotel,
        ).select_related(
            'guest', 'guest_stay', 'service_request__department',
            'service_request__guest_stay',
        ).order_by('-created_at')

        rating_type = self.request.query_params.get('rating_type')
        if rating_type in ('REQUEST', 'STAY'):
            qs = qs.filter(rating_type=rating_type)

        # Exact score filter (from frontend score dropdown)
        score = self.request.query_params.get('score')
        if score and score.isdigit() and 1 <= int(score) <= 5:
            qs = qs.filter(score=int(score))

        # Range filters (for programmatic use)
        min_score = self.request.query_params.get('min_score')
        if min_score and min_score.isdigit():
            qs = qs.filter(score__gte=int(min_score))

        max_score = self.request.query_params.get('max_score')
        if max_score and max_score.isdigit():
            qs = qs.filter(score__lte=int(max_score))

        department = self.request.query_params.get('department')
        if department:
            qs = qs.filter(service_request__department__slug=department)

        return qs


class AdminRatingSummary(HotelScopedMixin, APIView):
    """GET /hotels/{slug}/admin/ratings/summary/"""
    permission_classes = [IsAdminOrAbove]

    def get(self, request, hotel_slug):
        hotel = self.get_hotel()
        qs = Rating.objects.filter(hotel=hotel)

        total = qs.count()
        avg = qs.aggregate(avg=models.Avg('score'))['avg']

        # Score distribution
        dist_qs = qs.values('score').annotate(count=Count('id')).order_by('score')
        distribution = {str(i): 0 for i in range(1, 6)}
        for row in dist_qs:
            distribution[str(row['score'])] = row['count']

        # By department (request ratings only) — group by id for uniqueness
        by_dept = (
            qs.filter(rating_type='REQUEST', service_request__department__isnull=False)
            .values('service_request__department__id', 'service_request__department__name')
            .annotate(
                count=Count('id'),
                avg_score=models.Avg('score'),
            )
            .order_by('-avg_score')
        )
        dept_data = [
            {
                'department_id': row['service_request__department__id'],
                'department_name': row['service_request__department__name'],
                'count': row['count'],
                'avg_score': round(row['avg_score'], 1) if row['avg_score'] else None,
            }
            for row in by_dept
        ]

        return Response({
            'total_count': total,
            'average_score': round(avg, 2) if avg else None,
            'distribution': distribution,
            'by_department': dept_data,
        })
