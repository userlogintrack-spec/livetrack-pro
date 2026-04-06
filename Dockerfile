FROM python:3.10-slim

# Create non-root user for security
RUN useradd -m -u 1000 livetrack

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Collect static files (needs SECRET_KEY set)
RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput 2>/dev/null || true

# Create required directories
RUN mkdir -p /app/logs /app/media && chown -R livetrack:livetrack /app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/accounts/login/')" || exit 1

# Run as non-root user
USER livetrack

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "tracker.asgi:application"]
