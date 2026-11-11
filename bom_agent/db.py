"""SQLite — wyłącznie kolejka intake i log operacji (ADR-004).

Nie ma tu żadnych danych o częściach ani stanach. To jest jedyna rzecz,
której Part-DB nie modeluje: zdjęcia i dyktowania czekające na rozpoznanie
przez agenta.

Migracje idą po `PRAGMA user_version`. Przy jednej tabeli i jednym
użytkowniku Alembic byłby maszynerią cięższą od problemu — ale numer wersji
musi być, bo inaczej pierwsza zmiana schematu na żywej bazie oznacza
zgadywanie, co już jest zastosowane.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Każda migracja to (wersja, lista instrukcji SQL). Wersje rosną o 1
# i nigdy nie zmieniamy tych już wydanych — dopisujemy następną.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE intake_item (
                id            INTEGER PRIMARY KEY,
                kind          TEXT NOT NULL CHECK (kind IN ('photo','voice','manual')),
                payload_path  TEXT,
                payload_text  TEXT,
                location_hint TEXT,
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','processing',
                                                'committed','rejected')),
                result_json   TEXT,
                partdb_refs   TEXT,
                note          TEXT,
                created_at    TEXT NOT NULL,
                processed_at  TEXT
            )
            """,
            "CREATE INDEX idx_intake_status ON intake_item(status, created_at)",
            """
            CREATE TABLE op_log (
                id          INTEGER PRIMARY KEY,
                partdb_iri  TEXT NOT NULL,
                operation   TEXT NOT NULL,
                before_json TEXT,
                after_json  TEXT,
                reason      TEXT NOT NULL,
                session     TEXT,
                created_at  TEXT NOT NULL,
                reverted    INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_oplog_time ON op_log(created_at)",
        ],
    ),
    (
        2,
        [
            # Kilka ujęć jednej rzeczy (#66). Moduł fotografuje się z góry,
            # od spodu i z bliska na sitodruk — to jeden element, nie trzy.
            # Ujęcie dodatkowe jest wierszem wskazującym na pozycję główną;
            # osobno nigdy nie trafia do kolejki do rozpoznania.
            "ALTER TABLE intake_item ADD COLUMN parent_id INTEGER"
            " REFERENCES intake_item(id)",
            "CREATE INDEX idx_intake_parent ON intake_item(parent_id)",
        ],
    ),
    (
        3,
        [
            # Etap propozycji (ADR-021, #73). Rozpoznanie zapisuje TU, a nie
            # w Part-DB — dopiero zatwierdzenie przez człowieka rusza magazyn.
            # Dzięki temu rozpoznawanie bez nadzoru (#75) nie może niczego
            # zepsuć: najgorszy skutek to wiersz do odrzucenia.
            """
            CREATE TABLE intake_proposal (
                id           INTEGER PRIMARY KEY,
                item_id      INTEGER NOT NULL REFERENCES intake_item(id),
                payload_json TEXT NOT NULL,
                preview_json TEXT,
                status       TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','approved','rejected')),
                partdb_refs  TEXT,
                note         TEXT,
                created_at   TEXT NOT NULL,
                decided_at   TEXT
            )
            """,
            "CREATE INDEX idx_proposal_item ON intake_proposal(item_id, status)",
            # „Rozpoznaj to" wciśnięte na telefonie. Automat bierze WYŁĄCZNIE
            # oznaczone pozycje: zdjęcie nieoznaczone bywa niekompletne, bo
            # użytkownik zamierza dołożyć ujęcie spodu (#66).
            "ALTER TABLE intake_item ADD COLUMN recognize_requested_at TEXT",
        ],
    ),
    (
        4,
        [
            # Zmierzone na żywej instancji: dwa dotknięcia ✓ w telefonie
            # (albo jedno z podwójnym zdarzeniem) wysłały dwa żądania
            # zatwierdzenia. Oba przeszły sprawdzenie „czy jeszcze pending",
            # oba wykonały zapis, i część dostała DWA stany po 1 szt.
            # Znacznik zabrania propozycji ruszyć drugi raz: zabiera się ją
            # jednym UPDATE ... WHERE claimed_at IS NULL, więc drugi
            # równoległy zapis nie ma czego zabrać.
            "ALTER TABLE intake_proposal ADD COLUMN claimed_at TEXT",
        ],
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


class Database:
    """Cienka warstwa nad `sqlite3`.

    Połączenie na operację, nie pula. Przy jednym użytkowniku i zapisach
    rzędu kilkudziesięciu na sesję koszt otwarcia pliku jest nieistotny,
    a znika cała klasa błędów z współdzieleniem połączenia między wątkami.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL: czytanie nie blokuje pisania. Przy intake'u telefon dopisuje
        # w tle, gdy agent czyta kolejkę — bez tego jedno czekałoby na drugie.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def migrate(self) -> int:
        """Doprowadź schemat do najnowszej wersji. Zwróć wersję po migracji."""
        with self.connect() as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    conn.execute(statement)
                # PRAGMA nie przyjmuje parametrów — stąd f-string. Wartość
                # pochodzi z listy w tym pliku, nie z zewnątrz.
                conn.execute(f"PRAGMA user_version={version}")
                current = version
            return current

    def version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    # --- operacje ----------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Zapis. Zwraca `lastrowid` (przy INSERT) albo liczbę zmian."""
        with self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return cursor.lastrowid or cursor.rowcount

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- warianty asynchroniczne -------------------------------------------
    #
    # FastAPI obsługuje żądania w pętli zdarzeń; synchroniczne sqlite3
    # blokowałoby ją na czas zapisu. Przy pliku lokalnym to ułamki
    # milisekund, ale upload zdjęcia idzie w tej samej pętli i nie ma
    # powodu, żeby na siebie czekały.

    async def amigrate(self) -> int:
        return await asyncio.to_thread(self.migrate)

    async def aexecute(self, sql: str, params: Iterable[Any] = ()) -> int:
        return await asyncio.to_thread(self.execute, sql, params)

    async def aquery(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.query, sql, params)

    async def aquery_one(
        self, sql: str, params: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.query_one, sql, params)
