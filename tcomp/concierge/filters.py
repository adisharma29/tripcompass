import django_filters

from .models import ServiceRequest


class ServiceRequestFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=ServiceRequest.Status.choices)
    department = django_filters.NumberFilter(field_name='department_id')
    request_type = django_filters.ChoiceFilter(choices=ServiceRequest.RequestType.choices)
    after_hours = django_filters.BooleanFilter()
    created_after = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_before = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')

    class Meta:
        model = ServiceRequest
        fields = ['status', 'department', 'request_type', 'after_hours']
