"""Porównywanie nazw części — jedno miejsce dla całego systemu.

Reguły są tu, a nie w module intake'u, bo obowiązują wszędzie, gdzie pada
pytanie „czy to ta sama rzecz": przy zatwierdzaniu rozpoznania, przy
przypisywaniu pojemnika, a w przyszłości przy `check_bom`. Dwie kopie tych
reguł oznaczałyby, że zapis uznaje dwie pozycje za tę samą część, a inne
narzędzie za dwie różne — i nikt by nie wiedział, która ma rację.

Obie decyzje poniżej pochodzą z realnych danych, nie z teorii.
"""

from __future__ import annotations

import re


def strip_vendor_tags(text: str) -> str:
    """Usuń dopiski w nawiasach **bez cyfr** — `[LCSC]`, `(Espressif)`.

    Tylko takie: `(N16R8)` czy `(klon CH340)` niosą oznaczenie wersji, więc
    muszą zostać. Kasowanie wszystkich nawiasów gubiło różnicę między
    `ESP32-S3 (N16R8)` a `ESP32-S3 (N8R2)` — czyli między 16 a 8 MB flasha.
    """
    return re.sub(r"[\[(]\s*[^\])\d]*?\s*[\])]", " ", (text or "").lower())


def normalize(text: str) -> str:
    """Nazwa do porównań: bez znaczników dostawcy, znaków i wielkości liter.

    `"ESP32-WROOM-32 DevKit v1"` i `"esp32 wroom 32 devkit v1"` to ta sama
    część. `"BME280 [LCSC]"` też — nawias z nazwą dostawcy dokleja provider
    przy zaciąganiu danych i nie jest częścią oznaczenia.

    **Separator dziesiętny między cyframi zostaje.** Bez tego `1.2pF`
    i `12pF` sprowadzają się do tego samego ciągu — a to dwie różne części,
    różniące się dziesięciokrotnie pojemnością. Zmierzone na próbniku:
    zapis dołożył stan 12 pF do pozycji 1,2 pF, bo kropka wypadała razem
    z resztą interpunkcji. Przecinek sprowadzamy do kropki, żeby `1,2` i
    `1.2` były równoważne.
    """
    text = strip_vendor_tags(text).replace(",", ".")
    # Kropka poza liczbą (skróty, koniec zdania) nie niesie znaczenia.
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    return re.sub(r"[^a-z0-9.]", "", text)


def tokens(text: str) -> set[str]:
    """Rozpoznawalne człony nazwy — te, po których człowiek szuka części.

    Człon liczy się, gdy ma **co najmniej cztery znaki oraz jednocześnie
    literę i cyfrę**: `esp32`, `hcsr04`, `max7219`, `lp753636`, `1n4148`.

    Wymóg litery **obok** cyfry jest tu kluczowy i wynika z realnych danych.
    Wcześniej wystarczała sama cyfra — i wtedy `1206` (kod obudowy) stawał
    się „rozpoznawalnym członem" wspólnym dla wszystkich kondensatorów SMD.
    Efekt: przy wpisywaniu próbnika **każda** wartość wyglądała na podobną
    do każdej innej i zapis stawał w miejscu, czekając na rozstrzygnięcie
    osiemdziesięciu nieistniejących wątpliwości.

    Kody obudów (`1206`, `0805`), lata i same liczby są więc odrzucane —
    nie identyfikują części, tylko jej opakowanie.
    """
    raw = re.split(r"[^a-z0-9]+", strip_vendor_tags(text))
    return {
        token
        for token in raw
        if len(token) >= 4
        and any(char.isdigit() for char in token)
        and any(char.isalpha() for char in token)
    }
