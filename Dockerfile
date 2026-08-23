FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installs Chromium plus every OS-level library it needs (fonts, libnss3,
# etc.), a plain Python buildpack won't have these, and the payment
# transfer-details step needs a real headless browser to work at all.
RUN playwright install --with-deps chromium

COPY . .

# Railway assigns $PORT at runtime, never assume 8000. Migrations run on
# every startup, not just the first deploy, alembic upgrade head is
# idempotent (a no-op if there's nothing new to apply), so this is the
# simplest way to guarantee the schema is always current without a
# separate manual step.
CMD alembic upgrade head && uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}