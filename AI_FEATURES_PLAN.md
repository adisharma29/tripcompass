# AI Features Plan — Field Guide Concierge Platform

## Personas Today

| Persona | Auth method | Core workflow | Biggest friction |
|---|---|---|---|
| **Guest** | Phone + OTP → 24h GuestStay | Browse departments/experiences/events → submit ServiceRequest (BOOKING/INQUIRY/CUSTOM) → track status via SSE | Must navigate to correct department, pick correct experience, fill form fields manually. High drop-off for "I just want a cab at 6am." |
| **Staff** | Email + password or Phone + OTP | See requests in list/SSE → acknowledge → confirm/decline → add notes | Every request is triaged manually. No signal for urgency, no suggested responses, no pattern recognition across requests. |
| **Admin** | Same as Staff (ADMIN/SUPERADMIN role) | Create departments, experiences, events, manage members, configure hotel settings | Writing descriptions, highlights (JSON arrays), timing strings for 30+ experiences is hours of tedious content work per hotel. |
| **Platform** | Django admin | Onboard hotels, monitor escalation heartbeats | Each hotel is a silo. No cross-hotel intelligence. No way to surface "guests keep asking for X and we don't offer it." |

---

## Proposed AI Features (ranked by real value)

### 1. Natural Language Guest Requests

**Persona:** Guest
**Problem:** Current flow requires 4+ steps: navigate department → pick experience → select type → fill guest_notes/date/time/count. Most guests just want to say what they need.

**What it does:**
- New endpoint: `POST /api/v1/hotels/{hotel_slug}/ai/request/`
- Accepts `{ "message": "Can I book the sunset kayak trip for 2 people tomorrow at 4pm?" }`
- LLM resolves against the hotel's actual published departments, experiences, and events (grounded — no hallucination of options that don't exist)
- Extracts structured fields: `department`, `experience`, `event`, `request_type`, `guest_date`, `guest_time`, `guest_count`
- Returns a preview for the guest to confirm, then creates the `ServiceRequest`
- Falls back to `request_type=CUSTOM` with the raw message as `guest_notes` when it can't classify

**Why it's real value, not cosmetic:**
- Collapses 4 steps into 1. Directly increases request conversion.
- Uses your existing data model — the LLM picks from your hotel's actual catalog, not invented options.
- Backend-only. Can ship as an API endpoint before the frontend phases.

**Grounding approach:**
- On each request, query the hotel's published departments + experiences + events (already prefetched in `HotelPublicDetail`).
- Build a structured context: `[{"department": "Dining", "slug": "dining", "experiences": [{"name": "Sunset Dinner", "slug": "sunset-dinner", "category": "DINING", "timing": "7pm-10pm"}, ...]}]`
- System prompt instructs the LLM to ONLY select from provided options, or return `null` for unresolvable fields.
- Structured output (tool use / function calling) ensures you get JSON back, not free text.

**Risk:** LLM latency (~1-2s). Mitigate by making it a two-step flow: parse → preview → confirm.

---

### 2. Smart Request Routing + Auto-Triage

**Persona:** Staff
**Problem:** Staff sees all requests for their department in chronological order. A "lost room key" and a "dinner booking for next Tuesday" look identical in the list. After-hours escalation is purely time-based (15/30/60 min tiers), not intent-based.

**What it does:**
- On `ServiceRequest` creation, a lightweight classifier (can be a small model or even a prompted LLM) analyzes `guest_notes` + `request_type` + `experience.category` + `after_hours` flag
- Outputs:
  - **Urgency tag** (URGENT / NORMAL / LOW) — stored as a new field on `ServiceRequest`
  - **Suggested department** — when guest used the fallback/custom path and the request clearly belongs to a specific department
  - **Suggested response** — pre-filled `staff_notes` template for common patterns ("Thank you for your dinner reservation request. We have availability at...")
- Urgency tag is used in the dashboard and SSE stream to surface urgent requests first
- Suggested department enables a "reroute" prompt in the staff UI

**Why it's real value:**
- Staff currently reads every `guest_notes` to decide priority. This gives them a signal before they open the request.
- Mis-routed requests (guest picks wrong department) are a known pain point in concierge systems. Auto-suggest fixes this without removing human judgment.
- Doesn't change the state machine — still requires human acknowledge/confirm. AI is advisory only.

**Implementation:**
- Celery task on `ServiceRequest` creation (async, doesn't block the guest)
- Results stored in new JSONField `ai_triage` on ServiceRequest: `{"urgency": "URGENT", "suggested_department_id": 5, "suggested_response": "..."}`
- Staff sees this as annotations in the UI, not as automated actions

---

### 3. Admin Content Generation

**Persona:** Admin
**Problem:** Onboarding a new hotel means writing `description`, `highlights` (JSON list of strings), `price_display`, `timing`, `duration`, `capacity` for every Experience, Event, and Department. A hotel with 30 experiences means 30× copy work.

**What it does:**
- New admin endpoint: `POST /api/v1/hotels/{hotel_slug}/admin/ai/generate-content/`
- Accepts: `{ "model_type": "experience", "name": "Sunset Kayak", "category": "ACTIVITY", "keywords": ["river", "2 hours", "₹1500 per person", "beginner friendly"] }`
- Returns a draft: `{ "description": "...", "highlights": ["...", "..."], "price_display": "₹1,500 per person", "timing": "4:00 PM – 6:00 PM", "duration": "2 hours" }`
- Admin reviews, edits, saves. Never auto-publishes.

**Why it's real value:**
- Hotel onboarding is the biggest bottleneck for platform growth. This cuts content creation time by 80%.
- Draft-and-edit flow means quality stays under human control.
- Works for departments, experiences, events, and `HotelInfoSection` content.

**Implementation:**
- Thin endpoint that calls the LLM with hotel context (brand tone from existing descriptions, category conventions)
- No new models needed — returns JSON that maps directly to existing serializer fields
- Rate-limited per hotel (5 generations/minute) to prevent abuse

---

### 4. Demand Pattern Insights

**Persona:** Admin / Platform
**Problem:** `ServiceRequest` data contains rich signals — `guest_notes` on CUSTOM requests, NOT_AVAILABLE rates, peak time patterns — but admins have no way to access these insights beyond the existing analytics (which are purely quantitative).

**What it does:**
- Periodic Celery task (weekly) that:
  1. **Clusters CUSTOM requests** by topic — groups `guest_notes` using embeddings or keyword extraction, then uses an LLM to label each cluster ("Airport transfers", "Late checkout requests", "Laundry service")
  2. **Surfaces unmet demand** — clusters that don't map to any existing department/experience are flagged as "guests are asking for this but you don't offer it"
  3. **Identifies supply gaps** — experiences with high NOT_AVAILABLE / total ratio (e.g., "Sunset Dinner gets 40 requests/week but 60% are declined — consider adding capacity")
  4. **Peak analysis** — cross-references the existing heatmap data with request outcomes to find "Tuesday 6-8pm has 3× the requests but same staff count"

- Results stored in a new `HotelInsight` model and surfaced in the admin dashboard

**Why it's real value:**
- The data already exists in your `ServiceRequest` + `RequestActivity` tables. This is pure extraction, not invention.
- "What should we offer that we don't?" is the single most valuable question for a hotel — and currently unanswerable without reading hundreds of guest_notes manually.
- Quantitative analytics (Phase 1 already has heatmaps, department breakdowns, response times) answer "how much." This answers "what and why."

**Implementation:**
- New model: `HotelInsight(hotel, insight_type, title, body, data, period_start, period_end, created_at)`
- Celery beat task: weekly (configurable per hotel)
- LLM usage is batch/async, not user-facing — cost and latency are manageable
- Admin endpoint: `GET /api/v1/hotels/{hotel_slug}/admin/insights/`

---

### 5. Contextual Guest Recommendations

**Persona:** Guest
**Problem:** The guest sees a flat list of departments and experiences. There's no sense of "what's relevant to me right now" based on time of day, stay duration, or what they've already requested.

**What it does:**
- New guest endpoint: `GET /api/v1/hotels/{hotel_slug}/recommendations/`
- Returns a ranked list of 3-5 experiences/events with a short reason
- Ranking signals:
  - **Time of day** — dinner experiences surface in the evening, spa in the afternoon
  - **Request history** — don't recommend what they've already booked; suggest complementary categories
  - **Booking windows** — prioritize events whose booking window is closing soon ("This event is tomorrow and booking closes in 2 hours")
  - **Popularity** — experiences with high CONFIRMED/total ratio in the last 30 days
  - **Stay context** — first day vs last day (arrival: spa/dining, departure: checkout info/transport)

**Why it's real value:**
- Hotels make money when guests book experiences. A guest who opens the app and sees "here's what's relevant to you right now" is more likely to book than one browsing a catalog.
- All signals are already in your data: `GuestStay.created_at/expires_at`, `ServiceRequest` history, `Event.event_start` + booking windows, `Experience.category`, `Experience.timing`.

**Implementation:**
- Can be done without an LLM — a scoring function with weighted signals is more predictable and faster
- LLM optional: generate the "reason" text ("Popular with guests this week" / "Booking closes in 3 hours")
- New lightweight service function, no new models needed

---

## What I'd Skip

| Feature | Why skip it |
|---|---|
| **General hotel Q&A chatbot** | Your structured department/experience data already answers "what do you offer." A chatbot adds hallucination risk (wrong hours, wrong prices) without solving a problem your existing UI can't solve. |
| **Sentiment analysis on guest_notes** | Notes are too short (most are 1-2 sentences). The staff response loop is already fast (escalation handles urgency). Sentiment scoring adds noise, not signal. |
| **AI-generated images** | Hotels upload real photos. Generated images would undermine trust in a hospitality context. |
| **"Smart" pricing / yield management** | `price_display` is a string, not a transactional price. There's no inventory or payment system. Optimizing a display string is meaningless. |
| **Predictive no-show modeling** | You'd need months of historical data per hotel to train anything useful. Not viable for new hotel onboarding. |
| **AI-powered review responses** | You don't have a reviews system. |

---

## Recommended Implementation Order

| Priority | Feature | Effort | Value | Dependencies |
|---|---|---|---|---|
| **1** | Natural Language Guest Requests | Medium | Very High | LLM API key, published hotel catalog |
| **2** | Admin Content Generation | Low | High | LLM API key |
| **3** | Smart Request Routing + Auto-Triage | Medium | High | None (works with existing data) |
| **4** | Contextual Guest Recommendations | Low-Medium | Medium-High | Enough request history (~2 weeks per hotel) |
| **5** | Demand Pattern Insights | Medium | Medium | Enough CUSTOM requests to cluster (~1 month per hotel) |

Features 1 and 2 can ship immediately (they work on day one for a new hotel). Features 4 and 5 get better with data and are most valuable for hotels that have been live for a while.

---

## Technical Considerations

- **LLM provider:** Use a provider-agnostic abstraction (e.g., litellm or a thin wrapper) so you can swap between Claude, GPT-4, etc. Store the API key in env vars via `python-decouple` (consistent with existing pattern).
- **Cost control:** Guest-facing features (1, 5) should use smaller/faster models. Admin batch features (4) can use larger models since they run async.
- **Latency:** Never block the guest request flow on an LLM call. Feature 1 uses a preview step; Feature 3 runs as a Celery task after request creation.
- **Grounding:** Always pass the hotel's actual catalog as context. Never let the LLM invent departments, experiences, or prices.
- **Privacy:** Never send PII (phone numbers, room numbers) to external LLM APIs. Strip `guest_stay` identifiers before sending `guest_notes` for analysis.
- **Fallbacks:** Every AI feature must have a graceful degradation path. If the LLM is down, Feature 1 falls back to the normal form flow, Feature 3 skips triage, etc.
