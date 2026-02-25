from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """Allow clients to override page size via ?page_size=N (capped at 100)."""
    page_size_query_param = 'page_size'
    max_page_size = 100
