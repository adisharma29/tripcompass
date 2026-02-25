from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import BasePermission

from .models import Hotel, HotelMembership, GuestStay


def get_membership(user, hotel_slug):
    """Look up active membership for user at hotel identified by slug."""
    if not user or not user.is_authenticated:
        return None
    try:
        return HotelMembership.objects.select_related('department').get(
            user=user,
            hotel__slug=hotel_slug,
            hotel__is_active=True,
            is_active=True,
        )
    except HotelMembership.DoesNotExist:
        return None


def get_membership_by_hotel(user, hotel):
    """Look up active membership for user at a given hotel object."""
    if not user or not user.is_authenticated:
        return None
    try:
        return HotelMembership.objects.select_related('department').get(
            user=user,
            hotel=hotel,
            is_active=True,
        )
    except HotelMembership.DoesNotExist:
        return None


class IsHotelMember(BasePermission):
    """User has any active membership for the hotel in URL."""

    def has_permission(self, request, view):
        hotel_slug = view.kwargs.get('hotel_slug')
        if not hotel_slug:
            return False
        membership = get_membership(request.user, hotel_slug)
        if membership:
            request.membership = membership
            return True
        return False


class IsStaffOrAbove(BasePermission):
    """User has STAFF, ADMIN, or SUPERADMIN role."""

    def has_permission(self, request, view):
        hotel_slug = view.kwargs.get('hotel_slug')
        if not hotel_slug:
            return False
        membership = get_membership(request.user, hotel_slug)
        if membership and membership.role in (
            HotelMembership.Role.STAFF,
            HotelMembership.Role.ADMIN,
            HotelMembership.Role.SUPERADMIN,
        ):
            request.membership = membership
            return True
        return False


class IsAdminOrAbove(BasePermission):
    """User has ADMIN or SUPERADMIN role."""

    def has_permission(self, request, view):
        hotel_slug = view.kwargs.get('hotel_slug')
        if not hotel_slug:
            return False
        membership = get_membership(request.user, hotel_slug)
        if membership and membership.role in (
            HotelMembership.Role.ADMIN,
            HotelMembership.Role.SUPERADMIN,
        ):
            request.membership = membership
            return True
        return False


class IsSuperAdmin(BasePermission):
    """User has SUPERADMIN role for this hotel."""

    def has_permission(self, request, view):
        hotel_slug = view.kwargs.get('hotel_slug')
        if not hotel_slug:
            return False
        membership = get_membership(request.user, hotel_slug)
        if membership and membership.role == HotelMembership.Role.SUPERADMIN:
            request.membership = membership
            return True
        return False


class CanAccessRequestObject(BasePermission):
    """Object-level check for ServiceRequest (hotel-scoped routes).
    Derives hotel from URL `hotel_slug`.
    STAFF: request.department must equal membership.department.
    ADMIN/SUPERADMIN: any department within the same hotel."""

    def has_object_permission(self, request, view, obj):
        membership = getattr(request, 'membership', None)
        if not membership:
            hotel_slug = view.kwargs.get('hotel_slug')
            membership = get_membership(request.user, hotel_slug)
            if not membership:
                return False

        if obj.hotel != membership.hotel:
            return False

        if membership.role == HotelMembership.Role.STAFF:
            return obj.department == membership.department

        # ADMIN, SUPERADMIN can access any department in their hotel
        return True


class CanAccessRequestObjectByLookup(BasePermission):
    """Object-level check for ServiceRequest on slug-less routes
    (e.g. /me/requests/{public_id}/).
    Derives hotel from request_obj.hotel instead of URL kwargs."""

    def has_object_permission(self, request, view, obj):
        # Allow guest to view their own request
        if (request.user.user_type == 'GUEST'
                and obj.guest_stay
                and obj.guest_stay.guest == request.user):
            return True

        membership = get_membership_by_hotel(request.user, obj.hotel)
        if not membership:
            return False

        if membership.role == HotelMembership.Role.STAFF:
            return obj.department == membership.department

        return True


class IsActiveGuest(BasePermission):
    """User is authenticated, user_type=GUEST, and has an active
    non-expired GuestStay for the hotel in URL."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.user_type != 'GUEST':
            return False

        hotel_slug = view.kwargs.get('hotel_slug')
        if not hotel_slug:
            return False

        now = timezone.now()
        has_active_stay = GuestStay.objects.filter(
            guest=user,
            hotel__slug=hotel_slug,
            hotel__is_active=True,
            is_active=True,
            expires_at__gt=now,
        ).exists()

        return has_active_stay


class IsStayOwner(BasePermission):
    """Object-level check for GuestStay. Ensures the authenticated
    guest owns the stay being accessed."""

    def has_object_permission(self, request, view, obj):
        return obj.guest == request.user
