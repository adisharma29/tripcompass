# Concierge v1 — Implementation Progress

## Phase 1: Backend Foundation
Status: **COMPLETE**

### Steps
- [x] 1. Install new Python packages (celery, redis, django-filter, etc.)
- [x] 2. Update `.env.example` with all required vars (Redis, Gupshup, VAPID, etc.)
- [x] 3. Update `users/models.py` — add `user_type` (STAFF/GUEST) + partial unique indexes on email and phone
- [x] 4. Update `users/managers.py` — add `create_guest_user()` method
- [x] 5. Create `concierge` app with all 12 models (Hotel, HotelMembership, Department, Experience, GuestStay, OTPCode, ServiceRequest, RequestActivity, Notification, PushSubscription, QRCode, EscalationHeartbeat)
- [x] 6. Update settings (`base.py` — Celery, Redis cache, JWT cookies, Gupshup, VAPID, SSE, rate limits, upload limits; `dev.py` — Bearer fallback, CSRF origins; `prod.py` — cookie domain for refuje.com)
- [x] 7. Create Celery config (`celery.py` + `__init__.py` with DB connection signals)
- [x] 8. Create no-store middleware (`tcomp/middleware.py`)
- [x] 9. Update `docker-compose.yml` — added Redis, celery-worker, celery-beat services
- [x] 10. Run `makemigrations` + `migrate` — all applied successfully
- [x] 11. Write `concierge/authentication.py` — JWTCookieAuthentication
- [x] 12. Write `concierge/validators.py` — image upload validation (magic bytes, size, EXIF strip, resize)
- [x] 13. Write `concierge/mixins.py` — HotelScopedMixin
- [x] 14. Write `concierge/permissions.py` — 8 permission classes (IsHotelMember, IsStaffOrAbove, IsAdminOrAbove, IsSuperAdmin, CanAccessRequestObject, CanAccessRequestObjectByLookup, IsActiveGuest, IsStayOwner)
- [x] 15. Write `concierge/serializers.py` — all serializers (public, admin, guest, request CRUD, state machine validation, notifications)
- [x] 16. Write `concierge/views.py` + `concierge/urls.py` — all views and URL routing
- [x] 17. Write `concierge/services.py` — QR gen, OTP (WhatsApp + SMS fallback), rate limiting with DB fallback, push notifications, escalation, SSE pub/sub, dashboard stats
- [x] 18. Write `concierge/filters.py` — ServiceRequestFilter
- [x] 19. Write `concierge/signals.py` — placeholder (activity logging done explicitly in views)
- [x] 20. Write `concierge/tasks.py` — 6 Celery tasks (escalation, expiry, reminder, OTP sweep, OTP cleanup, digest)
- [x] 21. Register models in `concierge/admin.py` — with EscalationHeartbeat health display
- [x] 22. Add serializers to users app (AuthProfile, AuthProfileUpdate, OTPSend, OTPVerify, UserMinimal)
- [x] 23. Write cookie-based auth views (`CSRFTokenView`, `CookieTokenObtainPairView`, `CookieTokenRefreshView`, `LogoutView`, `OTPSendView`, `OTPVerifyView`, `AuthProfileView`)
- [x] 24. Update `users/urls.py` — csrf, token, refresh, otp/send, otp/verify, logout, profile
- [x] 25. Update `tcomp/urls.py` — mount concierge routes at `api/v1/`
- [x] 26. Smoke test — server starts, admin accessible, all imports work, CSRF endpoint returns no-store header

### Verification Results
- Docker build: OK
- `manage.py check`: no issues (1 silenced — auth.E003 for partial unique email)
- `manage.py migrate`: all 30+ migrations applied
- All model imports: OK
- All view imports: OK
- Celery app configured: OK
- `GET /api/v1/auth/csrf/`: 200, sets csrftoken cookie, Cache-Control: no-store
- `GET /api/v1/auth/profile/`: 401 (correct — no auth)
- Admin panel: 200

### Post-Review Fixes
1. **select_for_update() outside transaction** — wrapped `ServiceRequestAcknowledge.post()` in `transaction.atomic()`
2. **OTP SMS fallback non-functional** — `send_sms_fallback_for_otp()` now generates new code, calls `_send_sms_otp()`, only marks sent on success; sync path checks return value
3. **OTP rate limiting bypass when Redis down** — created `check_otp_rate_limit_phone()` and `check_otp_rate_limit_ip()` with DB fallback (counts OTPCode records)
4. **OTP verification not hotel-scoped** — `verify_otp()` now strictly filters by hotel when provided (no null fallback)
5. **Staff created without password** — explicit `User()` construction with `set_unusable_password()` in member creation
6. **docker-compose.prod.yml** — added Redis, celery-worker, celery-beat services with proper Redis URL env vars
7. **Redis eviction policy** — changed from `allkeys-lru` to `noeviction` in both dev and prod compose (prevents silent Celery task loss)
8. **celery-beat migration race** — celery-worker and celery-beat now depend on web service healthcheck (TCP port check, passes only after migrations + uvicorn startup)
9. **OTP fallback invalidated code before SMS delivery** — reordered `send_sms_fallback_for_otp()` to send SMS first, only update `code_hash` on success
10. **SSE stream sync ORM/Redis in async context** — rewrote `stream_request_events()` with `redis.asyncio`, `sync_to_async` for ORM, pre-fetched membership once
11. **confirmation_reason without status change** — added validation rejecting reason when no status transition is provided
12. **OTP verify leaking internal error details** — catch only `ValidationError` for controlled messages, generic `Exception` returns opaque message
13. **Duplicate get_queryset in ServiceRequestList** — removed dead code (second definition that shadowed the first)
14. **Celery beat tasks never scheduled** — added `CELERY_BEAT_SCHEDULE` dict to settings; `DatabaseScheduler` syncs these to DB on first startup
15. **Staff created with no login path** — made `phone` required in `MemberCreateSerializer`; always set on new user creation
16. **Guest user not promoted on membership creation** — existing GUEST users looked up by email are now promoted to `user_type='STAFF'` and get phone set if missing
17. **ServiceRequestList missing queryset attribute** — added `queryset = ServiceRequest.objects.all()` so `super().get_queryset()` chain through `HotelScopedMixin` resolves
18. **Refresh token not checking is_active** — added `is_active` check after user lookup, returns 401 for deactivated accounts
19. **Email login 500 on blank email** — early return for empty email; catch `MultipleObjectsReturned` alongside `DoesNotExist`
20. **Member invite IntegrityError on duplicate phone/email** — wrapped `user.save()` in both create and update paths with `IntegrityError` catch returning 400
21. **Cookie JWT auth missing CSRF** — `JWTCookieAuthentication` now enforces CSRF via Django's `CsrfViewMiddleware` on cookie-authenticated requests (same pattern as DRF `SessionAuthentication`)
22. **OTP send returns 200 when both channels fail** — `send_otp()` now raises `OTPDeliveryError` on total failure; view returns 502; orphan OTP record is deleted
23. **OTP marked used before hotel validation** — moved `is_used=True` after staff lookup and hotel precondition check; OTP preserved for retry if `hotel_slug` missing
24. **Refresh token rotation not revoking old tokens** — added `rest_framework_simplejwt.token_blacklist` to INSTALLED_APPS, set `BLACKLIST_AFTER_ROTATION: True`, removed `try/except AttributeError` guard
25. **Failed OTP delete weakens DB rate limit** — keep the OTP row on delivery failure (mark `is_used=True` instead of deleting) so DB-fallback rate limiting still counts it
26. **Stale access_token cookie breaks AllowAny views** — `JWTCookieAuthentication` now catches `InvalidToken`/`TokenError` and returns `None` (fall-through) instead of raising; CSRF only enforced on valid tokens
27. **OTP verify race condition** — wrapped OTP lookup + mark-used in `transaction.atomic()` with `select_for_update()`; concurrent requests block on the row lock and see `is_used=True`
28. **Guest user creation race on unique phone** — `create_guest_user()` now wrapped in `IntegrityError` catch with retry `get()` for the concurrent-create case
29. **SMS fallback non-atomic state update** — `_send_sms_otp()` no longer saves `channel`/`sms_fallback_sent` itself; callers (`send_otp`, `send_sms_fallback_for_otp`) do a single save with all fields (`code_hash`, `channel`, `sms_fallback_sent`) together
30. **Webhook auth bypass when secret unconfigured** — reject with 403 when `GUPSHUP_WA_WEBHOOK_SECRET` is empty; also return 403 (not 200) on invalid signature
31. **Logout doesn't blacklist refresh token** — `LogoutView` now calls `token.blacklist()` on the refresh cookie before clearing cookies
32. **OTP uses non-cryptographic PRNG** — `generate_otp_code()` switched from `random.choices()` to `secrets.choice()` (CSPRNG)
33. **OTP auth doesn't check is_active** — both staff and guest paths in `verify_otp()` now reject inactive users with "Account is disabled."
34. **Service request SLA fields not populated** — added `is_department_after_hours()` and `compute_response_due_at()` helpers; `ServiceRequestCreate` now sets `after_hours` and `response_due_at` at creation time
35. **Member onboarding ignores phone-only users** — lookup now tries email first, falls back to phone; promotes existing guest accounts to staff and backfills missing email/phone
36. **Sync SMS fallback crash window** — mark `sms_fallback_sent=True` BEFORE sending so sweeper won't touch the row if process crashes after delivery; revert flags on send failure
37. **Guest inactive check consumes OTP** — moved guest `is_active` check inside atomic block before `is_used=True`; disabled guest rejects without burning the OTP
38. **Misleading "sweeper can retry" comment** — fixed: both-channels-fail path sets `is_used=True` which prevents sweeper retry; updated comment to reflect that user must request a new OTP
39. **REMOTE_ADDR is proxy IP behind Traefik** — added `--proxy-headers --forwarded-allow-ips='*'` to uvicorn in prod compose; uvicorn now sets REMOTE_ADDR from X-Forwarded-For when request comes through trusted proxy (safe because only Traefik can reach the container)
40. **After-hours overnight windows across day boundaries** — extracted `_is_in_windows()` helper; `is_department_after_hours()` now also checks previous day's overnight windows for the after-midnight portion
41. **Sweeper SMS fallback crash-before-send** — replaced pre-mark pattern with claim pattern: sweeper sets `sms_fallback_claimed_at` atomically, sends SMS outside the lock, marks `sms_fallback_sent=True` on success; stale claims (>60s) are retryable; added `sms_fallback_claimed_at` field + migration
42. **Webhook bypasses claim guard** — `handle_wa_delivery_event()` now uses the same claim pattern as the sweeper (`sms_fallback_claimed_at` check + atomic claim before send); prevents sweeper+webhook racing and sending conflicting SMS codes
43. **Tighten forwarded-allow-ips** — changed from `'*'` to RFC 1918 private ranges (`172.16.0.0/12,192.168.0.0/16,10.0.0.0/8`); configurable via `TRUSTED_PROXY_IPS` env var

### Dependency Note
- `redis>=7.2.0` from plan section 1.10 is incompatible with `celery[redis]>=5.6.2` (celery pins redis<6). Removed explicit `redis` pin — celery manages its own redis dependency.

## Phase 2: Frontend Auth + Dashboard Shell
Status: **NOT STARTED** (backend only for now)

## Phase 3: Frontend — Admin CRUD
Status: **NOT STARTED**

## Phase 4: Frontend — Guest-Facing
Status: **NOT STARTED**

## Phase 5: Notifications + PWA
Status: **NOT STARTED**

## Phase 6: Polish
Status: **NOT STARTED**
