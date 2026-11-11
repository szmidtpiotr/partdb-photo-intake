"""Kubełki ilości klasy B (ADR-007) — jedno miejsce na przelicznik.

Rezystorów i kondensatorów nie liczymy co do sztuki, bo udawanie precyzji
tam, gdzie jej nie ma, kończy się porzuceniem bazy. Dyktowanie mówi
„dużo setek nanofaradów", a `PartLot` chce liczby — ten moduł jest
tłumaczem między jednym a drugim.

**Przelicznik musi być wspólny dla całego systemu.** Gdyby każda sesja
agenta wymyślała własną liczbę pod słowo „dużo" (raz 100, raz 500), zrzut
stanu przestałby być porównywalny między pozycjami, a wtedy nie odpowiada
już na pytanie „czy mam czym to zbudować".

Zapis do Part-DB idzie zawsze z `instock_unknown = true` — liczba jest
środkiem przedziału, nie pomiarem.
"""

from __future__ import annotations

# Progi z ADR-007: mało < 10, sporo 10–100, dużo > 100. Liczba przy kubełku
# to środek przedziału, czyli najuczciwsze przybliżenie, jakie mamy.
BUCKETS: dict[str, int] = {
    "brak": 0,
    "mało": 5,
    "sporo": 50,
    "dużo": 200,
}

# Warianty, które padają w dyktowaniu albo w tekście bez polskich znaków.
# Nie próbujemy tu rozumieć mowy — rozbicie zdania na pozycje robi agent
# (ADR-006). To jest tylko odporność na oczywiste zapisy tego samego słowa.
SYNONYMS: dict[str, str] = {
    "malo": "mało",
    "kilka": "mało",
    "garść": "mało",
    "garsc": "mało",
    "duzo": "dużo",
    "pełno": "dużo",
    "pelno": "dużo",
    "mnóstwo": "dużo",
    "mnostwo": "dużo",
    "kilkanaście": "sporo",
    "kilkanascie": "sporo",
    "trochę": "sporo",
    "troche": "sporo",
    "zero": "brak",
    "nie ma": "brak",
}

# Ilość nieoszacowana: wiem, że jest, ale nawet kubełka nie podałem.
# Zdarza się przy segregatorze-próbniku, gdzie liczy się sam fakt
# posiadania wartości, a nie ile jej zostało.
UNCOUNTED = "jest"


class UnknownBucket(ValueError):
    """Słowo nie jest kubełkiem z ADR-007."""


def canonical_bucket(word: str) -> str:
    """Słowo → nazwa kubełka. Nieznane słowo to błąd, nie ciche zero.

    Cicha zamiana „paręnaście" na 0 zapisałaby w magazynie brak części,
    która leży w kuwecie — a to jest dokładnie ta klasa błędu, przez którą
    przestaje się ufać bazie.
    """
    key = (word or "").strip().casefold()
    key = SYNONYMS.get(key, key)
    if key not in BUCKETS:
        raise UnknownBucket(
            f"{word!r} nie jest kubełkiem ilości. Dozwolone: "
            f"{', '.join(BUCKETS)} (ADR-007). Gdy znasz dokładną liczbę, "
            "podaj ją zamiast słowa."
        )
    return key


def parse_bucket(word: str) -> tuple[float, bool]:
    """Słowo → (orientacyjna liczba sztuk, czy ilość jest niepewna).

    `brak` jest jedynym kubełkiem, przy którym ilość NIE jest niepewna:
    „nie ma" to wiedza dokładna, a nie oszacowanie. Reszta idzie
    z `instock_unknown = true`.
    """
    key = canonical_bucket(word)
    return float(BUCKETS[key]), key != "brak"


def bucket_of(amount: float) -> str:
    """Liczba → słowo kubełka, do wyświetlania stanu klasy B."""
    if amount <= 0:
        return UNCOUNTED
    if amount < 10:
        return "mało"
    if amount <= 100:
        return "sporo"
    return "dużo"
