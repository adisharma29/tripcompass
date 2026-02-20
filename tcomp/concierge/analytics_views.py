"""Analytics API views for the concierge dashboard."""

import zoneinfo

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .analytics import (
    _parse_date_range,
    get_department_stats,
    get_experience_stats,
    get_heatmap_data,
    get_overview_stats,
    get_qr_placement_stats,
    get_requests_over_time,
    get_response_times,
)
from .mixins import HotelScopedMixin
from .models import HotelMembership
from .permissions import IsStaffOrAbove


class AnalyticsBaseView(HotelScopedMixin, APIView):
    """Base view that parses date range and extracts department filter."""
    permission_classes = [IsStaffOrAbove]

    def get_analytics_context(self, request):
        """Returns (hotel, hotel_tz, start_dt, end_dt, department, error_response).
        If error_response is not None, return it immediately."""
        hotel = self.get_hotel()
        hotel_tz = zoneinfo.ZoneInfo(hotel.timezone)

        start_dt, end_dt, err = _parse_date_range(request.query_params, hotel_tz)
        if err:
            return None, None, None, None, None, Response(
                {'detail': err}, status=status.HTTP_400_BAD_REQUEST,
            )

        membership = getattr(request, 'membership', None)
        department = None
        if membership and membership.role == HotelMembership.Role.STAFF:
            department = membership.department
            if department is None:
                return None, None, None, None, None, Response(
                    {'detail': 'Staff member has no department assigned.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        return hotel, hotel_tz, start_dt, end_dt, department, None


class AnalyticsOverview(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err
        data = get_overview_stats(hotel, start_dt, end_dt, department)
        return Response(data)


class AnalyticsRequestsOverTime(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err
        data = get_requests_over_time(hotel, start_dt, end_dt, department)
        return Response(data)


class AnalyticsDepartments(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err

        # STAFF cannot access department-level breakdown (hotel-wide only)
        membership = getattr(request, 'membership', None)
        if membership and membership.role == HotelMembership.Role.STAFF:
            return Response(
                {'detail': 'Department breakdown is not available for staff role.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = get_department_stats(hotel, start_dt, end_dt)
        return Response(data)


class AnalyticsExperiences(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err
        data = get_experience_stats(hotel, start_dt, end_dt, department)
        return Response(data)


class AnalyticsResponseTimes(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err
        data = get_response_times(hotel, start_dt, end_dt, department)
        return Response(data)


class AnalyticsHeatmap(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err
        data = get_heatmap_data(hotel, start_dt, end_dt, department)
        return Response(data)


class AnalyticsQRPlacements(AnalyticsBaseView):
    def get(self, request, **kwargs):
        hotel, hotel_tz, start_dt, end_dt, department, err = self.get_analytics_context(request)
        if err:
            return err

        # STAFF cannot access QR placement stats (hotel-wide only)
        membership = getattr(request, 'membership', None)
        if membership and membership.role == HotelMembership.Role.STAFF:
            return Response(
                {'detail': 'QR placement stats are not available for staff role.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = get_qr_placement_stats(hotel, start_dt, end_dt)
        return Response(data)
