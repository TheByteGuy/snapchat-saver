# syntax=docker/dockerfile:1
#
# snapchat-saver: one container with Xvfb + openbox + x11vnc (localhost only)
# + the FastAPI dashboard, all supervised by supervisord under tini.
#
# The base image already contains the Playwright browsers (in /ms-playwright)
# for exactly this Playwright version, so the Python package below is pinned
# to the same version and no `playwright install` is needed.
ARG PLAYWRIGHT_VERSION=1.65.1
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

ARG PLAYWRIGHT_VERSION
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="snapchat-saver" \
      org.opencontainers.image.description="Sends a daily fun-fact Snap through Snapchat Web to keep streaks alive" \
      org.opencontainers.image.source="https://github.com/TheByteGuy/snapchat-saver"

# Display stack, VNC, noVNC static files, process supervision, fonts.
# novnc is only used for its static files (/usr/share/novnc); the dashboard
# bridges the WebSocket to x11vnc itself, so no websockify process runs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        openbox \
        supervisor \
        tini \
        tzdata \
        fonts-liberation \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/* \
 && test -f /usr/share/novnc/vnc.html

WORKDIR /opt/snapchat-saver

# Python dependencies (test-only packages are skipped in the image).
# The extra "playwright==<version>" requirement is combined with the
# "playwright>=1.47" line from requirements.txt, forcing the version whose
# Chromium build ships in the base image.
COPY requirements.txt ./
RUN grep -viE '^[[:space:]]*pytest' requirements.txt > /tmp/requirements-runtime.txt \
 && pip install --no-cache-dir -r /tmp/requirements-runtime.txt "playwright==${PLAYWRIGHT_VERSION}" \
 && rm /tmp/requirements-runtime.txt \
 && python -c "import playwright, importlib.metadata as m; v = m.version('playwright'); assert v == '${PLAYWRIGHT_VERSION}', v"

# Process supervision and start-up scripts.
COPY docker/supervisord.conf /etc/snapchat-saver/supervisord.conf
COPY docker/openbox-rc.xml /etc/snapchat-saver/openbox-rc.xml
COPY docker/entrypoint.sh docker/wait-for-x.sh docker/fatal-listener.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/wait-for-x.sh /usr/local/bin/fatal-listener.py

# Application code (tests are not shipped).
COPY app/ ./app/

ENV DATA_DIR=/data \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOVNC_DIR=/usr/share/novnc \
    PUID=568 \
    PGID=568 \
    TZ=UTC

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS --max-time 8 http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/snapchat-saver/supervisord.conf"]
