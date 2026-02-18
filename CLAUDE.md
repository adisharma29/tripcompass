# Project: Field Guide — Hotel Concierge Platform

## Overview
Multi-tenant hotel concierge SaaS built on top of the existing travel guide platform (tcomp).
Backend: Django 5.2 + DRF + PostGIS + Celery + Redis. Frontend: Next.js (separate repo, not in scope yet).

## Domains
- Production: `refuje.com` (frontend), `api.refuje.com` (API)
- Development: `localhost:6001` (frontend), `localhost:8000` (API)

## Implementation Tracking
- **Always check `PROGRESS.md`** at the project root before starting work — it tracks what's done and what's next.
- Implementation follows 6 phases defined in `v1_implementation_plan_FIXED_v3_patched.md`.
- Work is backend-only for now. Frontend will come later.

## Branch
- Feature branch: `feat/concierge-v1`
- Do not push to main without review.

## Key Conventions
- Django apps live under `tcomp/` (the inner project dir is `tcomp/tcomp/`)
- Settings are split: `base.py`, `dev.py`, `prod.py`
- All new concierge models go in `tcomp/concierge/`
- Auth views live in `tcomp/users/` — concierge app must NOT register any `/auth/*` routes
- Cookie-based JWT auth (httpOnly) — no Bearer tokens in production
- Env vars via `python-decouple` (`config()`)
- Existing travel guide apps (`guides`, `location`) are untouched

## Commands
- Run server: `python manage.py runserver` or via docker-compose
- Migrations: `python manage.py makemigrations && python manage.py migrate`
- Tests: `python manage.py test`

## Do Not
- Commit with attribution to anyone
- Expose `confirmation_token` field in any serializer (reserved for Phase 2)
- Add Bearer auth to base or prod settings (cookie-only; Bearer is dev-only for testing)
