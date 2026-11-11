"""Konfiguracja z zmiennych środowiskowych.

Wzorzec pliku: `.env.example` w katalogu głównym repo.

Świadomie bez `pydantic-settings` — konfiguracja jest płaska, a jedna
zależność mniej to jeden powód mniej, żeby obraz się rozjechał.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PARTDB_URL = "http://localhost:8090"

# Adres, pod którym `bom-agent` jest widoczny z zewnątrz. Part-DB zapisuje go
# w załączniku ze zdjęciem intake'u (#64) i podaje przeglądarce, więc musi to
# być adres publiczny, a nie `localhost` czy nazwa usługi dockerowej.
DEFAULT_PUBLIC_URL = "http://localhost:8091"

# Publiczny adres Part-DB — ten, pod którym instancja jest widoczna z telefonu
# (np. `https://partdb.example.com`). Różny od `PARTDB_URL`, który jest
# adresem wewnętrznym usługi w sieci dockerowej. PWA buduje z niego link do
# rekordu części w archiwum intake'u (#69), więc musi wyjść na zewnątrz.
DEFAULT_PARTDB_PUBLIC_URL = "http://localhost:8090"

LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class ConfigError(RuntimeError):
    """Konfiguracja jest niekompletna albo bez sensu. Usługa nie wstanie."""


@dataclass(frozen=True)
class Settings:
    """Ustawienia usługi. Niemutowalne — czytane raz, na starcie procesu."""

    partdb_url: str = DEFAULT_PARTDB_URL
    partdb_api_token: str = field(default="", repr=False)
    partdb_timeout: float = 10.0
    public_base_url: str = DEFAULT_PUBLIC_URL
    partdb_public_url: str = DEFAULT_PARTDB_PUBLIC_URL
    bom_db_path: Path = Path("/data/bom.db")
    intake_photo_dir: Path = Path("/data/intake/photos")
    log_level: str = "INFO"
    mcp_http_token: str = field(default="", repr=False)
    # Twarda blokada dla sesji bez nadzoru (#75): `intake_pending` oddaje
    # wtedy WYŁĄCZNIE pozycje zlecone przyciskiem w PWA, niezależnie od
    # tego, o co poprosi model. Bez tego „tylko zlecone" jest zdaniem
    # w promptcie, a zdanie w promptcie nie jest gwarancją.
    intake_only_requested: bool = False

    @property
    def has_partdb_token(self) -> bool:
        return bool(self.partdb_api_token)

    def intake_photo_url(self, item_id: int) -> str:
        """Publiczny adres zdjęcia z kolejki — ten sam, który dostaje PWA."""
        return f"{self.public_base_url}/api/intake/{item_id}/photo"

    def partdb_part_url(self, part_id: int) -> str:
        """Publiczny adres rekordu części w Part-DB — do klikania z telefonu.

        Trasa `/en/part/{id}` to widok części w interfejsie Part-DB;
        archiwum intake'u (#69) linkuje tu z zatwierdzonej pozycji.
        """
        return f"{self.partdb_public_url}/en/part/{part_id}"


def _get(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key)
    return default if value is None or value.strip() == "" else value.strip()


def _get_bool(env: dict[str, str], key: str, default: bool = False) -> bool:
    """Prawda to `1`, `true`, `yes`, `on` — cokolwiek innego jest fałszem.

    Świadomie bez błędu przy nieznanej wartości: to przełącznik zaciskający
    uprawnienia, więc literówka ma zostawić go w pozycji „nie zaciskaj",
    a nie wywrócić usługę. Zaciśnięcie widać w logu przy starcie.
    """
    raw = _get(env, key, "1" if default else "0").strip().casefold()
    return raw in {"1", "true", "yes", "on", "tak"}


def _get_float(env: dict[str, str], key: str, default: float) -> float:
    raw = _get(env, key, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} nie jest liczbą") from exc
    if value <= 0:
        raise ConfigError(f"{key}={raw!r} musi być dodatnie")
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Zbuduj `Settings` ze środowiska.

    Brak tokenu NIE jest błędem konfiguracji: usługa ma wstać i przez
    `/health` powiedzieć, czego jej brakuje. Usługa, która nie startuje,
    nie ma jak tego zaraportować.
    """
    env = dict(os.environ if env is None else env)

    partdb_url = _get(env, "PARTDB_URL", DEFAULT_PARTDB_URL).rstrip("/")
    if not partdb_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"PARTDB_URL={partdb_url!r} musi zaczynać się od http:// albo https://"
        )
    if partdb_url.endswith("/api"):
        raise ConfigError(
            "PARTDB_URL wskazuje na adres instancji, nie na /api — "
            f"zamiast {partdb_url!r} podaj {partdb_url[: -len('/api')]!r}"
        )

    public_base_url = _get(env, "PUBLIC_BASE_URL", DEFAULT_PUBLIC_URL).rstrip("/")
    if not public_base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"PUBLIC_BASE_URL={public_base_url!r} musi zaczynać się "
            "od http:// albo https://"
        )

    partdb_public_url = _get(
        env, "PARTDB_PUBLIC_URL", DEFAULT_PARTDB_PUBLIC_URL
    ).rstrip("/")
    if not partdb_public_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"PARTDB_PUBLIC_URL={partdb_public_url!r} musi zaczynać się "
            "od http:// albo https://"
        )

    log_level = _get(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL={log_level!r} spoza {sorted(LOG_LEVELS)}")

    return Settings(
        partdb_url=partdb_url,
        partdb_api_token=_get(env, "PARTDB_API_TOKEN", ""),
        partdb_timeout=_get_float(env, "PARTDB_TIMEOUT", 10.0),
        public_base_url=public_base_url,
        partdb_public_url=partdb_public_url,
        bom_db_path=Path(_get(env, "BOM_DB_PATH", "/data/bom.db")),
        intake_photo_dir=Path(_get(env, "INTAKE_PHOTO_DIR", "/data/intake/photos")),
        log_level=log_level,
        mcp_http_token=_get(env, "MCP_HTTP_TOKEN", ""),
        intake_only_requested=_get_bool(env, "INTAKE_ONLY_REQUESTED"),
    )
