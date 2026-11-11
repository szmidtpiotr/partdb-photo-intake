"""Przygotowanie zdjęć do przekazania agentowi przez MCP.

Oryginał z telefonu zostaje na dysku nietknięty. Agent dostaje kopię
zmniejszoną, bo pełna rozdzielczość zjadłaby kontekst bez żadnego zysku
dla rozpoznania.

Zmierzone na zdjęciu z Pixela 10 Pro XL (4080×3072):

| dłuższy bok | plik | tokeny |
|---|---|---|
| oryginał | 1410 kB | ~16 700 |
| 2048 | 220 kB | 4 210 |
| **1568** | **150 kB** | **2 469** |
| 1024 | 83 kB | 1 052 |

1568 px to kompromis wybrany świadomie: niżej zaczyna ginąć drobny
sitodruk (`HC-SR04`, `AMS1117-3.3`), a to jedyna informacja, po której
rozpoznaje się moduł.

## Wycinki (`CropBox`)

Jedno zdjęcie często pokazuje **kilka różnych płytek naraz** — tak się
fotografuje, gdy wysypie się na blat woreczek z zamówienia. Każda z nich
jest osobną częścią w magazynie, więc każda ma dostać swój własny obraz;
podpięcie do pięciu części tego samego kadru z pięcioma modułami znaczy
tyle samo, co nie podpięcie niczego.

Wycinek opisujemy **ułamkami krawędzi** (0–1), nie pikselami, bo agent
widzi kadr zmniejszony do 1568 px i nie zna wymiaru oryginału. Ułamek jest
ten sam w obu rozdzielczościach. Liczymy go **po obrocie z EXIF**, czyli
w tej orientacji, w której agent zdjęcie widział — inaczej `x` i `y`
zamieniłyby się rolami.

Wycinków **nie zapisujemy jako plików**. Oryginał leży na dysku i jest
w kopii zapasowej; wycinek to cztery liczby w adresie
(`…/photo?crop=x,y,w,h`), z którego serwer odtwarza obraz na żądanie. Zła
ramka poprawia się wtedy podmianą adresu, a nie sprzątaniem osieroconych
plików.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

MAX_EDGE = 1568
QUALITY = 80

# Przybliżenie kosztu obrazu w tokenach: szerokość × wysokość / 750.
TOKENS_PER_PIXEL_BLOCK = 750

# Najmniejszy sensowny wycinek — 2% krawędzi. Mniejszy to prawie zawsze
# pomyłka w jednostkach, a nie płytka wielkości ziarna piasku.
CROP_MIN_EDGE = 0.02

# Ramka rysowana „na oko" lubi wyjść za kadr o włos. Taki nadmiar przycinamy
# do krawędzi; większy jest błędem, bo znaczy, że współrzędne policzono
# w innym układzie.
CROP_TOLERANCE = 0.02


class CropError(ValueError):
    """Wycinek jest niepoprawny — złe współrzędne albo zła jednostka."""


@dataclass(frozen=True)
class CropBox:
    """Prostokąt w ułamkach krawędzi kadru: lewy górny róg + szerokość i wysokość."""

    x: float
    y: float
    width: float
    height: float

    @classmethod
    def parse(cls, raw: Any) -> CropBox:
        """Zbuduj wycinek z tego, co przyszło od agenta albo z adresu.

        Przyjmujemy trzy zapisy tej samej rzeczy, bo wszystkie trzy padają
        naturalnie: `"0.1,0.2,0.3,0.4"` (adres), `[0.1, 0.2, 0.3, 0.4]`
        (lista w wywołaniu narzędzia) i `{"x": …, "y": …, "width": …,
        "height": …}` (słownik).

        **Procenty rozpoznajemy sami.** Model równie chętnie napisze
        `[10, 20, 30, 40]`, myśląc „procent kadru", jak `[0.1, 0.2, 0.3,
        0.4]`. Ułamek nie ma prawa mieć składowej większej od 1, więc te
        dwa zapisy są rozróżnialne bez pytania — a odrzucenie procentów
        z komunikatem o jednostkach kosztowałoby jedno wywołanie więcej
        i nic by nie wnosiło.
        """
        values = cls._four_numbers(raw)
        # Jednostka: cokolwiek powyżej 1 znaczy, że to procenty — ułamek
        # kadru nie może wystawać poza kadr o całą jego szerokość.
        if max(values) > 1:
            if max(values) > 100 + CROP_TOLERANCE * 100:
                raise CropError(
                    f"Wycinek {values} wychodzi poza kadr — podaj ułamki "
                    "krawędzi (0–1) albo procenty (0–100)"
                )
            values = [value / 100 for value in values]

        x, y, width, height = values
        if width < CROP_MIN_EDGE or height < CROP_MIN_EDGE:
            raise CropError(
                f"Wycinek {width:.3g}×{height:.3g} kadru jest mniejszy niż "
                f"{CROP_MIN_EDGE:.0%} krawędzi — sprawdź, czy to nie pomyłka "
                "w jednostkach (ułamki 0–1 albo procenty 0–100)"
            )
        if x < -CROP_TOLERANCE or y < -CROP_TOLERANCE:
            raise CropError(f"Wycinek zaczyna się poza kadrem: x={x:.3g}, y={y:.3g}")
        if x + width > 1 + CROP_TOLERANCE or y + height > 1 + CROP_TOLERANCE:
            raise CropError(
                f"Wycinek wychodzi za krawędź kadru: x+w={x + width:.3g}, "
                f"y+h={y + height:.3g} (dozwolone do 1)"
            )

        # Dopiero teraz przycinamy do kadru. Ramka rysowana na oko wystaje
        # o włos i to jest w porządku; sprawdzenie powyżej odsiało te, które
        # wystają na tyle, że opisują coś innego niż widać.
        x, y = max(0.0, x), max(0.0, y)
        return cls(x=x, y=y, width=min(width, 1.0 - x), height=min(height, 1.0 - y))

    @staticmethod
    def _four_numbers(raw: Any) -> list[float]:
        if isinstance(raw, CropBox):
            return [raw.x, raw.y, raw.width, raw.height]
        if isinstance(raw, str):
            parts = [bit for bit in raw.replace(";", ",").split(",") if bit.strip()]
        elif isinstance(raw, dict):
            try:
                parts = [
                    raw["x"],
                    raw["y"],
                    raw.get("width", raw.get("w")),
                    raw.get("height", raw.get("h")),
                ]
            except KeyError as exc:
                raise CropError(
                    "Wycinek jako słownik potrzebuje x, y, width, height — "
                    f"brakuje {exc}"
                ) from exc
        elif isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            raise CropError(
                f"Nie umiem odczytać wycinka z {type(raw).__name__} — podaj "
                '"x,y,w,h", [x, y, w, h] albo {"x": …, "y": …, "width": …, "height": …}'
            )

        if len(parts) != 4 or any(value is None for value in parts):
            raise CropError(
                f"Wycinek potrzebuje czterech liczb (x, y, szerokość, wysokość), "
                f"dostałem {len(parts)}: {raw!r}"
            )
        try:
            return [float(str(value).strip().replace(",", ".")) for value in parts]
        except ValueError as exc:
            raise CropError(f"Wycinek {raw!r} zawiera coś, co nie jest liczbą") from exc

    def as_param(self) -> str:
        """Zapis do adresu — cztery liczby po przecinku, bez ogonów zer."""
        return ",".join(
            f"{value:g}" for value in (self.x, self.y, self.width, self.height)
        )

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Ramka w pikselach kadru `width`×`height`, jako (left, top, right, bottom).

        Zaokrąglenie w górę dla prawej i dolnej krawędzi, a nie w dół:
        wycinek o krawędź za duży pokazuje trochę blatu, o krawędź za mały —
        ucina nadruk.
        """
        left = max(0, int(self.x * width))
        top = max(0, int(self.y * height))
        right = min(width, max(left + 1, round((self.x + self.width) * width)))
        bottom = min(height, max(top + 1, round((self.y + self.height) * height)))
        return left, top, right, bottom


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int
    source_width: int
    source_height: int
    # Wycinek, jeśli agent dostał fragment kadru, a nie całość.
    crop: CropBox | None = None

    @property
    def approx_tokens(self) -> int:
        return self.width * self.height // TOKENS_PER_PIXEL_BLOCK

    @property
    def size_kb(self) -> int:
        return len(self.data) // 1024


def prepare_for_agent(
    path: Path | str,
    *,
    max_edge: int = MAX_EDGE,
    quality: int = QUALITY,
    crop: CropBox | None = None,
) -> PreparedImage:
    """Obróć wg EXIF, wytnij wskazany fragment, zmniejsz i przekoduj do JPEG.

    **Obrót z EXIF jest obowiązkowy, nie kosmetyczny.** Telefon zapisuje
    kadr poziomo i dokłada znacznik orientacji; bez zastosowania go agent
    dostaje zdjęcie położone na boku i czyta sitodruk pionowo. Zmierzone
    na realnych zdjęciach z Pixela: wszystkie trzy przyszły jako 4080×3072
    z orientacją wymagającą obrotu do pionu.

    **Kolejność jest istotna: obrót → wycinek → zmniejszenie.** Wycinek
    liczy się w orientacji, w której agent widział zdjęcie, a wycinanie
    z oryginału (nie ze zmniejszonej kopii) jest tym, co daje zbliżenie:
    ćwiartka kadru 4080 px to nadal 1020 px szczegółu, więc drobny
    sitodruk staje się czytelny zamiast rozmyć się razem z całością.
    """
    with Image.open(path) as raw:
        source_size = raw.size
        image = ImageOps.exif_transpose(raw)
        # JPEG nie zna kanału alfa, a zdjęcia z telefonu bywają RGBA po
        # przejściu przez edytor.
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if crop is not None:
            image = image.crop(crop.pixels(image.width, image.height))
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        return PreparedImage(
            data=buffer.getvalue(),
            width=image.width,
            height=image.height,
            source_width=source_size[0],
            source_height=source_size[1],
            crop=crop,
        )
