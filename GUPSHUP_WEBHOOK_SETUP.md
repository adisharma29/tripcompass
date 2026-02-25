# Gupshup Webhook Setup

## Dashboard Config

1. Go to **Gupshup Dashboard** > App **refuje** > Settings > Webhooks
2. Set **Callback URL**: `https://api.vyntel.com/api/v1/webhooks/gupshup-wa/`
3. Check **Include Headers** and add:
   - Key: `X-Webhook-Secret`
   - Value: `ULMyDFnxd66y7ESc_GQoJrquza7giVm1eJcg-MvScDo`
4. Tick **Message Events**: Message, Failed, Delivered, Read
5. Leave everything else unchecked

## Env Vars

Already added to `.env` and `docker-compose.yml`:

```
GUPSHUP_WA_WEBHOOK_SECRET=ULMyDFnxd66y7ESc_GQoJrquza7giVm1eJcg-MvScDo
```

Restart containers after deploy: `docker compose up -d web celery-worker`
