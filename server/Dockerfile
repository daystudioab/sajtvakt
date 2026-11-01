FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SAJTVAKT_DATA=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY requirements.txt .
# Webbläsaren (Chromium) används för skärmdumparna av startsidorna.
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sajtvakt \
 && mkdir -p /data && chown sajtvakt:sajtvakt /data
COPY sajtvakt_server ./sajtvakt_server
# Kör inte som root: även om något skulle gå fel kan processen bara röra sina egna filer.
USER sajtvakt
VOLUME /data
EXPOSE 8000
# En enda process: schemaläggaren och jobbköerna ska bara finnas en gång.
CMD ["uvicorn", "sajtvakt_server.web:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
