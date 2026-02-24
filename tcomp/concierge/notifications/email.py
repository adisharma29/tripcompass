# Email adapter — stub. Full implementation in Phase 3 (post-WhatsApp).
#
# Will contain:
# - EmailAdapter(ChannelAdapter): async dispatch via Celery + Resend API
# - Route-based targeting (same dedupe pattern as WhatsApp)
# - Idempotency via DeliveryRecord
