import pytest
from fastapi.testclient import TestClient

from bom_agent import __version__
from bom_agent.config import load_settings
from bom_agent.db import Database
from bom_agent.intake.queue import IntakeQueue
from bom_agent.partdb_client import PartDbStatus
from bom_agent.web import create_app


class StubClient:
    """Podstawka pod `PartDbClient` — /health nie potrzebuje więcej."""

    def __init__(self, status: PartDbStatus):
        self._status = status
        self.closed = False

    async def ping(self) -> PartDbStatus:
        return self._status

    async def aclose(self) -> None:  # pragma: no cover — nie nasza instancja
        self.closed = True


@pytest.fixture
def app_with(tmp_path):
    """Kolejka na katalogu tymczasowym — inaczej testy migrowałyby bazę
    pod produkcyjną ścieżką `/data`, do której i tak nie ma dostępu."""

    def build(status: PartDbStatus, env: dict | None = None):
        settings = load_settings(env or {"PARTDB_API_TOKEN": "tcp_test"})
        db = Database(tmp_path / "bom.db")
        db.migrate()
        return create_app(
            settings,
            client=StubClient(status),
            queue=IntakeQueue(db, tmp_path / "photos"),
        )

    return build


def test_health_ok(app_with):
    status = PartDbStatus(
        reachable=True, authorized=True, latency_ms=12.3, total_parts=25
    )
    with TestClient(app_with(status)) as tc:
        response = tc.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "bom-agent"
    assert body["version"] == __version__
    assert body["partdb"]["reachable"] is True
    assert body["partdb"]["total_parts"] == 25
    assert body["partdb"]["error"] is None


def test_health_is_200_even_when_partdb_is_down(app_with):
    status = PartDbStatus(
        reachable=False, authorized=False, error="Part-DB nieosiągalny"
    )
    with TestClient(app_with(status)) as tc:
        response = tc.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["partdb"]["error"] == "Part-DB nieosiągalny"


def test_health_does_not_leak_token(app_with):
    status = PartDbStatus(reachable=True, authorized=True)
    env = {"PARTDB_API_TOKEN": "tcp_sekret", "PARTDB_URL": "http://partdb.test"}
    with TestClient(app_with(status, env)) as tc:
        response = tc.get("/health")

    assert "tcp_sekret" not in response.text
    assert response.json()["partdb"]["url"] == "http://partdb.test"
