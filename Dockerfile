FROM python:3.11-slim

# APP_ENV=prod in the image itself: the Settings validator then refuses to start without
# APP_PASSWORD / SECRET_KEY wherever the container runs (docker run, Railway, Render, Fly),
# not only where fly.toml sets it. `docker run -e APP_ENV=dev ...` opts back into dev.
#
# DATABASE_URL belongs here for the same reason: the app default is the RELATIVE
# sqlite:///./data/app.db (i.e. /app/data/app.db), so without it a plain `docker run` writes
# the bet ledger into the ephemeral container layer while this image prepares an unused
# /data. Mount a volume there — `docker run -v edges-data:/data ...` — or override the URL.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    APP_ENV=prod \
    DATABASE_URL=sqlite:////data/app.db

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

# Installed from the hashed lock so every build gets the exact tested versions
# (regenerate with `make lock` after editing requirements.txt).
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

RUN mkdir -p /data && chown -R app:app /app /data && chmod +x /app/docker/entrypoint.sh

EXPOSE 8080

# The entrypoint fixes ownership of the SQLite directory (root-owned Fly volume), then
# drops to the unprivileged `app` user before starting uvicorn. See docs/DECISIONS.md.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
