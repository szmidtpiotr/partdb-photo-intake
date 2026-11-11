# Obraz usługi bom-agent. Part-DB zostaje obrazem upstream (ADR-002) —
# ten plik dotyczy wyłącznie naszego kodu.

FROM python:3.13-slim AS base

# uv z oficjalnego obrazu: instalacja z `uv.lock` jest odtwarzalna,
# a nie „to, co akurat było na PyPI w dniu budowania".
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Zależności osobną warstwą — zmiana kodu nie unieważnia instalacji pakietów.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bom_agent ./bom_agent
# PWA jedzie w obrazie jako statyk — bez kroku budowania, więc to, co leży
# w repo, jest dokładnie tym, co dostaje telefon.
COPY pwa ./pwa
RUN uv sync --frozen --no-dev

# Usługa nie potrzebuje roota. `/data` to jedyne miejsce, w którym pisze:
# kolejka intake (SQLite) i zdjęcia z telefonu.
#
# `/app` zostaje własnością roota — aplikacja tylko z niego czyta, a rekurencyjny
# `chown` na środowisku wirtualnym mieliłby kilkanaście tysięcy plików. Zmierzone:
# na tym hoście trwał ponad dziesięć minut i był najdroższą warstwą obrazu.
RUN useradd --system --uid 1000 --create-home bom \
    && mkdir -p /data/intake/photos \
    && chown -R bom:bom /data
USER bom

ENV BOM_DB_PATH=/data/bom.db \
    INTAKE_PHOTO_DIR=/data/intake/photos \
    BOM_HTTP_PORT=8091

EXPOSE 8091

# Healthcheck pyta własne /health, które samo raportuje stan Part-DB.
# Kontener jest zdrowy także wtedy, gdy Part-DB leży — inaczej awaria
# zależności restartowałaby usługę, która działa poprawnie.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8091/health', timeout=4)"

CMD ["python", "-m", "bom_agent"]
