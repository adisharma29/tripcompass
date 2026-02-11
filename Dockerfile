FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install only runtime GDAL/GEOS/PROJ libs (not -dev packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    gdal-bin \
    libgdal36 \
    libgeos-c1t64 \
    libproj25 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Auto-detect library paths for both amd64 and arm64
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then LIB_DIR="aarch64-linux-gnu"; \
    else LIB_DIR="x86_64-linux-gnu"; fi && \
    ln -sf /usr/lib/${LIB_DIR}/libgdal.so /usr/lib/libgdal.so && \
    ln -sf /usr/lib/${LIB_DIR}/libgeos_c.so /usr/lib/libgeos_c.so

ENV GDAL_LIBRARY_PATH=/usr/lib/libgdal.so \
    GEOS_LIBRARY_PATH=/usr/lib/libgeos_c.so

WORKDIR /app

COPY requirements/base.txt /tmp/requirements/base.txt

RUN pip install --upgrade pip --root-user-action=ignore \
    && pip install --no-cache-dir -r /tmp/requirements/base.txt --root-user-action=ignore

# Development stage
FROM base AS development

COPY requirements/dev.txt /tmp/requirements/dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements/dev.txt --root-user-action=ignore

COPY ./tcomp /app

RUN useradd -m -u 1000 django && chown -R django:django /app
USER django

CMD ["uvicorn", "tcomp.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# Production stage
FROM base AS production

COPY ./tcomp /app

RUN useradd -m -u 1000 django && chown -R django:django /app

USER django

CMD ["uvicorn", "tcomp.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
