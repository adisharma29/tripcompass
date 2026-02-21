from django.urls import path

from .views import (
    AuthProfileView,
    CookieTokenObtainPairView,
    CookieTokenRefreshView,
    CSRFTokenView,
    LogoutView,
    OTPSendView,
    OTPVerifyView,
)

urlpatterns = [
    path('csrf/', CSRFTokenView.as_view(), name='csrf-token'),
    path('token/', CookieTokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('token/refresh/', CookieTokenRefreshView.as_view(), name='token-refresh'),
    path('otp/send/', OTPSendView.as_view(), name='otp-send'),
    path('otp/verify/', OTPVerifyView.as_view(), name='otp-verify'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', AuthProfileView.as_view(), name='auth-profile'),
]
