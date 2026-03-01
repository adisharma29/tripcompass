from django.urls import path

from .views import (
    AuthProfileView,
    CookieTokenObtainPairView,
    CookieTokenRefreshView,
    CSRFTokenView,
    EmailOTPSendView,
    EmailOTPVerifyView,
    ForgotPasswordView,
    LogoutView,
    OTPSendView,
    OTPVerifyView,
    SetPasswordView,
    verify_wa_invite,
)

urlpatterns = [
    path('csrf/', CSRFTokenView.as_view(), name='csrf-token'),
    path('token/', CookieTokenObtainPairView.as_view(), name='token-obtain-pair'),
    path('token/refresh/', CookieTokenRefreshView.as_view(), name='token-refresh'),
    path('otp/send/', OTPSendView.as_view(), name='otp-send'),
    path('otp/verify/', OTPVerifyView.as_view(), name='otp-verify'),
    path('otp/email/send/', EmailOTPSendView.as_view(), name='email-otp-send'),
    path('otp/email/verify/', EmailOTPVerifyView.as_view(), name='email-otp-verify'),
    path('set-password/', SetPasswordView.as_view(), name='set-password'),
    path('forgot-password/', ForgotPasswordView.as_view(), name='forgot-password'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', AuthProfileView.as_view(), name='auth-profile'),
    path('wa-invite/<str:token>/', verify_wa_invite, name='wa-invite-verify'),
]
