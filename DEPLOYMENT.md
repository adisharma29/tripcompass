# Production Deployment

## Prerequisites

- Docker and Docker Compose installed
- `.env.prod` configured (see `.env.prod.example`)

## Deploy

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

## Static Files

Static files are served from Cloudflare R2 and are **not** collected on every container restart.

After deploying new code with updated static files, run:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec web python manage.py collectstatic --noinput
```

## Useful Commands

**Run migrations manually:**

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec web python manage.py migrate --noinput
```

**Create a superuser:**

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -it web python manage.py createsuperuser
```

**Open a Django shell:**

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -it web python manage.py shell
```

**View logs:**

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f web
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f traefik
```

**Recreate a single service:**

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --force-recreate <service>
```
