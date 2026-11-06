# Syntax: docker/dockerfile:1.4

# ============================================================
# Multi-stage build for Telegram Bot Hosting Platform
# ============================================================
# Stage 1: Builder - Install dependencies and compile if needed
# ============================================================
FROM python:3.11-slim AS builder

# Install system dependencies needed for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies to a local directory
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============================================================
# Stage 2: Runtime - Minimal image with only runtime deps
# ============================================================
FROM python:3.11-slim AS runtime

# Install runtime system dependencies
# tini: يحصد العمليات الزومبي (البوتات المستضافة عمليات فرعية). أدوات البناء
# اختيارية (BOT_BUILD_TOOLS=0 لصورة أصغر) لأن بعض مكتبات البوتات تُترجم عند التثبيت.
ARG BOT_BUILD_TOOLS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gosu \
    tini \
    git \
    && if [ "$BOT_BUILD_TOOLS" = "1" ]; then \
         apt-get install -y --no-install-recommends build-essential libffi-dev libssl-dev; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/bash appuser

# Create app directory with proper permissions
WORKDIR /app
RUN chown -R appuser:appuser /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appuser . .

# Create directories for persistent data (will be mounted as volumes)
RUN mkdir -p /app/uploaded_bots /app/data /app/logs && \
    chown -R appuser:appuser /app/uploaded_bots /app/data /app/logs

# Initialize attached volumes as root, then drop privileges in entrypoint
COPY --chown=root:root infra/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
USER root

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/app/data/hosting.db \
    BOTS_DIR=/app/data/uploaded_bots \
    BACKUP_DIR=/app/data/backups \
    PIP_CACHE_DIR=/app/data/pip-cache

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Expose port (for health check server)
EXPOSE 8080

# Run as module
CMD ["python", "main.py"]