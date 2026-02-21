# Plan: Hotel Concierge Platform v1 — Full-Stack Implementation

## v4 Update Note (2026-02-18)

This file is an implementation-aligned update of `v1_implementation_plan_FIXED_v3_patched.md`.
The original v3 file is intentionally preserved unchanged.
v4 aligns endpoint paths, OTP fallback race-handling, proxy/IP trust, webhook signature behavior,
CSRF semantics for OTP endpoints, SSE event expectations, and production deployment defaults.

## Deployment assumptions (v1 pilots)

- **Canonical prod topology (split-subdomain):** Frontend on `refuje.com` (Cloudflare Workers via OpenNext), API on `api.refuje.com` (Docker + reverse proxy). Same apex domain → `SameSite=Lax` cookies work cross-subdomain. Requires `CSRF_COOKIE_DOMAIN = '.refuje.com'`, `CORS_ALLOW_CREDENTIALS = True`, and Safari iOS `SameSite` validation. See S11 for full settings.
- **Alternative (single-origin):** Serve Dashboard and API under the same origin via reverse proxy (e.g. `https://{hotel}.refuje.com/api/`). Simpler cookie behavior (no cross-subdomain), but requires proxy routing and is not the default OpenNext Worker + Docker API layout.
- **No caching on auth/session endpoints:** `/auth/csrf/`, `/auth/token/`, `/auth/token/refresh/`, `/auth/logout/`, `/auth/profile/`, `/auth/otp/send/`, `/auth/otp/verify/` must return `Cache-Control: no-store` (enforced via `NO_STORE_PATHS` middleware). The following dynamic-path endpoints also return `Cache-Control: no-store` (set directly in view): `GET /me/requests/{public_id}/` (authenticated resolver).


## Context

The PRDs (v3.1) are finalized. The next step is building the actual v1 platform — a full-stack web app (PWA) with a Django backend and Next.js frontend. No WhatsApp request channel in v1 (guests submit requests through the web platform only; WhatsApp is used for OTP delivery, not as a request intake channel). The GM is superadmin for their hotel, can add departments/staff, and guests interact through a hotel-specific page.

**User decisions:**
- Guest identity: **Phone + OTP** (WhatsApp primary via Gupshup WhatsApp API, SMS fallback via Gupshup Enterprise SMS API). Guest is a real User (user_type=GUEST) — data retained permanently. Same guest recognized across hotels.
- Staff login: **Email + password** or **Phone + OTP** (same OTP flow as guests — staff choose either method on the login page)
- Guest UX: Phone → OTP → room number screen → request form (name + date/time/guests/notes, pre-filled for returning guests)
- Staff dashboard real-time: **SSE** (Server-Sent Events via Redis pub/sub) — no polling
- Guest real-time: Push notifications only (no SSE for guests in v1)
- Repo strategy: Extend existing `tcomp-backend` and `tcomp-frontend` repos
- Notifications: Web Push (PWA) + in-app notification bell
- Platform admin: Hotel-level only (Field Guide team uses Django admin)

---

## Existing Codebase Summary

**Backend** (`tcomp-backend/tcomp/`):
- Django 5.2.11 + DRF 3.16.1 + SimpleJWT + PostGIS
- Apps: `users` (custom User, email-based), `guides` (Destination/Experience/GeoFeature), `location`
- Settings: split base/dev/prod, decouple for env vars
- Auth: JWT (5h access, 7d refresh), Bearer header
- URL prefix: `api/v1/`

**Frontend** (`tcomp-frontend/src/`):
- Next.js 16.1.6 (App Router) + React 19 + Tailwind v4
- Route groups: `(site)/` (marketing), `(fieldguide)/[...slug]` (interactive guide)
- Patterns: `getServerApiUrl()`/`getClientApiUrl()`, DRF pagination unwrap, Context+useReducer
- No auth, no admin, no PWA config

---

## Security Hardening (S1-S14, plus final review hardening)
## Data retention, backups, and audit logs (v1)

- **Data retention:** All data is **retained permanently** in v1 — guest profiles, stay history, request notes, staff notes. No scrubbing, no redaction.
- **Backups:** Define backup cadence (daily) and a simple **restore test** cadence (monthly) for pilot readiness.
- **Audit logs:** Staff action audit logs (ACK, status updates, assignments, revokes) retained permanently for case studies and dispute resolution. `RequestActivity.details` contains only structured non-PII keys (IDs, counts, tier labels) — no free-text content.

### Rate-limit DB fallback performance guardrails

When Redis/cache is unavailable and DB fallback counts are used:
- Ensure indexes match the fallback query patterns (e.g., `(hotel_id, created_at)`, `(guest_stay_id, created_at)`, `(hotel_id, room_number, created_at)`).
- Current backend behavior: run direct DB count fallback queries; there is no coarser-policy fallback tier yet.
- Emit an alert if DB-fallback usage exceeds a threshold (indicates Redis instability) (planned).

**Rate-limit response rule table (canonical):**

| Condition | Response | Notes |
|-----------|----------|-------|
| Limit exceeded (cache or DB) | **429** Too Many Requests | Normal rate limiting — always 429 regardless of infra path |
| DB fallback query slow (> budget) | n/a (not implemented) | No coarser-policy limiter branch in current backend |
| Cache fallback path hits DB error | **5xx** | Current backend does not map this branch to custom 503; DB outage surfaces as server error |

All implemented limiter branches return **429** when limits are exceeded. Infrastructure/database failures currently surface as generic **5xx** responses.


### S1. Staff auth: httpOnly cookies, not localStorage
JWT access token stored in httpOnly secure cookie (not localStorage). Eliminates XSS → account takeover vector. SimpleJWT configured to set cookies via a custom token view. CSRF protection enabled for cookie-based auth. Refresh token also httpOnly, separate cookie path (`/api/v1/auth/token/refresh/`).

**Two staff login methods (staff chooses on login page):**
1. **Email + password** — `POST /auth/token/` (existing `CookieTokenObtainPairView`)
2. **Phone + OTP** — `POST /auth/otp/send/` + `POST /auth/otp/verify/` (same endpoints as guest OTP — `OTPVerifyView` detects `user_type` from existing User record). For staff, OTP verify does **not** create a GuestStay — it just issues JWT cookies for the existing STAFF user.

**Backend:**
- Custom `CookieTokenObtainPairView` and `CookieTokenRefreshView` that set httpOnly cookies
- `OTPVerifyView` handles both user types: if phone matches an existing STAFF user, issues JWT without creating a GuestStay. If phone matches no user or a GUEST user, follows guest flow (creates User if needed + GuestStay). The `hotel_slug` param is required for guests (to create GuestStay) but optional for staff (staff phone lookup is hotel-independent).
- Add `JWTCookieAuthentication` class — reads token from cookie instead of `Authorization` header
- Settings: `SIMPLE_JWT.AUTH_COOKIE = 'access_token'`, `SIMPLE_JWT.AUTH_COOKIE_SECURE = True`, `SIMPLE_JWT.AUTH_COOKIE_SAMESITE = 'Lax'`
- Shorten access token to 30 min (refresh stays 7d, rotation enabled)

**Frontend:**
- No token storage in JS at all — cookies sent automatically with `credentials: 'include'`
- `authFetch()` just adds `credentials: 'include'` to every request
- Login page has two tabs: "Email" (email + password → `/auth/token/`) and "Phone" (phone + OTP → `/auth/otp/send/` + `/auth/otp/verify/`)
- Logout calls `/api/v1/auth/logout/` → backend clears cookies

### S2. Phone OTP authentication (shared by guests and staff)
OTP endpoints are shared — both guests and staff can authenticate via phone + OTP (WhatsApp primary, SMS fallback). The `OTPVerifyView` inspects the phone number to determine behavior:
- **Phone matches existing STAFF user:** issue JWT cookies for that user. No GuestStay created. `hotel_slug` is optional (staff are hotel-independent).
- **Phone matches existing GUEST user or no user:** follow guest flow — create User if needed (`user_type=GUEST`), create `GuestStay`. `hotel_slug` is required.

**OTP flow (guest path):**
1. `POST /auth/otp/send/` — { phone, hotel_slug (optional) } → backend generates 6-digit code, attempts delivery via **WhatsApp first** (Gupshup WhatsApp API), with **SMS fallback** (Gupshup Enterprise SMS API). Stores hashed code + delivery metadata in `OTPCode` (hotel FK set if `hotel_slug` provided, null otherwise). Rate limit: **3 OTPs per phone per hour, 5 per IP per hour.** IP throttling uses `REMOTE_ADDR` (populated from trusted proxy forwarding in prod), not raw `X-Forwarded-For` from client input. **Delivery strategy:** WhatsApp send is attempted first via `POST https://api.gupshup.io/wa/api/v1/template/msg`. If synchronous send fails, fallback is attempted inline. For async/timeouts, fallback uses a **claim pattern** with `sms_fallback_claimed_at`: webhook and sweeper only process unclaimed/stale rows, atomically claim with `select_for_update(skip_locked=True)`, send SMS outside row lock, mark `sms_fallback_sent=True` on success, clear claim on failure, and allow stale-claim retry after timeout. Successfully delivered WhatsApp OTPs (`wa_delivered=True`) are excluded from fallback. **`hotel_slug` resolution:** send endpoint does not look up user; staff-vs-guest branching happens at verify time.
2. `POST /auth/otp/verify/` — { phone, code, hotel_slug (optional), qr_code (optional) } → verifies code, then branches:
   - **Phone matches existing STAFF user:** issue JWT cookies, return user info + memberships. `hotel_slug` ignored, no GuestStay created.
   - **Phone matches existing GUEST user or no user, AND `hotel_slug` provided:** guest flow — create User if new (`user_type=GUEST`, `phone=phone`), create `GuestStay(guest=user, hotel=hotel, qr_code=qr_code)`, set JWT httpOnly cookies. Return user info + stay ID.
   - **Phone matches existing GUEST user or is unknown, AND `hotel_slug` missing:** → **400 Bad Request** (`hotel_slug is required for non-staff users`). Cannot create or resume a guest session without a hotel context.
   Rate limit: **5 wrong attempts per code, then code is invalidated.** **QR validation:** if `qr_code` is provided, resolve by `QRCode.code` field, verify it belongs to `hotel_slug` and `is_active=True` — silently ignore (set null) if invalid or missing, never block verification.
3. `PATCH /hotels/{hotel_slug}/stays/{stay_id}/` — { room_number } → guest enters room number on next screen. Validated against hotel pattern/range/blocklist. (Guest-only — staff skip this step.)

**OTP delivery: WhatsApp primary, SMS fallback (both via Gupshup).**
- **WhatsApp (primary):** Gupshup WhatsApp Business API (`POST https://api.gupshup.io/wa/api/v1/template/msg`). Uses a **utility template** (not authentication template — authentication templates are not available for Indian businesses on Gupshup). Template contains `{{1}}` placeholder for the 6-digit code + a "Copy code" quick-reply button. Auth via `apikey` header. Async delivery — track via webhook events (`enqueued` → `sent` → `delivered`). Failure codes: 1002 = not on WhatsApp, 1008 = not opted in.
- **SMS (fallback):** Gupshup Enterprise SMS API (`POST https://enterprise.smsgupshup.com/GatewayAPI/rest`, `method=TWO_FACTOR_AUTH`, `format=text`). Auth via `userid`/`password`. Pipe-delimited response (`success | phone | txn_id | OTP sent` or `error | code | message`). Used when WhatsApp send fails synchronously, webhook reports async failure, or timeout sweeper fires.
- **Local verification:** We generate the code ourselves, hash (SHA-256) and store in `OTPCode`, then pass the same code to Gupshup for delivery. Verification is DB-backed (provider-agnostic, supports our rate limiting + attempt tracking).
- **DLT compliance (India, SMS only):** before going live, register entity on a DLT operator platform (Jio/Airtel/Vodafone), get `principalEntityId`, register the OTP SMS template (transactional), and get `dltTemplateId`. Both IDs are configured in settings and passed to Gupshup for SMS sends. The WhatsApp utility template is approved through Meta's template review (via Gupshup dashboard), not DLT. An escalation SMS template must also be DLT-registered if `escalation_fallback_channel` includes SMS.

**GuestStay TTL:** 24 hours from OTP verification. After expiry, guest must re-verify OTP. The User record persists permanently — only the stay expires.

**Primary rate limits** (hotel Wi-Fi safe — dozens of guests share one NAT IP):
  - max 10 requests per **(hotel, guest_stay_id)** per hour
  - max 5 requests per **(hotel, room_number)** per hour
- **Secondary request-IP limits** (high thresholds, abuse-only): deferred in current backend
- `is_active` field on GuestStay — staff can deactivate from dashboard
- New endpoint: `POST /hotels/{hotel_slug}/stays/{stay_id}/revoke/` (IsStaffOrAbove)
- Room-number quality guardrails:
  - validate `room_number` against hotel-configured pattern (default: `^\d{3,4}$`)
  - reject blocked placeholders (default: `0`, `00`, `000`, `999`, `9999`)
  - optional hotel-configured allowed range (example: 100-899)
- **Stay binding:** Every guest write verifies the authenticated user matches the `guest_stay.guest` and `guest_stay.hotel == hotel_from_slug`.

### S3. Hotel ownership validation (multi-tenant isolation)
Every serializer and view that accepts a FK (department, experience, member) validates that the referenced object belongs to the same hotel as the URL `{hotel_slug}`. This is enforced at two layers:

**Layer 1 — `HotelScopedMixin`** (view mixin):
```python
class HotelScopedMixin:
    def get_hotel(self):
        return get_object_or_404(Hotel, slug=self.kwargs['hotel_slug'], is_active=True)

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
```
Every hotel-scoped ViewSet inherits this. Querysets are pre-filtered — impossible to see/modify other hotels' data.
`perform_create()` must be model-aware: if the target model has a direct `hotel` FK, inject `hotel=self.get_hotel()`; if not (for nested models like `Experience` that use `department.hotel`), call `serializer.save()` without a `hotel` kwarg and rely on serializer ownership validation.

**Layer 2 — Serializer validation:**
```python
def validate_department(self, value):
    if value.hotel != self.context['hotel']:
        raise ValidationError("Department does not belong to this hotel.")
    return value
```
Applied to every serializer that accepts department/experience IDs. Belt and suspenders.

### S4. Escalation scheduler: Celery Beat + heartbeat
Replace bare cron with **Celery Beat** (django-celery-beat). Advantages: DB-backed schedule, admin-visible, auto-retry on failure.

- `concierge/tasks.py` — `check_escalations` periodic task (runs every 5 min). **First step:** query only hotels where `escalation_enabled=True`. Hotels with escalation disabled are skipped entirely.
- Heartbeat: task writes `last_run` timestamp to `EscalationHeartbeat` (DB). Django admin can show "Escalation Health" from this model.
- Red/stale heartbeat dashboard banner is planned at frontend level.
- If heartbeat stays red for >15 minutes, send an **out-of-band alert** to Field Guide ops + hotel GM (email/SMS) so pilots don’t fail silently (planned; not yet implemented in backend code).
- Keep a redundant `manage.py check_escalations` systemd timer in pilot even when Celery is enabled (defense-in-depth if broker is degraded).
- **Dual-runner deduplication:** Because Celery Beat and systemd timer may run concurrently, escalation notifications must be idempotent:
  - **Mandatory (DB-level atomic uniqueness with delivery tracking):**
    1. **Insert** escalation event: `RequestActivity.objects.create(action='ESCALATED', escalation_tier=N, notified_at=None, claimed_at=None)` wrapped in `try/except IntegrityError`. The partial unique index rejects duplicates atomically. `IntegrityError` = row already exists, proceed to step 2 anyway (it may need delivery retry).
    2. **Claim** pending deliveries atomically: inside a transaction, `select_for_update(skip_locked=True)` on rows where `notified_at IS NULL AND (claimed_at IS NULL OR claimed_at < now() - 5min)` and `request__status='CREATED'`. Set `claimed_at=now()` on matched rows. The `skip_locked` ensures concurrent runners never claim the same row. The stale-claim timeout (5 min) reclaims rows from crashed workers.
    3. **Send** notification for each claimed event. On success, set `notified_at=now()`. On failure (or worker crash before this point), `notified_at` stays `NULL` and `claimed_at` expires after 5 min — next run reclaims and retries.
    This ensures: (a) each tier is inserted at most once (unique index), (b) each pending send is claimed by exactly one runner (`skip_locked`), (c) crashed workers release claims via stale timeout, (d) `notified_at` is set only on confirmed successful send, (e) no escalation is silently dropped or double-sent.
    (Do **not** use `get_or_create` — read-then-write race. Do **not** use `bulk_create(ignore_conflicts=True)` — Django does not reliably report whether the row was inserted vs ignored.)
  - **Optional optimization (cache lock):** `cache.add('escalation_lock', ..., timeout=300)` as a fast-path lock to skip the DB query when cache is available. If cache lock acquisition fails or cache is down, fall through to DB idempotency check.
  - **Escalation progression and tier timings:**
    - **Tier 1/2/3 recipient behavior (current backend):** all tiers currently notify the same audience (`department staff + ADMIN + SUPERADMIN`) via in-app + push.
    - Tier-specific recipient expansion (for example, Tier 3 widening to all staff across departments) is a planned enhancement.
    - Fallback email/SMS to on-call/GM is planned but not yet implemented in backend services.
    - Timings are measured from `ServiceRequest.created_at` (current backend: wall-clock elapsed time; schedule-window pausing for non-ops departments is deferred).
    - **Source of truth:** defaults in `settings.ESCALATION_TIER_MINUTES = [15, 30, 60]`. Per-hotel override via `Hotel.escalation_tier_minutes` (JSONField, nullable — falls back to settings default if null).
    - Each tier is a distinct `(request_id, escalation_tier)` pair and fires exactly once (enforced by partial unique index).
- If Celery is overkill for pilot: use only `manage.py check_escalations` via systemd timer, but still write heartbeat to DB. Surface in Django admin.

### S5. Push subscription privacy
- `subscription_info` field stores only `endpoint` + `keys` (p256dh, auth) — no user_agent, no IP
- **Device-scoped unsubscribe on logout:** Frontend must call `DELETE /me/push-subscriptions/{id}/` for the current device's subscription before calling `/api/v1/auth/logout/`. The frontend holds the subscription ID in memory (returned from the `POST /me/push-subscriptions/` registration response). If the frontend has no stored subscription ID (e.g. push was never granted), skip the DELETE and call logout directly. The logout endpoint itself only clears auth cookies — it does **not** auto-delete push subscriptions.
- **"Unlink all devices" button** in user settings: calls a bulk endpoint to delete all push subscriptions for the user
- Push payloads contain only `request_public_id` + type — no guest names or room numbers in the notification payload itself
- Clickthrough `url` must be opaque (`/dashboard/requests/{request_public_id}`), never include hotel slug, room number, or integer request IDs
- Request detail API enforces object-level permissions on every retrieve/update/acknowledge action (STAFF: own department only; ADMIN/SUPERADMIN: hotel-wide)
- `request_public_id` must be UUIDv4 (random capability ID) and dashboard links must never use numeric request IDs
- Escalation fallback channel settings (`escalation_fallback_channel`, on-call contact fields) are stored and validated in hotel settings.
  - Automated on-call email/SMS dispatch for escalation events is planned but not yet implemented in backend services.
  - If no fallback channel configured, onboarding enforces always-on front desk dashboard device/tablet.

### S6. Upload handling (image validation + sanitization)
New file: `concierge/validators.py`

```python
ALLOWED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp']
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB

def validate_image_upload(file):
    # 1. Check content type (read magic bytes, don't trust Content-Type header)
    # 2. Reject SVG, GIF, BMP, TIFF
    # 3. Check file size <= 5MB
    # 4. Re-save with Pillow to strip EXIF, re-encode as WebP/JPEG
    # 5. Resize to max 2048px longest edge
```

Applied to all ImageFields via model `clean()` or serializer validation. QR images are generated server-side (always PNG, always safe).

**Media serving:** in prod, media served from R2 with `Content-Disposition: inline` and `X-Content-Type-Options: nosniff` headers. No script execution from media domain.

### S7. Request state machine (explicit transitions)

```
CREATED ──→ ACKNOWLEDGED ──→ CONFIRMED
   │              │           NOT_AVAILABLE
   │              │           NO_SHOW
   │              │           ALREADY_BOOKED_OFFLINE
   │              └──→ (any terminal)
   │
   └──→ EXPIRED (system-set, 72h timeout)
```

**Rules:**
- `CREATED → ACKNOWLEDGED`: **explicit staff action only** — requires staff to click a dedicated "Acknowledge" button which calls `POST /requests/{id}/acknowledge/`. **Viewing the request detail page (GET) does NOT acknowledge it** — it only logs a `VIEWED` activity event. This prevents prefetching, accidental opens, or list-view glances from prematurely stopping escalations.
- `ACKNOWLEDGED → terminal`: staff selects terminal **status** (CONFIRMED, NOT_AVAILABLE, NO_SHOW, ALREADY_BOOKED_OFFLINE) and may add optional `confirmation_reason` detail code
- `CREATED → EXPIRED`: system-set after 72h if still CREATED (via Celery task)
- **No other transitions allowed.** `ServiceRequestUpdateSerializer.validate_status()` enforces this.
- Escalation fires only on requests in `CREATED` state (not yet acknowledged). Once acknowledged, no more escalations.
- Terminal states are immutable — no further status changes allowed.

> **Canonical rule (applies everywhere):** Viewing ≠ Acknowledging. Only `POST /acknowledge/` changes state. No GET/read operation ever mutates request status.

### S8. Department schedule (timezone + day-of-week)
Add to Department model:
- `schedule` (JSONField, default: all days 00:00-23:59)

```json
{
  "timezone": "Asia/Kolkata",
  "default": [["09:00", "21:00"]],
  "overrides": {
    "sun": [["10:00", "18:00"]],
    "sat": [["09:00", "22:00"]]
  }
}
```

**Overnight shift rule:** if `end < start` (e.g. `["22:00", "06:00"]`), treat as wrapping to the next day. Validation: reject if start == end. Example: night-duty Front Desk `["22:00", "06:00"]` means 10 PM today → 6 AM tomorrow. The schedule evaluator checks: if end < start, the window is active when `now >= start OR now < end`. Add tests for midnight-spanning shifts.

- Used by escalation task: only escalate during active hours
- Displayed on guest-facing department page: "Open today: 9 AM - 9 PM" (or "Open today: 10 PM - 6 AM" for night shifts)
- Requests submitted outside hours: still accepted, but flagged as `after_hours=True` in the request, escalation timer pauses until next active window

### S9. QR spam / public endpoint rate limiting
- **Primary limits** (stay/room-based, Wi-Fi safe):
  - `POST /hotels/{hotel_slug}/requests/`: max 10 per `guest_stay_id` per hour, max 5 per (hotel, room) per hour
- **OTP rate limits:**
  - `POST /auth/otp/send/`: max 3 per phone per hour, max 5 per IP per hour
  - `POST /auth/otp/verify/`: max 5 wrong attempts per OTP code (then code invalidated)
- **Secondary IP limits** (high thresholds, abuse-only):
  - `POST /hotels/{hotel_slug}/requests/`: **deferred** in current backend (no request-IP limiter yet)
- **Two-tier rate limiting architecture:**
  1. **Stay/room limits (critical, DB fallback):** Custom `check_rate_limit(key, limit, window)` service function. Tries Redis `INCR`/`GET` first. On cache connection failure, falls through to indexed DB count queries (see below). This is NOT `django-ratelimit` — it's a custom implementation because `django-ratelimit` has no DB fallback capability.
  2. **OTP IP limits (implemented):** Custom cache+DB fallback checks on OTP send (`check_otp_rate_limit_ip`). IP value comes from `REMOTE_ADDR` with trusted proxy forwarding in prod.

**Cache failure mode (Redis down):**
- **Public endpoints** (request submission): **DB-backed fallback limits** (not unlimited default-allow). When Redis is unavailable, stay/room rate limits fall back to indexed DB count queries. Rationale: real limits are still enforced; guests are not blocked by a cache blip, but spam is still capped.
- **DB fallback is explicit:** when cache `get/set` raises `ConnectionError`, run DB count checks for the last hour:
  - stay limit: `ServiceRequest.objects.filter(guest_stay=stay, created_at__gte=now-1h).count()`
  - room limit: `ServiceRequest.objects.filter(hotel=hotel, guest_stay__room_number=stay.room_number, created_at__gte=now-1h).count()`
  - OTP send IP limits: use `OTPCode` count fallback when cache is unavailable
- Required indexes for fallback path:
  - `ServiceRequest(hotel, created_at)`
  - `ServiceRequest(guest_stay, created_at)`
  - `GuestStay(hotel, room_number, created_at)`
- **OTP abuse prevention:** OTP codes expire in 10 minutes. 5 wrong attempts invalidate the code. 3 sends per phone per hour. `OTPCode` model tracks all attempts — no separate ledger needed.
- **Limiter observability:** cache failure logs exist; threshold-based alerting/escalation-health surfacing for limiter degradation is planned.

### S10. Guest-page XSS protection (httpOnly JWT cookies)
Guests authenticate via phone + OTP and receive the **same httpOnly JWT cookies as staff** (`access_token`, `refresh_token`). No tokens are ever readable by JavaScript. This eliminates XSS → session theft entirely.

**Defense-in-depth (CSP + text-only rendering):**
- **Strict CSP** for guest route group `(hotel)`: `script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:` — set via `next.config.ts` `headers()` or middleware
- **All hotel/department/experience descriptions rendered as plain text** — no `dangerouslySetInnerHTML`, no HTML rendering. Text only.

**Guest CSRF policy:** Guest unsafe methods (`PATCH /stays/`, `POST /requests/`) require CSRF protection via `X-CSRFToken` header (same double-submit pattern as staff). Guest page calls `GET /api/v1/auth/csrf/` on mount to obtain the `csrftoken` cookie. OTP endpoints accept anonymous requests normally; CSRF is enforced if authentication is actually performed from a valid auth cookie.

### S11. Deployment: CSRF/CORS assumptions
**Canonical prod topology (split-subdomain):**
- Frontend: `refuje.com` (Cloudflare Workers via OpenNext)
- Backend API: `api.refuje.com` (Docker, reverse proxy)
- Same apex domain → `SameSite=Lax` cookies work cross-subdomain with `.refuje.com` cookie domain
- This is the canonical layout — all settings below reflect this topology

**Required settings (prod):**
```python
CSRF_TRUSTED_ORIGINS = ['https://refuje.com', 'https://api.refuje.com']
CSRF_COOKIE_DOMAIN = '.refuje.com'       # CSRF cookie readable across subdomains
CSRF_COOKIE_SAMESITE = 'Lax'
CORS_ALLOWED_ORIGINS = ['https://refuje.com']
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_DOMAIN = '.refuje.com'     # shared across subdomains
SIMPLE_JWT['AUTH_COOKIE_DOMAIN'] = '.refuje.com'
FRONTEND_ORIGIN = 'https://refuje.com'        # used by generate_qr to build target_url (scheme-aware)
# Prevent proxy/CDN caching issues on auth endpoints.
# Middleware sets `Cache-Control: no-store` for these paths (exact prefix match).
NO_STORE_PATHS = [
    '/api/v1/auth/csrf/',
    '/api/v1/auth/token/',
    '/api/v1/auth/token/refresh/',
    '/api/v1/auth/otp/send/',
    '/api/v1/auth/otp/verify/',
    '/api/v1/auth/logout/',
    '/api/v1/auth/profile/',
]
# Dynamic-path endpoints that also require no-store (set directly in view):
#   GET  /me/requests/{public_id}/         (authenticated resolver)
# Middleware matches NO_STORE_PATHS by exact prefix.

# Uvicorn proxy trust for correct client IP in REMOTE_ADDR (OTP per-IP limits).
# Optional: set TRUSTED_PROXY_IPS in .env.prod to your exact Docker subnet(s).
# If unset, compose defaults to RFC1918 ranges.
TRUSTED_PROXY_IPS='172.16.0.0/12,192.168.0.0/16,10.0.0.0/8'
# docker-compose prod web command includes:
#   --proxy-headers --forwarded-allow-ips=${TRUSTED_PROXY_IPS:-172.16.0.0/12,192.168.0.0/16,10.0.0.0/8}
```

**Local dev:**
```python
CSRF_TRUSTED_ORIGINS = ['http://localhost:6001']
CORS_ALLOWED_ORIGINS = ['http://localhost:6001']
CORS_ALLOW_CREDENTIALS = True
FRONTEND_ORIGIN = 'http://localhost:6001'         # used by generate_qr to build target_url (scheme-aware)
# No cookie domain needed — localhost works without it
# Also return `Cache-Control: no-store` on all auth endpoints above
```

**Required runtime services (docker-compose):**
The deployment requires four runtime processes plus Redis. The existing repo uses **ASGI via uvicorn** (required for SSE streaming responses). Minimal services to add to `docker-compose.yml`:
```yaml
services:
  web:
    build: .
    command: >
      sh -c "python manage.py migrate --noinput &&
      uvicorn tcomp.asgi:application --host 0.0.0.0 --port 8000 --workers 4
      --proxy-headers --forwarded-allow-ips='${TRUSTED_PROXY_IPS:-172.16.0.0/12,192.168.0.0/16,10.0.0.0/8}'"
    env_file: .env
    ports: ["8000:8000"]
    depends_on:
      redis: { condition: service_healthy }

  redis:
    image: redis:7.4-alpine
    command: redis-server --appendonly no --maxmemory 128mb --maxmemory-policy noeviction
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3

  celery-worker:
    build: .
    command: celery -A tcomp worker -l info --concurrency=2 --prefetch-multiplier=1 --max-tasks-per-child=200 -O fair
    env_file: .env
    depends_on:
      web: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "celery", "-A", "tcomp", "inspect", "ping"]
      interval: 30s
      timeout: 10s
      retries: 3

  celery-beat:
    build: .
    command: celery -A tcomp beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    env_file: .env
    depends_on:
      web: { condition: service_healthy }
```
**Serving model: ASGI (uvicorn).** The existing repo already uses `uvicorn tcomp.asgi:application` in both `docker-compose.yml`, `docker-compose.prod.yml`, and `Dockerfile`. ASGI is required for SSE streaming (`StreamingHttpResponse` with async generator). Do **not** switch to WSGI/gunicorn — SSE would require gevent workers and loses Django's native async streaming support. In production, Traefik (already configured in `docker-compose.prod.yml`) sits in front of uvicorn. The systemd timer (`manage.py check_escalations`) runs on the host as redundancy alongside Celery Beat.

### S12. After-hours escalation (current backend behavior)
**All escalation behavior below applies only to hotels with `escalation_enabled=True`.** Hotels with escalation disabled are skipped by the escalation scanner.

Current implementation:
- `after_hours` is computed at request creation from department schedule (including overnight windows).
- On create, if `after_hours=True` and fallback department exists, an informational fallback notification is sent.
- `response_due_at` is computed from the first escalation tier and reminder task uses that timestamp.
- Escalation timing currently uses wall-clock elapsed time from `created_at`; schedule-paused escalation windows are **not** yet applied in `check_escalations()`.

Planned enhancement (deferred):
- Pause escalation timers outside active schedule windows for non-ops departments and resume in next active window.
- Add explicit ops/non-ops routing behavior during escalation tiers.

### S13. Data retention
- **All data is retained permanently in v1.** Guest profiles, stay history, request notes (guest and staff), activity logs — nothing is scrubbed or redacted.
- **Stay lifecycle:** `GuestStay.expires_at` (24h) marks the stay as unable to create new requests. The record is retained permanently for reference and analytics.
- Aggregate stats (counts, conversion rates, department breakdowns) preserved permanently.

### ~~S14. requires_pin~~ — REMOVED
PIN system entirely removed. Guest identity is now verified via phone + OTP. No PIN required for any experience category.

---

## Part 1: Backend — New `concierge` App

### 1.1 New Files

```
tcomp-backend/tcomp/concierge/
├── __init__.py
├── models.py          # All models
├── serializers.py     # DRF serializers (guest-facing + admin)
├── views.py           # ViewSets and APIViews
├── permissions.py     # Hotel-scoped RBAC permission classes
├── filters.py         # django-filter FilterSets
├── urls.py            # Router + URL patterns
├── admin.py           # Django admin registration
├── signals.py         # Post-save signals (activity log, notifications)
├── services.py        # Business logic (QR gen, push, escalation)
├── validators.py      # Image upload validation + sanitization
├── mixins.py          # HotelScopedMixin, state machine helpers
├── authentication.py  # JWTCookieAuthentication class (reads token from cookie; auth views live in users app)
├── tasks.py           # Celery tasks (escalation, OTP cleanup, digest)
├── management/
│   └── commands/
│       └── generate_vapid_keys.py
└── migrations/
```

### 1.2 Models (`concierge/models.py`)

**Hotel**
- `name`, `slug` (unique), `description`, `tagline`
- `logo` (ImageField), `cover_image` (ImageField)
- `address`, `city`, `state`, `pin_code`
- `phone`, `email`, `website`
- `location` (PointField, nullable — optional for v1)
- `timezone` (CharField, default `Asia/Kolkata`)
- `fallback_department` (FK → Department, nullable — typically Front Desk; receives after-hours ops escalations)
- `room_number_pattern` (CharField, default `^\d{3,4}$` — room validation regex for guest flows)
- `blocked_room_numbers` (JSONField, default `["0","00","000","999","9999"]`)
- `room_number_min` / `room_number_max` (IntegerField, nullable — optional range validation)
- `escalation_enabled` (BooleanField, default False — when False, the escalation scheduler skips this hotel entirely. Hotels can operate without escalation in v1.)
- `escalation_fallback_channel` (CharField: `NONE`, `EMAIL`, `SMS`, `EMAIL_SMS`)
- `oncall_email` (EmailField, nullable), `oncall_phone` (CharField, nullable)
- `require_frontdesk_kiosk` (BooleanField, default True — operational guardrail if fallback channel unset)
- `escalation_tier_minutes` (JSONField, nullable — e.g. `[15, 30, 60]`. Falls back to `settings.ESCALATION_TIER_MINUTES` if null.)
- `is_active` (BooleanField), `created_at`, `updated_at`

**Hotel activation validation:** A hotel can be set `is_active=True` without enabling escalation. **If `escalation_enabled=True`**, then either (a) `escalation_fallback_channel != NONE` with valid `oncall_email`/`oncall_phone`, or (b) `require_frontdesk_kiosk=True` must be satisfied. Enforced in `Hotel.clean()` and `HotelSettingsSerializer.validate()`. Hotels with `escalation_enabled=False` skip these checks — they simply don't run escalation.

**HotelMembership** — RBAC pivot table
- `user` (FK → User), `hotel` (FK → Hotel)
- `role` (CharField, choices: `SUPERADMIN`, `ADMIN`, `STAFF`)
- `department` (FK → Department, nullable — only required for STAFF)
- `is_active`, `created_at`
- `unique_together: (user, hotel)`

**Department**
- `hotel` (FK → Hotel)
- `name`, `slug`
- `description` (TextField)
- `photo` (ImageField), `icon` (CharField, nullable — icon identifier)
- `display_order` (IntegerField, default 0)
- `schedule` (JSONField, default `{"timezone": "Asia/Kolkata", "default": [["00:00", "23:59"]]}`) — day-of-week + timezone
- `is_ops` (BooleanField, default False — True for Front Desk, Housekeeping, etc. Ops depts never pause escalation)
- `is_active`, `created_at`, `updated_at`
- `unique_together: (hotel, slug)`

**Experience** (hotel experiences — separate from `guides.Experience`)
- `department` (FK → Department, related_name `experiences`)
- `name`, `slug`
- `description` (TextField)
- `photo` (ImageField), `cover_image` (ImageField, nullable)
- `price` (DecimalField, nullable), `price_display` (CharField — "₹2,500 per couple")
- `category` (CharField, choices: `DINING`, `SPA`, `ACTIVITY`, `TOUR`, `TRANSPORT`, `OTHER`)
- `timing` (CharField — "7 PM - 10 PM"), `duration` (CharField — "2 hours")
- `capacity` (CharField, nullable — "2-4 guests")
- `highlights` (JSONField, default list — array of strings)
- `is_active`, `display_order`, `created_at`, `updated_at`

**GuestStay** — links a guest user to a hotel visit
- `guest` (FK → User, related_name `stays` — must be `user_type=GUEST`)
- `hotel` (FK → Hotel)
- `room_number` (CharField, blank — entered on a separate screen after OTP verification, pre-filled on subsequent requests, editable)
- `qr_code` (FK → QRCode, nullable — set during OTP verify from the `?qr=` param. Resolved by `QRCode.code` lookup, validated against `hotel` + `is_active`. Tracks which QR initiated this stay.)
- `is_active` (BooleanField, default True — staff can deactivate from dashboard)
- `created_at`, `expires_at` (DateTimeField — **24 hours** from OTP verification)
- Index on `(hotel, room_number, created_at)` for DB fallback room-rate limits
- Index on `(guest, hotel)` for quick stay lookup
- `room_number` validated against hotel pattern/range and blocked-room list when set
- **Cross-hotel recognition:** same guest User can have stays at multiple hotels. `guest.stays.all()` shows full history.

**OTPCode** — phone verification codes
- `phone` (CharField, db_index)
- `code_hash` (CharField — SHA256/HMAC of 6-digit code; plaintext never stored)
- `hotel` (FK → Hotel, **nullable** — set when OTP is requested from a guest flow with `hotel_slug`; null for staff phone login where no hotel context is needed. Rate-limit queries use `phone` + `ip_hash` regardless of hotel.)
- `channel` (CharField, choices: `WHATSAPP`, `SMS` — records which channel actually delivered the OTP. Set after delivery attempt resolves: `WHATSAPP` if WhatsApp send succeeded, `SMS` if fell back to SMS.)
- `gupshup_message_id` (CharField, nullable — Gupshup `messageId` from WhatsApp API response or transaction ID from SMS API. Used for delivery tracking/debugging.)
- `sms_fallback_sent` (BooleanField, default False — set True only after fallback SMS send success)
- `sms_fallback_claimed_at` (DateTimeField, nullable — claim timestamp for webhook/sweeper fallback worker coordination; stale claims are retryable)
- `wa_delivered` (BooleanField, default False — set True when Gupshup webhook reports `delivered` or `read` for this OTP's `gupshup_message_id`. Tells the timeout sweeper this OTP was successfully delivered via WhatsApp — no SMS fallback needed.)
- `ip_hash` (CharField — SHA256/HMAC of client IP for rate limiting; raw IP never stored)
- `attempts` (IntegerField, default 0 — wrong verification attempts; invalidated at 5)
- `is_used` (BooleanField, default False)
- `created_at`, `expires_at` (DateTimeField — **10 minutes** from creation)
- Index on `(phone, created_at)` for rate-limit queries

**ServiceRequest** — the request ledger
- `hotel` (FK → Hotel)
- `public_id` (UUIDField, unique, default uuid4, db_index) — opaque identifier for push clickthrough / dashboard deep-links
- `guest_stay` (FK → GuestStay)
- `experience` (FK → Experience, nullable — null for custom/department-level requests)
- `department` (FK → Department — **derived from `experience.department` when experience is provided**; client-supplied mismatched department rejected)
- `request_type` (CharField: `BOOKING`, `INQUIRY`, `CUSTOM`)
- `status` (CharField: `CREATED`, `ACKNOWLEDGED`, `CONFIRMED`, `NOT_AVAILABLE`, `NO_SHOW`, `ALREADY_BOOKED_OFFLINE`, `EXPIRED`)
- `guest_notes` (TextField)
- `guest_date` (DateField, nullable), `guest_time` (TimeField, nullable), `guest_count` (IntegerField, nullable)
- `staff_notes` (TextField, blank)
- `assigned_to` (FK → User, nullable)
- `confirmation_token` (UUIDField, unique, default uuid4) — **reserved for Phase 2** (WhatsApp confirmation links). No public endpoint accepts or exposes this field in v1; exclude from serializers and Django admin list/display/export in v1.
- `confirmation_reason` (CharField, blank — optional detail code for terminal outcome, not duplicate of status)
- `after_hours` (BooleanField, default False — True if submitted outside dept schedule)
- `response_due_at` (DateTimeField, nullable — guest-facing "we will confirm by" timestamp)
- `acknowledged_at` (DateTimeField, nullable), `confirmed_at` (DateTimeField, nullable)
- `created_at`, `updated_at`
- Index on `(hotel, status, created_at)`
- Index on `(hotel, created_at)` (rate-limit fallback counts)
- Index on `(guest_stay, created_at)` (stay fallback counts)
- Index on `(hotel, updated_at)` (SSE catchup / list ordering)

**State machine enforced in `ServiceRequestUpdateSerializer`:**
```
VALID_TRANSITIONS = {
    'CREATED': ['ACKNOWLEDGED'],
    'ACKNOWLEDGED': ['CONFIRMED', 'NOT_AVAILABLE', 'NO_SHOW', 'ALREADY_BOOKED_OFFLINE'],
}
# EXPIRED set only by system task. Terminal states have no outgoing transitions.
```
`confirmation_reason` is validated with status coupling:
- optional for terminal states, must be blank for non-terminal states
- must be a detail code that does **not** duplicate status (example codes: `SOLD_OUT`, `MAINTENANCE`, `GUEST_UNREACHABLE`, `WALK_IN`)
- if provided, value must belong to allowed detail-code enum for the selected terminal status

**RequestActivity** — audit trail
- `request` (FK → ServiceRequest, related_name `activities`)
- `actor` (FK → User, nullable — null for guest/system)
- `action` (CharField: `CREATED`, `VIEWED`, `ACKNOWLEDGED`, `CONFIRMED`, `CLOSED`, `ESCALATED`, `NOTE_ADDED`, `EXPIRED`). **`CLOSED` covers negative terminal statuses** (`NOT_AVAILABLE`, `NO_SHOW`, `ALREADY_BOOKED_OFFLINE`) — the specific terminal status is recorded in `details.status_to`. `CONFIRMED` is the positive terminal action. This avoids a `REJECTED` action that has no corresponding request status.
- `escalation_tier` (PositiveSmallIntegerField, nullable — only set when `action='ESCALATED'`, e.g. 1, 2, 3). **Check constraint:** `action='ESCALATED' → escalation_tier IS NOT NULL` and `action!='ESCALATED' → escalation_tier IS NULL`.
- `claimed_at` (DateTimeField, nullable — only used for `ESCALATED` events. Set when a runner claims the row for delivery. Stale claims older than 5 min are reclaimed by the next runner, recovering from worker crashes.) **Check constraint:** `action != 'ESCALATED' → claimed_at IS NULL`.
- `notified_at` (DateTimeField, nullable — only used for `ESCALATED` events. Null = delivery not yet confirmed. Set **only** on successful notification send. Enables retry of failed/crashed sends without re-inserting the dedupe row.) **Check constraint:** `action != 'ESCALATED' → notified_at IS NULL`.
- `details` (JSONField, default dict — **allowlisted keys only**: `status_from`, `status_to`, `note_length` (integer — character count, not content), `assigned_to_id`, `department_id`. Must **never** contain guest name, room number, free-text notes, or other PII. Enforced by a `clean_activity_details()` helper called before every save. Staff note content lives only on `ServiceRequest.staff_notes` — activity log records that a note was added and its length, not the content.)
- `created_at`
- **Unique constraint:** `(request, action, escalation_tier)` where `action='ESCALATED'` — enforces one-time-per-tier at the DB level. Implemented as a partial unique index: `CREATE UNIQUE INDEX ... ON request_activity (request_id, escalation_tier) WHERE action = 'ESCALATED'`.

**Notification** — in-app notifications
- `user` (FK → User, related_name `notifications`)
- `hotel` (FK → Hotel)
- `request` (FK → ServiceRequest, nullable)
- `title` (CharField), `body` (TextField)
- `notification_type` (CharField: `NEW_REQUEST`, `ESCALATION`, `DAILY_DIGEST`, `SYSTEM`)
- `is_read` (BooleanField, default False)
- `created_at`
- Index on `(user, is_read, created_at)`

**PushSubscription** — Web Push (minimal data, privacy-safe)
- `user` (FK → User, related_name `push_subscriptions`)
- `subscription_info` (JSONField — { endpoint, keys: { p256dh, auth } } — no user_agent, no IP)
- `is_active` (BooleanField, default True)
- `created_at`

**QRCode**
- `hotel` (FK → Hotel)
- `code` (CharField, unique, db_index — short non-sequential public identifier, e.g. `a3x9kQ`. Generated on create via `secrets.token_urlsafe(6)`. Used in scan URL: `?qr={code}`. Avoids PK enumeration.)
- `department` (FK → Department, nullable — hotel-level if null)
- `placement` (CharField, choices: `LOBBY`, `ROOM`, `RESTAURANT`, `SPA`, `POOL`, `BAR`, `GYM`, `GARDEN`, `OTHER`)
- `label` (CharField — freeform description: "Reception Desk", "Room 304 Tent Card", "Pool Bar Menu")
- `qr_image` (ImageField, upload_to `qr_codes/`)
- `is_active` (BooleanField, default True)
- `created_by` (FK → User)
- `created_at`
- **`target_url` is auto-generated** on create (not a stored field): `{FRONTEND_ORIGIN}/h/{hotel.slug}?qr={code}`. The `generate_qr` service builds this URL, encodes it into the QR PNG, and saves it. Admin never enters a URL manually.
- **Analytics note:** `GuestStay.qr_code` FK records which QR initiated a stay. This tracks **OTP-verified stays per QR**, not raw scans. A separate scan-event log is not in v1 scope.

**EscalationHeartbeat** — scheduler health monitoring
- `task_name` (CharField, unique)
- `last_run` (DateTimeField)
- `status` (CharField: `OK`, `FAILED`)
- `details` (TextField, blank)

### 1.3 Permissions (`concierge/permissions.py`)

All permissions are **hotel-scoped** — they look up the hotel from the URL kwargs (`hotel_slug`) and check the user's membership role for that specific hotel.

```python
class IsHotelMember(BasePermission):
    """User has any active membership for the hotel in URL."""

class IsStaffOrAbove(BasePermission):
    """User has STAFF, ADMIN, or SUPERADMIN role. STAFF only sees own department."""

class IsAdminOrAbove(BasePermission):
    """User has ADMIN or SUPERADMIN role."""

class IsSuperAdmin(BasePermission):
    """User has SUPERADMIN role for this hotel."""

class CanAccessRequestObject(BasePermission):
    """Object-level check for ServiceRequest (hotel-scoped routes).
    Derives hotel from URL `hotel_slug` kwarg.
    STAFF: request.department must equal membership.department.
    ADMIN/SUPERADMIN: any department within the same hotel."""

class CanAccessRequestObjectByLookup(BasePermission):
    """Object-level check for ServiceRequest on slug-less routes (e.g. /me/requests/{public_id}/).
    Derives hotel from `request_obj.hotel` instead of URL kwargs.
    Checks user has an active membership in request_obj.hotel with appropriate role.
    Same STAFF/ADMIN/SUPERADMIN rules as CanAccessRequestObject."""

class IsActiveGuest(BasePermission):
    """User is authenticated, user_type=GUEST, and has an active non-expired GuestStay
    for the hotel in URL (`hotel_slug`). Used on guest-scoped hotel endpoints
    (request create, stay update). Checks:
    1. request.user.user_type == 'GUEST'
    2. GuestStay.objects.filter(guest=user, hotel=hotel, is_active=True, expires_at__gt=now()).exists()
    Rejects with 401 if not authenticated, 403 if user is not a guest or has no valid stay."""

class IsStayOwner(BasePermission):
    """Object-level check for GuestStay. Ensures the authenticated guest owns the stay
    being accessed (stay.guest == request.user). Used on PATCH /stays/{stay_id}/.
    Combined with IsActiveGuest for full authorization."""
```

**Staff permission helpers:** `get_membership(user, hotel_slug)` → returns `HotelMembership` or None. For slug-less routes, use `get_membership_by_hotel(user, hotel)` → same lookup by hotel object instead of slug.

**Staff endpoint assignment:**
`ServiceRequestDetail`, `ServiceRequestUpdate`, `ServiceRequestAcknowledge`, `ServiceRequestTakeOwnership`, and `RequestNoteCreate` use `CanAccessRequestObject` (hotel-scoped).
`MyRequestDetail` (`/me/requests/{public_id}/`) uses `CanAccessRequestObjectByLookup` (slug-less — derives hotel from the request object itself).

**Guest endpoint assignment:**
`ServiceRequestCreate` uses `[IsActiveGuest]` — validates guest has active stay with room_number set.
`GuestStayUpdate` uses `[IsActiveGuest, IsStayOwner]` — validates guest owns the stay being patched.
`MyStaysList` and `MyRequestsList` use `[IsAuthenticated]` — filtered to `request.user` in queryset (works for both guest and staff).

### 1.4 Hotel-Scoped Mixin (`concierge/mixins.py`)

```python
class HotelScopedMixin:
    """Ensures all queries are filtered to the hotel in the URL.
    Prevents cross-hotel data leakage."""

    def get_hotel(self):
        return get_object_or_404(Hotel, slug=self.kwargs['hotel_slug'], is_active=True)

    def get_queryset(self):
        qs = super().get_queryset()
        hotel = self.get_hotel()
        # Filter by hotel FK — works for Department, Experience, ServiceRequest, etc.
        if hasattr(qs.model, 'hotel'):
            return qs.filter(hotel=hotel)
        # For nested models (Experience → department__hotel)
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
            # Nested ownership (e.g., Experience.department.hotel) is enforced in serializer validation.
            serializer.save()
```

### 1.5 API Endpoints (`concierge/urls.py`)

Mounted at `api/v1/` via `tcomp/urls.py`. Defines `hotels/`, `me/`, and `webhooks/` URL prefixes.

> **URL kwarg convention:** All hotel-scoped routes use `{hotel_slug}` as the URL kwarg name. This matches the Python code (`self.kwargs['hotel_slug']` in `HotelScopedMixin` and permission classes). Department routes use `{dept_slug}`, experience routes use `{exp_id}`.

**Public (AllowAny, rate-limited):**
| Method | URL | View | Rate Limit |
|--------|-----|------|------------|
| GET | `/hotels/{hotel_slug}/` | HotelPublicDetail | — |
| GET | `/hotels/{hotel_slug}/departments/` | DepartmentPublicList | — |
| GET | `/hotels/{hotel_slug}/departments/{dept_slug}/` | DepartmentPublicDetail | — |
| GET | `/hotels/{hotel_slug}/experiences/{exp_id}/` | ExperiencePublicDetail | — |
| POST | `/webhooks/gupshup-wa/` | GupshupWAWebhookView | **Gupshup WhatsApp delivery webhook.** AllowAny (no cookie auth — Gupshup can't authenticate as a user). Validated via HMAC signature (`GUPSHUP_WA_WEBHOOK_SECRET`). Receives delivery events (enqueued/sent/delivered/failed) for WhatsApp OTP messages. On failure event, triggers SMS fallback via `handle_wa_delivery_event()`. Invalid signature returns **403**; valid signed callbacks return 200. **Mounted at** `path('webhooks/gupshup-wa/', GupshupWAWebhookView.as_view())` in `concierge/urls.py` (already under `api/v1/` via `tcomp/urls.py` include). |

**Guest (IsAuthenticated, user_type=GUEST, cookie auth):**
| Method | URL | View | Rate Limit |
|--------|-----|------|------------|
| POST | `/hotels/{hotel_slug}/requests/` | ServiceRequestCreate | 10/stay/hr, 5/room/hr (implemented). Guest must have active `GuestStay` for this hotel with room_number set. |
| PATCH | `/hotels/{hotel_slug}/stays/{stay_id}/` | GuestStayUpdate | — | Update room_number on current stay. Validated against hotel pattern/range/blocklist. |
| GET | `/me/stays/` | MyStaysList | — | Guest's stays across all hotels. |
| GET | `/me/requests/` | MyRequestsList | — | Guest's requests across all stays. |

**Staff (IsStaffOrAbove, cookie auth):**
| Method | URL | View | Purpose |
|--------|-----|------|---------|
| GET | `/hotels/{hotel_slug}/requests/list/` | ServiceRequestList | List requests (staff sees own dept only) |
| GET | `/hotels/{hotel_slug}/requests/{id}/` | ServiceRequestDetail | Request detail + activity log (logs VIEWED event, does NOT change status) |
| GET | `/hotels/{hotel_slug}/requests/public/{public_id}/` | ServiceRequestDetailByPublicId | Hotel-scoped opaque-ID lookup (for in-dashboard navigation when hotel context is known). **Must use same `CanAccessRequestObject` permission check as ID route.** For push deep-links (no hotel context), use `/me/requests/{public_id}/` instead. |
| POST | `/hotels/{hotel_slug}/requests/{id}/acknowledge/` | ServiceRequestAcknowledge | **Explicit acknowledge** — moves CREATED→ACKNOWLEDGED. Stops escalation. **Idempotent:** repeat ACK returns 200; implement with transaction/row-lock to avoid races with escalation task. |
| POST | `/hotels/{hotel_slug}/requests/{id}/take-ownership/` | ServiceRequestTakeOwnership | Fallback/front desk or admin claims after-hours request ownership (`assigned_to`) |
| PATCH | `/hotels/{hotel_slug}/requests/{id}/` | ServiceRequestUpdate | Update status from ACKNOWLEDGED→terminal (state machine enforced) |
| POST | `/hotels/{hotel_slug}/requests/{id}/notes/` | RequestNoteCreate | Add staff note |
| POST | `/hotels/{hotel_slug}/stays/{stay_id}/revoke/` | GuestStayRevoke | Deactivate a guest stay. **View validates `stay.hotel == URL hotel`** (uses HotelScopedMixin queryset filtering — prevents cross-hotel revocation). |
| GET | `/hotels/{hotel_slug}/dashboard/` | DashboardStats | Aggregate stats (counts, rates) |
| GET | `/hotels/{hotel_slug}/requests/stream/` | RequestSSEStream | **SSE endpoint.** Server-Sent Events stream for real-time request updates. Authenticated staff only. Sends events: `request.created`, `request.updated`. Uses Redis pub/sub. Client auto-reconnects on disconnect. |

**Admin (IsAdminOrAbove, cookie auth):**
| Method | URL | View | Purpose |
|--------|-----|------|---------|
| CRUD | `/hotels/{hotel_slug}/admin/departments/` | DepartmentViewSet | Manage departments |
| CRUD | `/hotels/{hotel_slug}/admin/departments/{dept_slug}/experiences/` | ExperienceViewSet | Manage experiences |
| CRUD | `/hotels/{hotel_slug}/admin/qr-codes/` | QRCodeViewSet | Generate/list/delete QR codes |

**SuperAdmin (IsSuperAdmin, cookie auth):**
| Method | URL | View | Purpose |
|--------|-----|------|---------|
| GET/POST | `/hotels/{hotel_slug}/admin/members/` | MemberList/Create | List & invite staff |
| PATCH/DELETE | `/hotels/{hotel_slug}/admin/members/{id}/` | MemberDetail | Update role / deactivate |
| PATCH | `/hotels/{hotel_slug}/admin/settings/` | HotelSettingsUpdate | Timezone, room validation, escalation settings, etc. |

**User-scoped (IsAuthenticated, cookie auth):**
| Method | URL | View | Purpose |
|--------|-----|------|---------|
| GET | `/me/hotels/` | MyHotelsList | Hotels the user belongs to |
| GET | `/me/notifications/` | NotificationList | User's notifications |
| POST | `/me/notifications/mark-read/` | NotificationMarkRead | Mark notifications as read |
| POST | `/me/push-subscriptions/` | PushSubscriptionCreate | Register push subscription |
| DELETE | `/me/push-subscriptions/{id}/` | PushSubscriptionDelete | Unregister single device subscription |
| DELETE | `/me/push-subscriptions/` | PushSubscriptionBulkDelete | **"Unlink all devices"** — deletes all push subscriptions for the authenticated user (see S5) |
| GET | `/me/requests/{public_id}/` | MyRequestDetail | **Push deep-link resolver.** Looks up request by `public_id`, derives hotel from `request.hotel` (not URL slug), validates user has an active membership via `CanAccessRequestObjectByLookup`. Returns full request detail + `hotel_slug`. View sets `Cache-Control: no-store`. Used by frontend `/dashboard/requests/[publicId]` route. |

**Auth (cookie-based) — owned by `users` app at `/api/v1/auth/`:**

> **Canonical rule:** All auth endpoints are defined in `users/urls.py` and mounted at `path('api/v1/auth/', include('users.urls'))` in `tcomp/urls.py`. The `concierge` app **must not** register any `/auth/*` routes. Existing Bearer-only views in `users/views.py` are replaced with cookie-based equivalents (see backend modifications table).

| Method | URL | View | Purpose |
|--------|-----|------|---------|
| GET | `/auth/csrf/` | CSRFTokenView | Sets `csrftoken` cookie (non-httpOnly, readable by JS for double-submit), `Cache-Control: no-store` |
| POST | `/auth/token/` | CookieTokenObtainPairView | Staff login (email+password) → sets httpOnly cookies, `Cache-Control: no-store`. **Replaces** existing `TokenObtainPairView`. |
| POST | `/auth/token/refresh/` | CookieTokenRefreshView | Refresh → rotates cookies, `Cache-Control: no-store`. **Replaces** existing `TokenRefreshView`. |
| POST | `/auth/otp/send/` | OTPSendView | **Phone login step 1 (guests + staff).** { phone, hotel_slug (optional) } → sends 6-digit OTP via **WhatsApp** (Gupshup WhatsApp API, primary) with **SMS fallback** (Gupshup Enterprise SMS API). Does not look up user — branching happens at verify. Rate: 3/phone/hr, 5/IP/hr. `Cache-Control: no-store`. **New endpoint.** |
| POST | `/auth/otp/verify/` | OTPVerifyView | **Phone login step 2 (guests + staff).** { phone, code, hotel_slug (optional), qr_code (optional) } → verifies OTP then branches: (a) phone matches existing STAFF → JWT cookies + memberships, no GuestStay; (b) guest/unknown phone + hotel_slug → create User if new + GuestStay + JWT cookies; (c) guest/unknown phone + no hotel_slug → **400**. Rate: 5 wrong attempts per code. `Cache-Control: no-store`. **New endpoint.** |
| POST | `/auth/logout/` | LogoutView | Clears auth cookies only. Push subscription cleanup is the frontend's responsibility — must call `DELETE /me/push-subscriptions/{id}/` before logout (see S5). `Cache-Control: no-store`. **New endpoint.** |
| GET | `/auth/profile/` | AuthProfileView | Returns current user + memberships (staff) or stays (guest) for session validation on app mount, `Cache-Control: no-store`. **Replaces** existing `ProfileView` (adds membership/stay data). |
| PATCH | `/auth/profile/` | AuthProfileView | Updates own `phone`, `first_name`, `last_name`. Phone validated for uniqueness (partial unique index). Primary path for staff to add/change phone for OTP login. `Cache-Control: no-store`. **New endpoint.** |

### 1.6 Key Serializers

- `HotelPublicSerializer` — name, slug, description, logo, cover, departments (nested)
- `HotelSettingsSerializer` — timezone, room validation settings (pattern/range/blocklist), `escalation_enabled` (toggle), escalation fallback settings (channel, on-call contact, kiosk requirement — validated only when `escalation_enabled=True`)
- `DepartmentSerializer` — full CRUD fields, nested experiences for read, schedule (JSON validated)
- `ExperienceSerializer` — full CRUD fields, **validates `department.hotel == context['hotel']`**
- `OTPSendSerializer` — phone (validated format), hotel_slug (optional — validated if provided, but send doesn't branch on user type). OTPCode record stores hotel FK if slug provided, null otherwise.
- `OTPVerifySerializer` — phone, code (write-only), hotel_slug (optional), qr_code (optional, write-only — resolved by `QRCode.code` lookup, validated: must belong to hotel + `is_active=True`, silently ignored if invalid). **Branching logic:** (a) phone matches existing STAFF user → issue JWT, return user info + memberships, skip GuestStay. (b) phone matches existing GUEST or no user + `hotel_slug` provided → guest flow, creates/finds User (user_type=GUEST), creates GuestStay. (c) guest/unknown phone + no `hotel_slug` → **400** (`hotel_slug is required for non-staff users`).
- `GuestStayUpdateSerializer` — room_number (validated against hotel pattern/range/blocklist)
- `ServiceRequestCreateSerializer` — experience (optional, **validates ownership**), department (**validated/derived**), request_type, guest_name (CharField, write-only), guest_notes, guest_date, guest_time, guest_count. View derives `guest_stay` from the authenticated guest user + hotel. Validates stay is active and not expired. If `experience` exists, server sets `department = experience.department` and rejects mismatched client department; sets `response_due_at`. **`guest_name` mapping:** on create, if user's `first_name` is blank, split `guest_name` on the first space — everything before is `first_name`, everything after (if any) is `last_name`. If `first_name` is already set (returning guest), do not overwrite — the name from the first request persists. Guest can update name via `/auth/profile/` PATCH if needed. Empty `last_name` is allowed (single-name guests). The form pre-fills `guest_name` from `user.first_name + ' ' + user.last_name` for returning guests.
- `ServiceRequestListSerializer` — summary with guest info, status, timestamps. **`guest_name`** is a read-only `SerializerMethodField`: returns `f"{user.first_name} {user.last_name}".strip()` from `guest_stay.guest`. **`room_number`** is sourced from `guest_stay.room_number`.
- `ServiceRequestUpdateSerializer` — status (**state machine validated**), staff_notes, confirmation_reason (optional detail code for terminal states only; must be blank otherwise)
- `AuthProfileSerializer` — current user profile + hotel memberships (staff) or stays (guest) (used by `/auth/profile/`)
- `MemberSerializer` — user email/name/phone, role, department (**validates department.hotel**). `phone` is read-only here (staff update their own phone via `/auth/profile/`).
- `MemberCreateSerializer` — email, role, department, phone (optional — creates user if needed). If `phone` is provided, it's set on the User record at invite time, enabling immediate OTP login.
- `AuthProfileUpdateSerializer` — allows authenticated user to update their own `phone`, `first_name`, `last_name` via `PATCH /auth/profile/`. Phone validated for uniqueness (partial unique index). This is the primary path for staff to add/change their phone number for OTP login.
- `QRCodeSerializer` — `code` (read-only, auto-generated), `placement` (required, from enum), `label`, `department` (**validates ownership**), `target_url` (read-only, auto-generated), `is_active`, `qr_image` (read-only, generated on create), `stay_count` (read-only, annotated `Count('gueststay')` — OTP-verified stays attributed to this QR). On create: delegates to `generate_qr` service. Admin provides only `label`, `placement`, and optionally `department`.
- `NotificationSerializer` — title, body, type, is_read, request_public_id (no guest PII in payload)
- `ServiceRequest* serializers` must never expose `confirmation_token` (Phase 2 field). Add explicit `exclude = ['confirmation_token']` guard.

### 1.7 Services (`concierge/services.py`)

- `generate_qr(hotel, label, placement, department=None)` — generates non-sequential `code` via `secrets.token_urlsafe(6)`, builds `target_url` (`{FRONTEND_ORIGIN}/h/{hotel.slug}?qr={code}`), encodes URL into QR PNG via `qrcode` library, creates `QRCode` record (code, placement, label, hotel, department, is_active=True, created_by), saves PNG to media. Admin never enters a URL manually.
- `send_push_notification(user, title, body, url=None)` — uses `pywebpush`, payload contains only `request_public_id` + type (no guest PII); `url` must use opaque route `/dashboard/requests/{public_id}`
- `notify_department_staff(department, request)` — creates Notification for all active department STAFF plus hotel ADMIN/SUPERADMIN, sends push
- `notify_after_hours_fallback(request)` — for non-ops requests created outside hours, sends immediate informational notification to fallback department/front desk
- `send_otp(phone, hotel=None)` — generates 6-digit code, stores SHA-256 hash in `OTPCode`, delivers via **WhatsApp first, SMS fallback**. `hotel` is optional (staff OTP send doesn't require hotel context). Synchronous fallback path marks channel/fallback status before SMS call and rolls back state on failure.
- `_send_sms_otp(otp_code, code)` — low-level SMS sender that returns success/failure. It does not own OTP state transitions.
- `send_sms_fallback_for_otp(otp)` — webhook/sweeper fallback helper: caller must claim row first; helper sends SMS, updates `code_hash + channel + sms_fallback_sent` on success, and clears `sms_fallback_claimed_at` on failure for retry.
- `handle_wa_delivery_event(payload)` — processes Gupshup webhook events. `delivered/read` set `wa_delivered=True`. `failed` path uses same stale-claim guard as sweeper (`sms_fallback_claimed_at is null or stale`), atomically claims with `select_for_update(skip_locked=True)`, then calls fallback sender outside DB lock to avoid long lock holds.
- `verify_otp(phone, code, hotel=None, qr_code=None)` — validates code hash, checks expiry/attempts. On success: looks up User by phone. **If existing STAFF user:** returns `(user, None)` — no GuestStay created, `hotel` param ignored. **If existing GUEST user or unknown phone:** `hotel` is required (raises `ValidationError` if None); finds or creates User (user_type=GUEST), creates GuestStay (with `qr_code` FK if provided), returns `(user, stay)`.
- `is_department_after_hours(department)` — computes after-hours using department schedule (supports overnight windows and prior-day spillover)
- `compute_response_due_at(hotel)` — computes `response_due_at` from first escalation tier
- `publish_request_event(hotel, event_type, request)` — publishes SSE event to Redis pub/sub channel `hotel:{hotel_id}:requests`. Event types: `request.created`, `request.updated`. Payload: `{ event, request_id, public_id, status, department_id, updated_at }`.
- `stream_request_events(hotel, user)` — SSE generator. Subscribes to Redis pub/sub channel, yields events as `text/event-stream`. Filters events by user's department (STAFF) or sends all (ADMIN/SUPERADMIN).
- `check_escalations()` — filters to hotels where `escalation_enabled=True`, evaluates pending `CREATED` requests against tier thresholds (`Hotel.escalation_tier_minutes` override or settings default), and fires idempotent escalation activities/notifications with claim semantics. Current implementation uses wall-clock elapsed time (no schedule-window pausing yet) and currently uses the same recipient audience across all tiers. Writes heartbeat status.
- `expire_stale_requests()` — marks `CREATED` requests older than 72h as `EXPIRED`
- `send_response_due_reminders()` — scans every run and sends reminder when `response_due_at` is already passed and request is still `CREATED` (current backend has no one-time reminder dedupe marker)
- `get_dashboard_stats(hotel, department=None)` — aggregates current dashboard stats
- `validate_and_process_image(file)` — strips EXIF, resize to 2048px max, re-encode JPG/PNG/WebP, reject SVG/GIF

### 1.8 Celery Tasks (`concierge/tasks.py`)

```python
@shared_task
def check_escalations_task():
    """Runs every 5 min via Celery Beat (and/or systemd timer).
    Skips hotels where escalation_enabled=False.
    Idempotent with atomic claim:
    1. Insert escalation event (create + IntegrityError = already exists).
    2. Claim pending events via select_for_update(skip_locked=True) where
       notified_at IS NULL AND (claimed_at IS NULL OR stale > 5min). Set claimed_at=now().
    3. Send notification. On success, set notified_at=now(). On failure/crash,
       notified_at stays NULL and claimed_at expires after 5min — next run reclaims.
    Partial unique index prevents duplicate tier inserts. skip_locked + claimed_at
    prevents duplicate sends and recovers from worker crashes.
    Optional cache.add() fast-path lock avoids DB round-trip when cache is available.
    Writes heartbeat to EscalationHeartbeat model after each check run."""

@shared_task
def expire_stale_requests_task():
    """Runs hourly. Marks CREATED requests older than 72h as EXPIRED."""

@shared_task
def response_due_reminder_task():
    """Runs every 5 min. Sends reminder if response_due_at passed and request still CREATED."""

@shared_task
def otp_wa_fallback_sweep_task():
    """Runs every 10 seconds via Celery Beat (short-interval periodic task).
    Queries OTPCode where created_at exceeded WA timeout window,
    wa_delivered=False, sms_fallback_sent=False, and OTP still valid.
    Uses stale-claim coordination:
    - claim only rows where sms_fallback_claimed_at is null or stale (>60s)
    - set sms_fallback_claimed_at atomically under select_for_update(skip_locked=True)
    - send SMS outside the lock via send_sms_fallback_for_otp()
    Success path sets sms_fallback_sent=True; failure clears claim for retry.
    Prevents duplicate concurrent sends between sweeper and webhook fallback."""

@shared_task
def cleanup_expired_otps_task():
    """Runs daily. Deletes OTPCode records older than 24 hours (codes expire in 10 min
    but records kept briefly for rate-limit queries)."""

@shared_task
def daily_digest_task():
    """Runs daily at configured time per hotel. Sends digest notification."""

```

Fallback if Celery is too heavy for pilot: `manage.py check_escalations` via systemd timer, but still write `EscalationHeartbeat`. In pilot, keep systemd timer as redundancy even when Celery is enabled. Surface health in Django admin and dashboard warning banner.

### 1.9 Settings Additions (`tcomp/settings/base.py`)

```python
INSTALLED_APPS += ['django_filters', 'django_celery_beat', 'django_celery_results', 'concierge']

REST_FRAMEWORK['DEFAULT_FILTER_BACKENDS'] = [
    'django_filters.rest_framework.DjangoFilterBackend',
    'rest_framework.filters.OrderingFilter',
]

REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES'] = [
    'concierge.authentication.JWTCookieAuthentication',
    # ⛔ NO JWTAuthentication (Bearer) here — cookie-only in base/prod.
    # Bearer fallback is added ONLY in dev.py for API testing convenience.
]

# Shorten access token for cookie auth
SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'] = timedelta(minutes=30)
SIMPLE_JWT['AUTH_COOKIE'] = 'access_token'
SIMPLE_JWT['AUTH_COOKIE_SECURE'] = not DEBUG
SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'] = True
SIMPLE_JWT['AUTH_COOKIE_SAMESITE'] = 'Lax'
SIMPLE_JWT['AUTH_COOKIE_PATH'] = '/'
SIMPLE_JWT['REFRESH_COOKIE'] = 'refresh_token'
SIMPLE_JWT['REFRESH_COOKIE_PATH'] = '/api/v1/auth/token/refresh/'

# Middleware sets `Cache-Control: no-store` for these paths (exact prefix match).
NO_STORE_PATHS = [
    '/api/v1/auth/csrf/',
    '/api/v1/auth/token/',
    '/api/v1/auth/token/refresh/',
    '/api/v1/auth/otp/send/',
    '/api/v1/auth/otp/verify/',
    '/api/v1/auth/logout/',
    '/api/v1/auth/profile/',
]
# Dynamic-path endpoints that also require no-store (set directly in view):
#   GET  /me/requests/{public_id}/         (authenticated resolver)

# Web Push VAPID keys
WEBPUSH_VAPID_PRIVATE_KEY = config('VAPID_PRIVATE_KEY', default='')
WEBPUSH_VAPID_PUBLIC_KEY = config('VAPID_PUBLIC_KEY', default='')
WEBPUSH_VAPID_ADMIN_EMAIL = config('VAPID_ADMIN_EMAIL', default='admin@refuje.com')

# --- Gupshup WhatsApp API (primary OTP channel) ---
# API endpoint: POST https://api.gupshup.io/wa/api/v1/template/msg
# Auth: apikey header
# Template type: utility (authentication templates not available for Indian businesses)
GUPSHUP_WA_API_KEY = config('GUPSHUP_WA_API_KEY', default='')       # WhatsApp API key from Gupshup dashboard
GUPSHUP_WA_SOURCE_PHONE = config('GUPSHUP_WA_SOURCE_PHONE', default='')  # registered WhatsApp Business number (E.164, e.g. 919XXXXXXXXXX)
GUPSHUP_WA_APP_NAME = config('GUPSHUP_WA_APP_NAME', default='FieldGuide')  # Gupshup app name for src.name param
GUPSHUP_WA_OTP_TEMPLATE_ID = config('GUPSHUP_WA_OTP_TEMPLATE_ID', default='')  # approved utility template ID (UUID)
# Template text (configured in Gupshup dashboard, approved by Meta):
#   "Your Field Guide verification code is {{1}}. Do not share this code."
# params array: ["123456"] — code appears in body + copy button
GUPSHUP_WA_WEBHOOK_SECRET = config('GUPSHUP_WA_WEBHOOK_SECRET', default='')  # HMAC secret for webhook signature validation
GUPSHUP_WA_FALLBACK_TIMEOUT_SECONDS = 10  # if no WhatsApp delivery confirmation within this window, fire SMS fallback

# --- Gupshup Enterprise SMS API (fallback OTP + escalation SMS) ---
# API endpoint: POST https://enterprise.smsgupshup.com/GatewayAPI/rest
# Auth: userid + password (plain auth scheme over HTTPS)
# OTP method: TWO_FACTOR_AUTH
# DLT compliance: required for India — register entity + OTP SMS template on DLT platform
GUPSHUP_SMS_USERID = config('GUPSHUP_SMS_USERID', default='')       # numeric account ID
GUPSHUP_SMS_PASSWORD = config('GUPSHUP_SMS_PASSWORD', default='')   # URL-encoded
GUPSHUP_SMS_SENDER_MASK = config('GUPSHUP_SMS_SENDER_MASK', default='FLDGDE')  # DLT-approved sender ID (6 chars transactional)
GUPSHUP_SMS_DLT_TEMPLATE_ID = config('GUPSHUP_SMS_DLT_TEMPLATE_ID', default='')  # 19-digit DLT template URN for OTP SMS
GUPSHUP_SMS_PRINCIPAL_ENTITY_ID = config('GUPSHUP_SMS_PRINCIPAL_ENTITY_ID', default='')  # DLT entity registration ID
GUPSHUP_SMS_OTP_MSG_TEMPLATE = 'Your Field Guide verification code is %code%'  # must contain %code% placeholder
GUPSHUP_SMS_ESCALATION_DLT_TEMPLATE_ID = config('GUPSHUP_SMS_ESCALATION_DLT_TEMPLATE_ID', default='')  # DLT template for escalation SMS alerts

# --- OTP settings (shared across channels) ---
OTP_EXPIRY_SECONDS = 600   # 10 minutes
OTP_CODE_LENGTH = 6
OTP_CODE_TYPE = 'NUMERIC'   # NUMERIC | ALPHABETIC | ALPHANUMERIC
OTP_MAX_ATTEMPTS = 5       # wrong attempts per code before invalidation
OTP_SEND_RATE_PER_PHONE = 3  # max OTPs per phone per hour
OTP_SEND_RATE_PER_IP = 5     # max OTPs per IP per hour

# SSE (Server-Sent Events via Redis pub/sub)
SSE_REDIS_URL = config('SSE_REDIS_URL', default='redis://localhost:6379/2')
SSE_HEARTBEAT_SECONDS = 15  # keep-alive ping interval

# Escalation tier thresholds (minutes from request creation)
# Per-hotel override via Hotel.escalation_tier_minutes (JSONField)
ESCALATION_TIER_MINUTES = [15, 30, 60]

# Celery (Redis as broker, DB for results — same Redis instance as cache, different DB number)
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'           # task results in PostgreSQL (survives Redis restart)
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
# Resilience: ack late so tasks survive worker crash, reject on lost worker
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = 10 * 60              # hard kill after 10 min
CELERY_TASK_SOFT_TIME_LIMIT = 5 * 60          # SoftTimeLimitExceeded after 5 min
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200       # recycle workers to prevent memory leaks
CELERY_WORKER_PREFETCH_MULTIPLIER = 1         # fair scheduling
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 3600}

# Django cache (for rate limiting)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': config('CACHE_REDIS_URL', default='redis://localhost:6379/1'),
    }
}

# Upload limits
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # 5 MB
```

### 1.10 New Python Dependencies

```
django-filter>=25.2
django-celery-beat>=2.8.1
django-celery-results>=2.6.0
celery[redis]>=5.6.2
django-ratelimit>=4.1.0
qrcode[pil]>=8.2
pywebpush>=2.3.0
py-vapid>=1.9.4
Pillow>=12.1.1  # already likely installed, needed for image processing
redis>=7.2.0
requests>=2.32.5  # Gupshup WhatsApp API + Enterprise SMS API calls
```

### 1.11 URL Integration (`tcomp/urls.py`)

```python
# Existing (upgraded — users app now serves cookie-based auth views):
path('api/v1/auth/', include('users.urls')),   # Single auth route source-of-truth
# New:
path('api/v1/', include('concierge.urls')),     # hotels/* + me/* routes only — NO auth/* routes
```

**Concierge must not register any `/auth/*` URL patterns.** `concierge/urls.py` defines `hotels/`, `me/`, and `webhooks/` prefixes.

---

## Part 2: Frontend — Guest-Facing Hotel Pages + Admin Dashboard

### 2.1 New Route Structure

```
src/app/
├── (hotel)/
│   └── h/[hotelSlug]/
│       ├── page.tsx              # Hotel landing page (departments grid). Reads `?qr=` param from URL, stores in state, threads through OTP flow to `verifyOTP()`.
│       ├── layout.tsx            # Hotel context provider + guest auth check
│       ├── [deptSlug]/
│       │   └── page.tsx          # Department detail + experience list
│       ├── verify/
│       │   └── page.tsx          # Guest OTP flow (phone → code → room number)
│       └── request/
│           └── page.tsx          # Request form (modal or page for deep-link)
├── (dashboard)/
│   └── dashboard/
│       ├── layout.tsx            # Auth guard + sidebar + notification bell
│       ├── page.tsx              # Redirect to first hotel or hotel picker
│       ├── requests/
│       │   └── [publicId]/
│       │       └── page.tsx      # Push deep-link resolver route (opaque public_id, no hotel slug)
│       ├── [hotelSlug]/
│       │   ├── page.tsx          # Dashboard home (stats cards, recent requests)
│       │   ├── requests/
│       │   │   └── page.tsx      # Request queue (filterable table, SSE-driven)
│       │   ├── departments/
│       │   │   ├── page.tsx      # Department list + create
│       │   │   └── [deptSlug]/
│       │   │       └── page.tsx  # Department edit + experiences CRUD
│       │   ├── team/
│       │   │   └── page.tsx      # Member management (SUPERADMIN only)
│       │   ├── qr-codes/
│       │   │   └── page.tsx      # QR generator + list
│       │   └── settings/
│       │       └── page.tsx      # Hotel settings (timezone, room validation, escalation enabled toggle + config)
├── login/
│   └── page.tsx                  # Login page — two tabs: "Email" (email+password) and "Phone" (OTP). Both → JWT cookie.
```

### 2.2 New Lib Files

```
src/lib/
├── auth.ts               # Cookie-based auth: authFetch(), loginWithEmail(), loginWithOTP(), logout, NO token storage
├── guest-auth.ts         # Guest OTP auth: sendOTP(), verifyOTP(), updateRoom() — backend sets httpOnly JWT cookies
├── concierge-api.ts      # All concierge API functions (guest + admin)
├── concierge-types.ts    # TypeScript types for all concierge models
├── push.ts               # Push subscription helper (subscribe/unsubscribe)
├── use-request-stream.ts # SSE hook: useRequestStream(hotelSlug) — subscribes to /requests/stream/, returns live request events
```

### 2.3 Auth System (`src/lib/auth.ts`) — Cookie-Based

**No tokens in JavaScript.** All auth is via httpOnly cookies.

```typescript
const API = getClientApiUrl();

/** Read Django's CSRF token from the csrftoken cookie. */
function getCSRFToken(): string {
  const match = document.cookie.match(/csrftoken=([^;]+)/);
  return match ? match[1] : '';
}

/** Ensure the CSRF cookie exists (call once on app boot / after login). */
export async function ensureCSRFCookie(): Promise<void> {
  // GET /api/v1/auth/csrf/ sets the csrftoken cookie (no-op if already set)
  await fetch(`${API}/api/v1/auth/csrf/`, { credentials: 'include' });
}

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export async function authFetch(url: string, options: RequestInit = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers);

  // Attach CSRF token for all unsafe methods (required for cookie auth)
  if (UNSAFE_METHODS.has(method)) {
    headers.set('X-CSRFToken', getCSRFToken());
  }

  const res = await fetch(url, {
    ...options,
    headers,
    credentials: 'include',  // sends httpOnly cookies automatically
  });

  if (res.status === 401) {
    // Try refresh
    const refreshRes = await fetch(`${API}/api/v1/auth/token/refresh/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': getCSRFToken() },
      credentials: 'include',
    });
    if (refreshRes.ok) {
      // Retry original request with fresh cookies
      return fetch(url, { ...options, headers, credentials: 'include' });
    }
    // Refresh failed — redirect to login
    window.location.href = '/login';
  }

  return res;
}

export async function loginWithEmail(email: string, password: string) {
  await ensureCSRFCookie();

  const res = await fetch(`${API}/api/v1/auth/token/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCSRFToken(),
    },
    credentials: 'include',
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error('Invalid credentials');
  return res.json(); // returns user info, token is in httpOnly cookie
}

export async function sendStaffOTP(phone: string) {
  await ensureCSRFCookie();

  const res = await fetch(`${API}/api/v1/auth/otp/send/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCSRFToken(),
    },
    credentials: 'include',
    body: JSON.stringify({ phone }),  // no hotel_slug — staff are hotel-independent
  });
  if (!res.ok) throw new Error('Failed to send OTP');
}

export async function loginWithOTP(phone: string, code: string) {
  await ensureCSRFCookie();

  const res = await fetch(`${API}/api/v1/auth/otp/verify/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCSRFToken(),
    },
    credentials: 'include',
    body: JSON.stringify({ phone, code }),  // no hotel_slug for staff
  });
  if (!res.ok) throw new Error('Invalid OTP');
  return res.json(); // returns user info + memberships, token is in httpOnly cookie
}

export async function logout() {
  await fetch(`${API}/api/v1/auth/logout/`, {
    method: 'POST',
    headers: { 'X-CSRFToken': getCSRFToken() },
    credentials: 'include',
  });
}
```

**CSRF flow end-to-end:**
1. On dashboard origin app mount (or login page load), call `ensureCSRFCookie()` → `GET /api/v1/auth/csrf/` sets the `csrftoken` cookie (non-httpOnly, readable by JS)
2. Every unsafe request (POST/PATCH/DELETE) reads the cookie and sends `X-CSRFToken` header
3. Django validates the header against the cookie (standard double-submit pattern)
4. `CSRF_COOKIE_DOMAIN = '.refuje.com'` in prod ensures the cookie works across `refuje.com` → `api.refuje.com`
5. Auth endpoints (`/auth/csrf`, `/auth/token`, `/auth/token/refresh`, `/auth/otp/send`, `/auth/otp/verify`, `/auth/logout`, `/auth/profile`) must return `Cache-Control: no-store` to prevent CDN/proxy caching side effects.

### 2.4 Auth Context (`src/context/AuthContext.tsx`)

```typescript
interface AuthContextValue {
  user: User | null;
  membership: HotelMembership | null;  // current hotel's membership
  isLoading: boolean;
  loginWithEmail: (email: string, password: string) => Promise<void>;
  loginWithOTP: (phone: string, code: string) => Promise<void>;
  sendStaffOTP: (phone: string) => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
  role: Role | null;
}
```

- On mount: call `/api/v1/auth/profile/` with `credentials: 'include'` to check if session is valid
- No timers needed — cookie expiry is handled by the browser, refresh is handled by `authFetch()`
- Wrap `(dashboard)` layout with this provider

### 2.5 Guest Auth (`src/lib/guest-auth.ts`)

- **No token in JavaScript.** Backend sets httpOnly JWT cookies on OTP verification (same cookie system as staff auth).
- `sendOTP(phone, hotelSlug)` → `POST /api/v1/auth/otp/send/` with `credentials: 'include'` + `X-CSRFToken`. **Delivery channel (WhatsApp/SMS) is transparent to frontend** — backend handles channel selection and fallback; frontend only knows "OTP was sent".
- `verifyOTP(phone, code, hotelSlug, qrCode?)` → `POST /api/v1/auth/otp/verify/` with `{ phone, code, hotel_slug, qr_code }` → backend sets JWT httpOnly cookies, returns `{ user, stay_id }`. Frontend stores `stay_id` in component state (not localStorage) for the room-number PATCH. `qrCode` is read from URL `?qr=` param on the hotel landing page and threaded through the OTP flow.
- `updateRoom(hotelSlug, stayId, roomNumber)` → `PATCH /hotels/{hotelSlug}/stays/{stayId}/` → updates room number on guest stay
- Guest page calls `GET /api/v1/auth/csrf/` on mount to obtain `csrftoken` cookie for CSRF double-submit on POSTs
- OTP flow triggers on first CTA tap (e.g. "Book" button); backend returns 401 if no valid JWT cookie exists → redirect to verify page
- "Your session has expired, please verify again" message on 401 from guest endpoints

### 2.6 Key Components

**Guest-facing (`src/components/hotel/`):**
- `HotelHeader.tsx` — hotel logo, name, description
- `DepartmentCard.tsx` — photo, name, description, experience count, schedule badge ("Open today: 9-9")
- `ExperienceCard.tsx` — photo, name, price, timing, "Book" button
- `OTPFlow.tsx` — 3-step guest verification: phone input → OTP code entry → room number entry. On verify success, redirects to request form or previous CTA target.
- `RequestModal.tsx` — form modal: experience pre-filled, guest_name (pre-filled for returning guests), date/time/guests/notes, submit
- `RequestConfirmation.tsx` — immediate acknowledgement card after submit (`Request received`, expected response window, after-hours message)

**Dashboard (`src/components/dashboard/`):**
- `Sidebar.tsx` — nav: Dashboard, Requests, Departments, Team, QR Codes, Settings (role-filtered)
- `RequestTable.tsx` — filterable/sortable table of requests with status badges + state machine actions. **Driven by SSE** (`useRequestStream` hook) — updates in real-time without polling.
- `RequestDetail.tsx` — full request view + activity timeline + status actions (only valid transitions shown)
- `StatsCards.tsx` — today's requests, pending, confirmed, conversion rate
- `DepartmentForm.tsx` — create/edit department with photo upload + schedule editor
- `ExperienceForm.tsx` — create/edit experience with photo upload (validates JPG/PNG/WebP client-side too)
- `MemberTable.tsx` — list members (shows email, phone, role), invite new (email + optional phone), change role
- `QRGenerator.tsx` — select placement (dropdown: Lobby, Room, Restaurant, Spa, Pool, Bar, Gym, Garden, Other) + label (freeform) + optional department → generate → preview QR image with `target_url` shown → download PNG. List of existing QR codes with placement, label, stay count (OTP-verified stays), and active/inactive toggle.
- `NotificationBell.tsx` — bell icon with unread count badge, dropdown list
- `StatusBadge.tsx` — color-coded status pill
- `EscalationHealth.tsx` — small indicator showing if escalation system is healthy (for SUPERADMIN). **Only rendered when `hotel.escalation_enabled=True`** — hidden otherwise.
- `EscalationDegradedBanner.tsx` — visible warning when heartbeat stale/failing ("Escalations degraded. Monitor queue manually."). **Only rendered when `hotel.escalation_enabled=True`.**

### 2.7 PWA Setup

**Files to create:**
- `public/manifest.json` — app name, icons, display: standalone, theme color
- `public/sw.js` — service worker for push notifications only (no offline caching in v1)
- `src/lib/push.ts` — subscribes to push, sends subscription to backend

**Service worker (`public/sw.js`):**
```javascript
self.addEventListener('push', (event) => {
  const data = event.data.json();
  // Payload: { title, body, url, request_public_id } — no guest PII
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/icon-192.png',
      data: { url: data.url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data.url));
});
```

### 2.8 New Frontend Dependencies

```
# None — using native fetch, Tailwind for styling, no UI library
```

### 2.9 Concierge Types (`src/lib/concierge-types.ts`)

```typescript
export type Role = 'SUPERADMIN' | 'ADMIN' | 'STAFF';

export type RequestStatus =
  | 'CREATED' | 'ACKNOWLEDGED'
  | 'CONFIRMED' | 'NOT_AVAILABLE' | 'NO_SHOW' | 'ALREADY_BOOKED_OFFLINE'
  | 'EXPIRED';

// State machine: which transitions are valid from each state
export const VALID_TRANSITIONS: Record<RequestStatus, RequestStatus[]> = {
  CREATED: ['ACKNOWLEDGED'],
  ACKNOWLEDGED: ['CONFIRMED', 'NOT_AVAILABLE', 'NO_SHOW', 'ALREADY_BOOKED_OFFLINE'],
  CONFIRMED: [],
  NOT_AVAILABLE: [],
  NO_SHOW: [],
  ALREADY_BOOKED_OFFLINE: [],
  EXPIRED: [],
};

export type RequestType = 'BOOKING' | 'INQUIRY' | 'CUSTOM';
export type NotificationType = 'NEW_REQUEST' | 'ESCALATION' | 'DAILY_DIGEST' | 'SYSTEM';

export interface Hotel {
  id: number; name: string; slug: string; description: string;
  tagline: string; logo: string | null; cover_image: string | null;
  timezone: string;
}

export interface Department {
  id: number; name: string; slug: string; description: string;
  photo: string | null; icon: string | null; display_order: number;
  schedule: DepartmentSchedule; experiences: Experience[];
  is_ops: boolean; is_active: boolean;
}

export interface DepartmentSchedule {
  timezone: string;
  default: [string, string][];
  overrides?: Record<string, [string, string][]>;
}

export interface Experience {
  id: number; name: string; slug: string; description: string;
  photo: string | null; cover_image: string | null;
  price_display: string; category: string;
  timing: string; duration: string; capacity: string | null;
  highlights: string[];
}

export interface GuestStay {
  id: number; hotel: number; room_number: string;
  is_active: boolean; created_at: string; expires_at: string;
}

// guest_name is sourced from guest_stay.guest (User model).
// room_number is sourced from guest_stay.room_number.
// guest_name from guest_stay.guest (User), room_number from guest_stay.
export interface ServiceRequest {
  id: number; public_id: string; status: RequestStatus; request_type: RequestType;
  guest_name: string; room_number: string;
  experience: Experience | null; department: Department;
  guest_notes: string; guest_date: string | null;
  guest_time: string | null; guest_count: number | null;
  staff_notes: string; after_hours: boolean;
  confirmation_reason?: string | null; response_due_at?: string | null;
  ack_message?: string;
  created_at: string; acknowledged_at: string | null; confirmed_at: string | null;
  activities: RequestActivity[];
}

export interface RequestActivity {
  action: string; actor_name: string | null;
  details: Record<string, unknown>; created_at: string;
}

export interface HotelMembership {
  id: number; user: UserMinimal; role: Role;
  department: Department | null; is_active: boolean;
}

export interface UserMinimal {
  id: number; email: string; first_name: string; last_name: string;
}

export interface Notification {
  id: number; title: string; body: string;
  notification_type: NotificationType; is_read: boolean;
  created_at: string; request_public_id: string | null;
}

export interface DashboardStats {
  total_requests: number; pending: number; acknowledged: number;
  confirmed: number; conversion_rate: number;
  by_department: { name: string; count: number }[];
}
```

---

## Part 3: Files to Modify in Existing Codebase

### Backend modifications:
| File | Change |
|------|--------|
| `tcomp/tcomp/settings/base.py` | Add `concierge` + `django_filters` + `django_celery_beat` + `django_celery_results` to INSTALLED_APPS; update REST_FRAMEWORK auth + filter backends; add SIMPLE_JWT cookie settings; add Celery + Redis + VAPID + upload limit settings; add Celery resilience settings (ack late, reject on lost, time limits) |
| `tcomp/tcomp/middleware.py` | Add no-store middleware matching `NO_STORE_PATHS` (auth endpoints → `Cache-Control: no-store`). Dynamic-path no-store (e.g. `/me/requests/{public_id}/`) is handled at view level. |
| `tcomp/tcomp/settings/dev.py` | Add `SIMPLE_JWT.AUTH_COOKIE_SECURE = False`; **append** `rest_framework_simplejwt.authentication.JWTAuthentication` to `DEFAULT_AUTHENTICATION_CLASSES` (Bearer fallback for Postman/curl — dev-only, never in base or prod); add `CSRF_TRUSTED_ORIGINS` + `CORS_ALLOWED_ORIGINS` for `localhost:6001` |
| `tcomp/tcomp/settings/prod.py` | Explicitly confirm **no Bearer auth** — do NOT add `JWTAuthentication`. Inherits cookie-only from base. Add `CSRF_COOKIE_DOMAIN = '.refuje.com'` + `SIMPLE_JWT['AUTH_COOKIE_DOMAIN'] = '.refuje.com'` |
| `tcomp/tcomp/urls.py` | Add `path('api/v1/', include('concierge.urls'))` for hotels/* + me/* routes. Keep existing `path('api/v1/auth/', include('users.urls'))` as single auth route source. |
| `tcomp/users/models.py` | Add `user_type` field (CharField, choices: `STAFF`/`GUEST`, default `STAFF`) and `phone` field (`CharField(max_length=20, blank=True, default='')`). **Phone uniqueness:** enforce with a partial unique constraint/index for non-blank values (`phone > ''`). **Email for guests:** change `email` from `EmailField(unique=True)` to `EmailField(blank=True, default='')`; enforce uniqueness via partial unique constraint/index for non-blank values (`email > ''`). Keep `USERNAME_FIELD = 'email'` — staff can log in by email+password OR phone+OTP; guests use phone+OTP only. Both OTP paths bypass `TokenObtainPairSerializer` (JWT issued manually via `RefreshToken.for_user()`). |
| `tcomp/users/managers.py` | Refactor `UserManager` to support guest creation: add `create_guest_user(phone, **extra_fields)` method that creates a User with `user_type='GUEST'`, `phone=phone`, `email=''`, `set_unusable_password()`. The existing `_create_user(email, password)` remains unchanged for staff. `create_guest_user` must NOT call `_create_user` (which requires email). |
| `tcomp/users/views.py` | Replace Bearer-only `TokenObtainPairView`/`TokenRefreshView`/`ProfileView` with cookie-based equivalents (`CookieTokenObtainPairView`, `CookieTokenRefreshView`, `AuthProfileView`). Add `CSRFTokenView`, `LogoutView`, `OTPSendView`, `OTPVerifyView`. Remove `RegisterView` (registration not exposed in v1 — staff invited via SuperAdmin). **OTPVerifyView** handles both user types: looks up phone → if existing STAFF user, issues JWT (no GuestStay); otherwise follows guest flow (create User if new + GuestStay). Issues JWT manually: `refresh = RefreshToken.for_user(user)` → sets httpOnly cookies. Does NOT use `TokenObtainPairSerializer` (which expects email+password). **AuthProfileView** supports GET (read) and PATCH (update phone/name via `AuthProfileUpdateSerializer`). |
| `tcomp/users/urls.py` | Update URL patterns: add `csrf/`, `logout/`, `otp/send/`, `otp/verify/`; keep `token/`, `token/refresh/`, `profile/` (now pointing to cookie-based views, GET+PATCH). Remove `register/`. |
| `tcomp/users/serializers.py` | Add `UserMinimalSerializer` (id, email, first_name, last_name, phone) + `AuthProfileSerializer` (user + hotel memberships/stays) + `AuthProfileUpdateSerializer` (phone, first_name, last_name — phone validated for uniqueness) + `OTPSendSerializer` + `OTPVerifySerializer` — reused by concierge |
| `tcomp/tcomp/celery.py` | New file — Celery app configuration (`app = Celery('tcomp')`, `config_from_object('django.conf:settings', namespace='CELERY')`, `autodiscover_tasks()`). Include `task_prerun`/`task_postrun` signals to call `close_old_connections()` (prevents stale PostgreSQL connections in long-lived workers). |
| `tcomp/tcomp/__init__.py` | Import celery app for autodiscover |

### Frontend modifications:
| File | Change |
|------|--------|
| `src/app/layout.tsx` | Add manifest link, service worker registration script |
| `next.config.ts` | Add CORS/cookie config if needed for dev proxy |

Everything else is **new files** — no modifications to existing guides, location, or site/fieldguide code.

---

## Implementation Order

### Phase 1: Backend Foundation (do first — frontend depends on API)
1. Install new Python packages (django-filter, celery, redis, django-celery-beat, django-celery-results, django-ratelimit, qrcode, pywebpush, py-vapid, requests)
2. Create `.env.example` with all required environment variables: `SECRET_KEY`, `DEBUG`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `CELERY_BROKER_URL` (default `/0`), `CACHE_REDIS_URL` (default `/1`), `SSE_REDIS_URL` (default `/2`), `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_ADMIN_EMAIL`, `GUPSHUP_WA_API_KEY`, `GUPSHUP_WA_SOURCE_PHONE`, `GUPSHUP_WA_APP_NAME`, `GUPSHUP_WA_OTP_TEMPLATE_ID`, `GUPSHUP_WA_WEBHOOK_SECRET`, `GUPSHUP_SMS_USERID`, `GUPSHUP_SMS_PASSWORD`, `GUPSHUP_SMS_SENDER_MASK`, `GUPSHUP_SMS_DLT_TEMPLATE_ID`, `GUPSHUP_SMS_PRINCIPAL_ENTITY_ID`, `GUPSHUP_SMS_ESCALATION_DLT_TEMPLATE_ID`. `TRUSTED_PROXY_IPS` is optional and typically set in `.env.prod` to tighten forwarded-header trust (compose has a safe default). Reference the settings block for full list. (Existing backend uses individual `DB_*` vars, not `DATABASE_URL`. Redis uses three separate env vars to avoid DB-number collapse in prod.)
3. Update `users/models.py` — add `user_type` (STAFF/GUEST) and `phone` fields to User model
4. Create `concierge` app skeleton + all models (GuestStay, OTPCode, ServiceRequest, RequestActivity, Hotel, Department, Experience, Notification, PushSubscription, QRCode, EscalationHeartbeat)
5. Update settings (INSTALLED_APPS, auth cookies, Celery + resilience, Redis cache, upload limits, Gupshup WhatsApp API + SMS API, DLT, OTP, SSE, auth no-store paths)
6. Create Celery config (`celery.py`, `__init__.py`) with DB connection management signals
7. Update `docker-compose.yml` (dev) and `docker-compose.prod.yml` (prod) with web, redis, celery-worker, celery-beat services (see S11 runtime services). Repo already has both files — add the new services to each.
8. Run `makemigrations` + `migrate`
9. Write `authentication.py` (JWTCookieAuthentication)
10. Write `validators.py` (image upload validation)
11. Write `mixins.py` (HotelScopedMixin)
12. Write `permissions.py` (hotel-scoped RBAC)
13. Write `serializers.py` (with ownership validation + state machine enforcement)
14. Write `views.py` + `urls.py` (with custom Redis+DB-fallback rate-limit service calls, object-level request permissions, SSE stream endpoint, Gupshup WhatsApp webhook endpoint)
15. Write `services.py` (QR gen, push, OTP send/verify with WhatsApp-primary + SMS-fallback, WhatsApp webhook handler, SSE publish/stream, escalation, image processing, custom rate-limit with DB fallback)
16. Write `filters.py`
17. Write `signals.py` (minimal/optional; most activity logging, notifications, and SSE publish flow are handled explicitly in views/services)
18. Write `tasks.py` (Celery tasks: escalation, response-due reminders, expiry, WhatsApp OTP fallback sweeper, OTP cleanup, digest)
19. Register models in Django admin (`admin.py`) — include EscalationHeartbeat health display
20. Add `UserMinimalSerializer`, `OTPSendSerializer`, `OTPVerifySerializer` to users app
21. Write cookie-based auth views (login, refresh, logout, CSRF, OTP send, OTP verify)

### Phase 2: Frontend Auth + Dashboard Shell
22. Create `auth.ts` (cookie-based, NO localStorage tokens — `loginWithEmail`, `sendStaffOTP`, `loginWithOTP`), AuthContext, login page with Email and Phone tabs
23. Create `(dashboard)` layout with auth guard + sidebar
24. Create concierge types + API lib (with `credentials: 'include'` everywhere)
25. Create `use-request-stream.ts` — SSE hook for real-time dashboard updates via `GET /hotels/{hotel_slug}/requests/stream/`
26. Create dashboard home (stats cards, recent requests)

### Phase 3: Frontend — Admin CRUD
27. Departments CRUD pages + forms (with schedule editor)
28. Experiences CRUD pages + forms (with image upload validation on client)
29. Team management page (member list, invite, role change)
30. Hotel settings page (timezone, room-number validation rules, escalation enabled toggle, escalation fallback channel/contact, kiosk requirement)
31. QR code generator page

### Phase 4: Frontend — Guest-Facing
32. Hotel landing page (departments grid with schedule badges). Reads `?qr=` param from URL (set by QR code scan); stores in page state and passes through to OTP verify flow.
33. Department detail + experience list
34. Guest OTP verification page (`/h/{hotelSlug}/verify/`) — phone input → OTP code → room number (3-step flow)
35. Create `guest-auth.ts` (sendOTP, verifyOTP(phone, code, hotelSlug, qrCode?), updateRoom — backend sets httpOnly JWT cookies). `qrCode` threaded from URL `?qr=` param through verify call.
36. Request form modal (guest_name pre-filled for returning guests, date/time/guests/notes)
37. Request submission flow (auth check → form → submit → immediate acknowledgement card with SLA)

### Phase 5: Notifications + PWA
38. PWA manifest + service worker (push only, no PII in payloads)
39. Push subscription flow (subscribe on dashboard login, unregister before logout — see S5)
40. Notification bell component + dropdown
41. Backend notifications on new request/escalation (push + optional escalation-only email/SMS fallback)

### Phase 6: Polish
42. Request detail page with activity timeline
43. Request status update flow (only valid state transitions shown as buttons)
44. Escalation health indicator + degraded banner in dashboard (SUPERADMIN)
45. Mobile responsive dashboard
46. Error handling, loading states, empty states

---

## Verification

1. **Backend smoke test:** `python manage.py runserver` — no errors, admin accessible, all models visible
2. **Auth security test:**
   - Login via email+password → verify cookies are httpOnly (browser DevTools → Application → Cookies → no `access_token` visible to JS)
   - Login via phone+OTP (staff user) → same httpOnly cookie result, no GuestStay created
   - `document.cookie` in console → should NOT contain access_token (but SHOULD contain `csrftoken`)
   - `PATCH /api/v1/auth/profile/` (cookie-authenticated) without `X-CSRFToken` header → 403 Forbidden
   - `PATCH /api/v1/auth/profile/` with valid `X-CSRFToken` from cookie → 200 OK
   - `GET /api/v1/auth/csrf/` → sets csrftoken cookie (verify in DevTools)
   - `GET /api/v1/auth/profile/` with valid cookie auth → 200 with current user + memberships (staff) or stays (guest)
   - `GET /api/v1/auth/csrf/`, `/auth/token/`, `/auth/token/refresh/`, `/auth/otp/send/`, `/auth/otp/verify/`, `/auth/logout/`, `/auth/profile/` all return `Cache-Control: no-store`
   - Verify no duplicate auth routes: only `users.urls` mounts `/api/v1/auth/*` — `concierge.urls` has no auth patterns
   - Staff OTP verify without `hotel_slug` → 200 (hotel_slug optional for staff)
   - Staff OTP verify with `hotel_slug` → 200, still no GuestStay created (staff phone detected)
   - `PATCH /auth/profile/` with `{ phone: "+91..." }` → 200, phone updated on user (staff can add phone for OTP login)
   - `PATCH /auth/profile/` with phone already taken by another user → 400 (uniqueness enforced)
   - Guest/unknown phone + no `hotel_slug` → OTP send succeeds (send doesn't branch), but verify returns **400** (`hotel_slug is required for non-staff users`)
3. **Guest OTP auth test:**
   - `POST /auth/otp/send/` with valid phone + hotel_slug → 200 (OTP sent via WhatsApp; falls back to SMS if WhatsApp unavailable)
   - `POST /auth/otp/verify/` with correct code → sets httpOnly JWT cookies (access_token + refresh_token), returns user info + stay_id
   - `document.cookie` in console → should NOT contain access_token (httpOnly)
   - `PATCH /hotels/{hotel_slug}/stays/{stay_id}/` with room_number → 200 (room set on stay)
   - Guest `POST /hotels/{hotel_slug}/requests/` with JWT cookie → succeeds
   - Guest `POST /hotels/{hotel_slug}/requests/` without `X-CSRFToken` → 403 (CSRF enforced for guests)
   - OTP with wrong code 5 times → code invalidated, must request new OTP
   - OTP send rate limit: 4th send to same phone within 1 hour → 429
   - OTP send rate limit: 6th send from same IP within 1 hour → 429
   - Same phone verifying at different hotel → creates new GuestStay, same User reused
   - OTP verify with valid `qr_code` param → GuestStay created with `qr_code` FK set to matching QRCode
   - OTP verify with `qr_code` belonging to different hotel → `qr_code` silently ignored, GuestStay created without FK
   - OTP verify with inactive `qr_code` → silently ignored, GuestStay created without FK
   - OTP verify with no `qr_code` → GuestStay created with `qr_code=NULL` (direct URL access, no QR scan)
   - Guest `GET /me/stays/` → returns stays across all hotels
   - OTP send → `OTPCode.channel` set to `WHATSAPP` when WhatsApp delivery succeeds
   - Webhook `delivered` event → sets `wa_delivered=True`; sweeper skips this OTP (no spurious SMS)
   - OTP send to non-WhatsApp number → Gupshup webhook fires failure (code 1002), SMS fallback triggered, `OTPCode.channel` updated to `SMS`
   - OTP send when WhatsApp API returns 400 synchronously → immediate SMS fallback, `OTPCode.channel` set to `SMS`
   - OTP send when WhatsApp delivery has no webhook confirmation within `GUPSHUP_WA_FALLBACK_TIMEOUT_SECONDS` → sweeper fires SMS fallback (only if `wa_delivered=False`), `OTPCode.channel` updated to `SMS`
   - Webhook endpoint validates HMAC signature → invalid signature returns 403
   - Webhook duplicate/late events for already-expired OTPCode → ignored (idempotent)
4. **Multi-tenant isolation test:**
   - Create 2 hotels with different departments
   - As staff of Hotel A, attempt to access Hotel B's requests → 404
   - Submit request with department_id from Hotel B → 400 validation error
   - Create Experience through admin endpoint (model has no direct `hotel` FK) → succeeds without `perform_create()` kwarg errors
   - Attempt Experience create with Department from another hotel → 400 validation error
   - Create request with `experience=A` and mismatched `department=B` (same hotel) → rejected (400)
   - Create request with `experience=A` and no department value → server derives `department=A.department`
5. **Rate limiting test (Wi-Fi safe):**
   - Submit 11 requests from same guest stay → 11th rejected (429) — stay-primary limit
   - Submit 6 requests from same room (different stays) → 6th rejected — room-primary limit
   - Request-IP secondary limiter is currently deferred; verify stay and room limits are enforced
6. **State machine test:**
   - Try CREATED → CONFIRMED directly → rejected
   - GET request detail → verify status still CREATED (viewing doesn't acknowledge)
   - POST /acknowledge/ → status becomes ACKNOWLEDGED → escalation stops
   - ACKNOWLEDGED → CONFIRMED → try changing again → rejected
   - Verify EXPIRED is system-only (staff can't set it)
7. **Guest stay + room validation test:**
   - Verify OTP → stay created with 24h expiry → wait 24h (or mock) → guest request rejected (stay expired)
   - Set room number with blocked value (`9999`) or invalid pattern → rejected
   - Set room number with valid pattern → accepted
   - Revoke stay from dashboard → guest gets 401 on next request
   - **Cross-hotel revocation test**: Staff of Hotel A tries to revoke stay from Hotel B via URL manipulation → 404 (HotelScopedMixin filters it out)
   - Guest with expired stay re-verifies OTP → new GuestStay created, same User, room pre-filled from previous stay
8. **Upload test:**
   - Upload SVG → rejected
   - Upload 10MB image → rejected
   - Upload valid JPEG → accepted, EXIF stripped, resized
9. **Escalation test:**
   - **Hotel with `escalation_enabled=False`:** create request → wait past tier 1 threshold → no escalation notification (scheduler skips this hotel)
   - **Hotel with `escalation_enabled=True`:** create request to experience dept → wait ≥15 min (or mock time past tier 1 threshold) → tier 1 escalation notification appears
   - On-call email/SMS escalation fallback is planned but not yet implemented; verify in-app/push escalation path
   - If fallback channel is NOT configured but `escalation_enabled=True`, onboarding enforces `require_frontdesk_kiosk=True`
   - Acknowledge request → no more escalations
   - Create request to experience dept outside hours → `after_hours=True`
   - Same after-hours experience request also triggers immediate informational notification to fallback/front desk
   - Fallback/front desk can call `POST /take-ownership/` and request remains in original department with updated `assigned_to`
   - Response payload includes `response_due_at`
   - If `response_due_at` passes and status is still `CREATED`, reminder notification is triggered
   - Escalation currently uses wall-clock elapsed time; schedule-window pause/resume and ops-specific routing are deferred
   - **Overnight shift test**: dept schedule `["22:00","06:00"]` → at 23:00 dept is active, at 07:00 dept is inactive
   - Toggle `escalation_enabled` from True to False → scheduler stops processing this hotel on next run
10. **Bearer auth disabled in prod:**
    - In prod settings, send request with `Authorization: Bearer <valid_token>` → rejected (cookie-only)
    - In dev settings, same request → accepted (dev-only fallback)
11. **CSP test:**
    - Guest page: inject `<script>alert(1)</script>` in experience description → rendered as plain text, not executed
    - Check response headers include strict CSP for guest routes
12. **Data retention test:**
    - All data (guest profiles, notes, stay history) retained permanently — no scrubbing in v1
    - Run `cleanup_expired_otps_task` → OTPCode records older than 24h deleted (only transient auth data cleaned)
    - Dashboard stats show correct aggregate counts
13. **SSE real-time test:**
    - Staff connects to `GET /hotels/{hotel_slug}/requests/stream/` → receives SSE connection with periodic heartbeat
    - Guest submits request → staff SSE stream receives `request.created` event
    - Staff acknowledges request → SSE stream receives `request.updated` event
    - Escalation processing may generate notifications/activity, but current SSE stream publishes request create/update events only
    - STAFF user only receives events for own department; ADMIN/SUPERADMIN receives all hotel events
    - Unauthenticated request to SSE endpoint → 401
14. **Frontend test:**
    - Login → dashboard with stats
    - Create department + experiences → appears on guest page
    - Guest flow: hotel page → experience → OTP verify (phone → code → room) → request form → confirmation view shows immediate acknowledgement + SLA
    - Returning guest: phone pre-recognized, name pre-filled on request form
    - Push notification on new request → click opens `/dashboard/requests/{publicId}` → calls `/me/requests/{public_id}/` to resolve hotel context
    - Dashboard request detail URLs use opaque `public_id` route (`/dashboard/requests/{publicId}`), never numeric request IDs or hotel slug
    - Dashboard request table updates in real-time via SSE
15. **Cache failure mode test (Redis down):**
    - Stop Redis → submit guest request under limits → succeeds (DB-backed fallback limits still enforced)
    - Stop Redis → exceed per-stay/per-room limits → rejected (429) via DB fallback counts
    - Stop Redis → OTP send still works (Gupshup WhatsApp/SMS APIs are external; rate limit falls back to DB count of recent OTPCode records; WhatsApp timeout fallback uses DB-stored `created_at` instead of cache timer)
    - If cache fallback path also hits DB failures, expect generic server error (5xx) in current backend
    - Check logs for cache failure warnings/degraded mode
16. **RBAC test:**
    - STAFF sees own department's requests only
    - STAFF cannot open detail/ack/update for another department's request (404/403 via object permission check)
    - Push clickthrough via `public_id` for out-of-scope request returns 404/403
    - `/me/requests/{public_id}/` returns same data as hotel-scoped route (permission parity)
    - ADMIN can manage departments but not team
    - SUPERADMIN can do everything
17. **Status/reason consistency test:**
    - Set terminal status without `confirmation_reason` → allowed
    - Set non-terminal status with `confirmation_reason` → rejected
    - Set terminal status with disallowed detail code enum for that status → rejected
    - Set `confirmation_reason` equal to status literal (e.g., `NOT_AVAILABLE`) → rejected (reason must be detail code)
18. **Escalation resilience test** (requires `escalation_enabled=True`):
    - Force stale heartbeat → dashboard shows "Escalations degraded" banner
    - Hotel with `escalation_enabled=False` → no "Escalations degraded" banner even with stale heartbeat (widget hidden)
    - Verify redundant systemd timer still executes `check_escalations` when Celery broker is unavailable
    - **Scheduler dedupe**: trigger both Celery Beat and systemd timer simultaneously → only one escalation notification per `(request_id, escalation_tier)` per request lifetime (partial unique index rejects duplicate inserts atomically)
19. **Activity action/status alignment test:**
    - Set status to NOT_AVAILABLE → activity logged with `action='CLOSED'` and `details.status_to='NOT_AVAILABLE'`
    - Set status to CONFIRMED → activity logged with `action='CONFIRMED'` (not `CLOSED`)
    - No `REJECTED` action exists in the activity enum — assert enum values match: `CREATED, VIEWED, ACKNOWLEDGED, CONFIRMED, CLOSED, ESCALATED, NOTE_ADDED, EXPIRED`
20. **Phase-2 token safety test:**
    - Assert no API serializer includes `confirmation_token`
    - Admin/dashboard responses do not expose `confirmation_token` fields

### Object-permission test cases (must pass)

Automated tests to ensure object-level permissions are enforced for **all** request routes (by `id` and by `public_id`):

- STAFF cannot `GET/PATCH/ACK` any request outside their **department** (even with direct URL).
- ADMIN cannot access any data outside their **hotel** (id/public_id/detail/list).
- `ServiceRequestDetailByPublicId` uses `CanAccessRequestObject` (same as ID route — hotel from URL slug).
- `/me/requests/{public_id}/` uses `CanAccessRequestObjectByLookup` (derives hotel from request object — same STAFF/ADMIN/SUPERADMIN rules, different hotel resolution path).
- Guest with active stay at Hotel A cannot submit request to Hotel B (stay binding enforced).

- ✅ Assert `confirmation_token` is **excluded** from all DRF serializers and Django admin list/display/export in v1 (unit test).


## Pilot operations (additions)

### Pilot operational dependency: always-on front desk queue

To avoid “silent failures” (push disabled, PWA not installed, devices muted), each pilot hotel must have **one always-on front desk device** (tablet/desktop) logged into the dashboard and pinned to the **Requests Queue**.

- If `escalation_enabled=True`: during onboarding, record **front desk escalation contacts** (SMS + email) and run a **daily contact test** (automated or manual): send a test escalation and confirm receipt.
- If escalation contacts are invalid or delivery fails, show a dashboard banner: **"Escalation delivery degraded — verify contacts."**
- Hotels with `escalation_enabled=False` skip escalation setup entirely — the always-on front desk queue is still recommended but escalation contacts are not required.

## Final hardening before pilots (must-have)

### Auth security — RESOLVED (see S1, S2, S10)
v1 canonical: Both staff and guests can authenticate via phone + OTP. Staff also have email+password login. On verification, backend sets httpOnly JWT cookies. `SameSite=Lax`, `Secure` in prod. No localStorage token storage. CSP is defense-in-depth. Guest unsafe methods require CSRF (`X-CSRFToken`). GuestStay links guest to hotel with 24h TTL. See S1, S2, and S10 for full details.

### Cache/Redis down behavior — RESOLVED (see S9)
Canonical behavior defined in S9:
- **Guest request create:** DB-backed fallback limits (stay/room count queries) + 429 when exceeded. Not unlimited default-allow.
- **OTP rate limiting:** DB fallback via `OTPCode` table counts. If cache fallback path also hits DB failures, current backend surfaces generic 5xx.
- Logging/alerts: cache failure warnings are logged; threshold-based limiter degradation alerting is planned.

### Escalation reliability (don't rely on someone watching the dashboard)
- **Applies only to hotels with `escalation_enabled=True`.** Hotels that haven't opted in are unaffected.
- Keep heartbeat + dashboard indicator, but add **out-of-band alerting**:
  - If escalation worker/beat heartbeat is stale for >15 minutes, send email/SMS to Field Guide ops + hotel GM (during pilot) — planned; not yet implemented in backend code.

### Staff dashboard real-time — RESOLVED (SSE, promoted to Phase 2)
`useRequestStream(hotelSlug)` SSE hook connects to `GET /hotels/{hotel_slug}/requests/stream/` for real-time updates. Push notifications remain optional acceleration. SSE is the primary real-time delivery mechanism for staff dashboard. See S2 user decisions.

### Opaque public_id routes (permission parity) — RESOLVED (promoted to core spec + verification)
- `ServiceRequestDetailByPublicId` (hotel-scoped) uses `CanAccessRequestObject` — same as numeric ID route.
- `/me/requests/{public_id}/` (slug-less resolver) uses `CanAccessRequestObjectByLookup` — derives hotel from `request.hotel`, applies same STAFF/ADMIN/SUPERADMIN role rules.
- Automated tests for cross-department and cross-role access via `public_id` are in main verification checklist (tests 16 + object-permission section).

### Canonical auth endpoints + CSRF flow — RESOLVED (see S1, S11, auth endpoint table)
Auth is owned by `users` app at `/api/v1/auth/*`. Concierge must not register duplicate auth routes. Frontend `authFetch()`: bootstraps CSRF cookie (no-store), always sends `credentials: 'include'`, always sets `X-CSRFToken` for unsafe methods. Guest pages use the same CSRF flow for guest POSTs. OTP endpoints accept anonymous requests; CSRF is enforced when a valid auth cookie is used for authentication.

### Invariant enforcement (experience → department)
- On request create: if `experience` is present, **derive** `department = experience.department` and reject mismatches.
- Ensure all request creation paths go through the same serializer/service to prevent bypass.
