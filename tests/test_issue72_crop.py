"""TDD: Issue #72 — kilka różnych płytek na jednym zdjęciu.

Odwrotność #66. Tam kilka ujęć opisywało jedną rzecz; tu jedno ujęcie
pokazuje kilka rzeczy — tak wygląda zdjęcie wysypanej na blat paczki.
Każdy moduł jest osobną częścią i ma dostać **swój** obraz: wycinek kadru,
a nie ten sam kadr z czterema obcymi płytkami powtórzony pięć razy.

Wycinek nie jest nowym plikiem. To cztery liczby w adresie zdjęcia
(`…/photo?crop=x,y,w,h`), z których serwer odtwarza fragment na żądanie —
oryginał zostaje jedynym plikiem na dysku i w kopii zapasowej.
"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PilImage

from bom_agent import mcp_server
from bom_agent.config import Settings, load_settings
from bom_agent.db import Database
from bom_agent.intake.commit import CommitError, IntakeCommitter, PartSpec, SourcePhoto
from bom_agent.intake.images import CropBox, CropError, prepare_for_agent
from bom_agent.intake.queue import IntakeItem, IntakeQueue, Shot
from bom_agent.web import create_app

# Kwadranty w czterech kolorach: po wycięciu widać po samym kolorze, czy
# ramka trafiła tam, gdzie miała.
QUADRANTS = {
    "lewy górny": (200, 20, 20),
    "prawy górny": (20, 200, 20),
    "lewy dolny": (20, 20, 200),
    "prawy dolny": (200, 200, 20),
}


def four_colour_photo(path, size=(4000, 3000)):
    width, height = size
    image = PilImage.new("RGB", size)
    half_w, half_h = width // 2, height // 2
    boxes = {
        "lewy górny": (0, 0, half_w, half_h),
        "prawy górny": (half_w, 0, width, half_h),
        "lewy dolny": (0, half_h, half_w, height),
        "prawy dolny": (half_w, half_h, width, height),
    }
    for name, box in boxes.items():
        image.paste(QUADRANTS[name], box)
    image.save(path, "JPEG", quality=95)
    return path


def dominant(data: bytes) -> tuple[int, int, int]:
    with PilImage.open(io.BytesIO(data)) as image:
        # Środek wycinka, żeby ominąć artefakty JPEG na styku kwadrantów.
        pixel = image.convert("RGB").resize((3, 3)).getpixel((1, 1))
    return pixel


def closest_quadrant(data: bytes) -> str:
    r, g, b = dominant(data)
    return min(
        QUADRANTS,
        key=lambda name: sum(
            (a - c) ** 2 for a, c in zip(QUADRANTS[name], (r, g, b), strict=True)
        ),
    )


# ─── Współrzędne: ułamki krawędzi, nie piksele ───────────────────────────────


def test_crop_accepts_the_three_shapes_an_agent_actually_writes():
    """Adres, lista i słownik to ten sam prostokąt — model pisze każdą z tych
    form naturalnie, a odmowa kosztowałaby wywołanie narzędzia więcej."""
    expected = CropBox(x=0.1, y=0.2, width=0.3, height=0.4)

    assert CropBox.parse("0.1,0.2,0.3,0.4") == expected
    assert CropBox.parse([0.1, 0.2, 0.3, 0.4]) == expected
    assert CropBox.parse({"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}) == expected
    assert CropBox.parse({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}) == expected


def test_percent_is_recognised_without_asking():
    """`[10, 20, 30, 40]` znaczy „procent kadru". Ułamek nie może mieć
    składowej większej od 1, więc te dwa zapisy są rozróżnialne bez pytania —
    a wzięcie procentów za ułamki wycięłoby cały kadr zamiast jednej płytki."""
    assert CropBox.parse([10, 20, 30, 40]) == CropBox.parse([0.1, 0.2, 0.3, 0.4])


def test_frame_drawn_slightly_over_the_edge_is_trimmed_not_rejected():
    """Ramka „na oko" wystaje o włos i to jest w porządku — obcinamy ją do
    kadru zamiast odrzucać rozpoznanie z powodu jednego procenta."""
    box = CropBox.parse([0.5, 0.5, 0.51, 0.52])

    assert box.x + box.width == pytest.approx(1.0)
    assert box.y + box.height == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.1,0.2,0.3", "czterech liczb"),
        ([0.1, 0.2, 0.001, 0.4], "jednostkach"),
        ([0.5, 0.5, 0.9, 0.9], "za krawędź"),
        ("a,b,c,d", "nie jest liczbą"),
        ([300, 300, 300, 300], "poza kadr"),
    ],
)
def test_nonsense_crop_says_what_is_wrong(raw, expected):
    with pytest.raises(CropError, match=expected):
        CropBox.parse(raw)


def test_url_form_round_trips():
    box = CropBox.parse([0.25, 0.5, 0.25, 0.5])
    assert box.as_param() == "0.25,0.5,0.25,0.5"
    assert CropBox.parse(box.as_param()) == box


# ─── Wycinanie: geometria i zbliżenie ────────────────────────────────────────


def test_crop_takes_the_region_the_coordinates_point_at(tmp_path):
    path = four_colour_photo(tmp_path / "blat.jpg")

    corners = {
        "lewy górny": [0, 0, 0.5, 0.5],
        "prawy górny": [0.5, 0, 0.5, 0.5],
        "lewy dolny": [0, 0.5, 0.5, 0.5],
        "prawy dolny": [0.5, 0.5, 0.5, 0.5],
    }
    for name, box in corners.items():
        fragment = prepare_for_agent(path, crop=CropBox.parse(box))
        assert closest_quadrant(fragment.data) == name


def test_crop_comes_from_the_original_so_it_really_zooms(tmp_path):
    """Wycinek robimy z oryginału, nie ze zmniejszonej kopii — ćwiartka kadru
    4000 px to nadal 1000 px szczegółu, czyli sitodruk nieczytelny w całym
    kadrze bywa czytelny w wycinku. To cały sens tej operacji."""
    path = four_colour_photo(tmp_path / "blat.jpg")

    whole = prepare_for_agent(path)
    quarter = prepare_for_agent(path, crop=CropBox.parse([0.5, 0.5, 0.5, 0.5]))

    # Ćwiartka szerokości oryginału (2000 px) po zmniejszeniu do 1568 daje
    # więcej pikseli na tę samą płytkę niż całość zmieszczona w 1568.
    assert quarter.width * 2 > whole.width
    assert quarter.crop is not None


def test_crop_is_measured_after_the_exif_rotation(tmp_path):
    """Agent widzi kadr obrócony wg EXIF i w tej orientacji liczy ramkę.
    Wycinanie przed obrotem zamieniłoby `x` z `y` i podpięło do części
    zdjęcie sąsiedniego modułu."""
    path = tmp_path / "obrocone.jpg"
    image = PilImage.new("RGB", (4000, 3000), QUADRANTS["lewy górny"])
    # Prawa połowa zapisanego (poziomego) kadru; po obrocie o 90° trafia
    # na dół, więc „dolna połowa" ma ją znaleźć.
    image.paste(QUADRANTS["prawy dolny"], (2000, 0, 4000, 3000))
    exif = image.getexif()
    exif[274] = 6  # Orientation: obróć do pionu
    image.save(path, "JPEG", exif=exif, quality=95)

    lower = prepare_for_agent(path, crop=CropBox.parse([0, 0.55, 1, 0.45]))
    assert closest_quadrant(lower.data) == "prawy dolny"


# ─── Adres wycinka: endpoint HTTP ────────────────────────────────────────────


@pytest.fixture
def web(tmp_path):
    db = Database(tmp_path / "bom.db")
    db.migrate()
    queue = IntakeQueue(db, tmp_path / "photos")

    class StubPartDb:
        async def ping(self):
            from bom_agent.partdb_client import PartDbStatus

            return PartDbStatus(reachable=True, authorized=True, total_parts=0)

        async def aclose(self):  # pragma: no cover
            pass

    app = create_app(
        load_settings({"PARTDB_API_TOKEN": "tcp_test"}),
        client=StubPartDb(),
        queue=queue,
    )
    with TestClient(app) as client:
        yield client


def upload(web, tmp_path):
    path = four_colour_photo(tmp_path / "wysyp.jpg")
    response = web.post(
        "/api/intake/photo",
        files={"photo": ("wysyp.jpg", path.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 201
    return response.json()


def test_photo_endpoint_serves_the_requested_fragment(web, tmp_path):
    item = upload(web, tmp_path)

    response = web.get(item["photo_url"], params={"crop": "0.5,0,0.5,0.5"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert closest_quadrant(response.content) == "prawy górny"


def test_photo_endpoint_without_crop_is_untouched(web, tmp_path):
    """Dotychczasowy adres oddaje plik z dysku bez przekodowania — PWA
    pokazuje dokładnie to, co telefon wysłał."""
    item = upload(web, tmp_path)
    path = four_colour_photo(tmp_path / "wysyp.jpg")

    response = web.get(item["photo_url"])

    assert response.content == path.read_bytes()


def test_bad_crop_in_the_url_is_a_client_error(web, tmp_path):
    item = upload(web, tmp_path)

    response = web.get(item["photo_url"], params={"crop": "polowa"})

    assert response.status_code == 400
    assert "wycinek" in response.json()["detail"].lower()


def test_fragment_url_is_cacheable(web, tmp_path):
    """Te same cztery liczby dają ten sam obraz, więc niech go trzyma
    przeglądarka i Part-DB — inaczej każde otwarcie listy części
    przelicza kilkanaście wycinków z wielomegabajtowych oryginałów."""
    item = upload(web, tmp_path)

    response = web.get(item["photo_url"], params={"crop": "0,0,0.5,0.5"})

    assert "max-age" in response.headers.get("cache-control", "")


# ─── Zapis: która płytka do której części ────────────────────────────────────

LOCATIONS = [
    {
        "@id": "/api/storage_locations/2",
        "name": "Kuweta A1",
        "full_path": "Karton A → Kuweta A1",
    }
]
CATEGORIES = [{"@id": "/api/categories/3", "name": "Czujniki"}]
ATTACHMENT_TYPES = [{"@id": "/api/attachment_types/2", "name": "Zdjęcie"}]

PHOTO = SourcePhoto(
    url="https://intake.example.com/api/intake/17/photo", item_id=17
)
SECOND_SHOT = SourcePhoto(
    url="https://intake.example.com/api/intake/18/photo", item_id=18
)


class FakeClient:
    def __init__(self, parts=None):
        self.parts = list(parts or [])
        self.created: list[tuple[str, dict]] = []
        self.patched: list[tuple[str, dict]] = []
        self._next_id = 100

    async def list_storage_locations(self):
        return LOCATIONS

    async def list_categories(self):
        return CATEGORIES

    async def list_parts(self):
        return self.parts

    async def list_attachment_types(self):
        return ATTACHMENT_TYPES

    async def create_attachment(self, payload):
        return await self.create("attachments", payload)

    async def create_part(self, payload):
        return await self.create("parts", payload)

    async def create_part_lot(self, payload):
        return await self.create("part_lots", payload)

    async def create(self, resource, payload):
        self._next_id += 1
        self.created.append((resource, payload))
        return {"@id": f"/api/{resource}/{self._next_id}", **payload}

    async def patch(self, iri, payload):
        self.patched.append((iri, payload))
        return {"@id": iri, **payload}

    def attachments(self):
        return [
            payload for resource, payload in self.created if resource == "attachments"
        ]


def two_boards():
    return [
        PartSpec.from_dict(
            {
                "name": "HC-SR04",
                "amount": 1,
                "category": "Czujniki",
                "crop": [0, 0, 0.5, 1],
            }
        ),
        PartSpec.from_dict(
            {
                "name": "DHT22",
                "amount": 1,
                "category": "Czujniki",
                "crop": [0.5, 0, 0.5, 1],
            }
        ),
    ]


async def test_each_board_gets_its_own_fragment_and_nothing_else():
    """Sedno #72: dwie płytki z jednego kadru → dwie części, każda ze swoim
    wycinkiem. Doklejenie obok niego całego kadru wyglądałoby na hojność,
    a odebrałoby miniaturze sens — obie części miałyby to samo zdjęcie."""
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan(two_boards(), "A/A1", photos=[PHOTO])
    await committer.execute(plan)

    urls = [payload["url"] for payload in client.attachments()]
    assert urls == [
        f"{PHOTO.url}?crop=0,0,0.5,1",
        f"{PHOTO.url}?crop=0.5,0,0.5,1",
    ]
    # Miniaturą każdej części zostaje jej własny wycinek.
    assert len(client.patched) == 2
    assert all("master_picture_attachment" in payload for _, payload in client.patched)


async def test_attachment_names_differ_so_partdb_accepts_them():
    """Part-DB odpowiada `422: name: This value is already used` na drugi
    załącznik o tej samej nazwie w obrębie części. Współrzędne w nazwie
    rozróżniają wycinki tego samego zdjęcia."""
    client = FakeClient()
    committer = IntakeCommitter(client)

    plan = await committer.plan(two_boards(), "A/A1", photos=[PHOTO])
    await committer.execute(plan)

    names = [payload["name"] for payload in client.attachments()]
    assert len(set(names)) == len(names)
    assert all("intake #17" in name for name in names)


async def test_part_without_crop_still_gets_every_shot():
    """Zachowanie z #66 zostaje nietknięte: bez wycinka część dostaje
    wszystkie ujęcia, bo wszystkie pokazują ją samą."""
    client = FakeClient()
    committer = IntakeCommitter(client)

    spec = PartSpec.from_dict({"name": "HC-SR04", "category": "Czujniki"})
    plan = await committer.plan([spec], "A/A1", photos=[PHOTO, SECOND_SHOT])
    await committer.execute(plan)

    urls = [payload["url"] for payload in client.attachments()]
    assert urls == [PHOTO.url, SECOND_SHOT.url]


async def test_crop_can_point_at_a_later_shot():
    """Pozycja może mieć i kilka ujęć, i kilka płytek w jednym z nich."""
    client = FakeClient()
    committer = IntakeCommitter(client)

    spec = PartSpec.from_dict(
        {"name": "DHT22", "category": "Czujniki", "crop": [0.5, 0, 0.5, 1], "shot": 2}
    )
    plan = await committer.plan([spec], "A/A1", photos=[PHOTO, SECOND_SHOT])
    await committer.execute(plan)

    assert client.attachments()[0]["url"] == f"{SECOND_SHOT.url}?crop=0.5,0,0.5,1"


async def test_crop_from_a_shot_that_does_not_exist_stops_the_plan():
    """Zły numer ujęcia to pomyłka agenta i ma się wydać w planie, a nie
    skończyć zdjęciem czegoś innego przy części."""
    committer = IntakeCommitter(FakeClient())
    spec = PartSpec.from_dict(
        {"name": "DHT22", "category": "Czujniki", "crop": [0, 0, 0.5, 1], "shot": 3}
    )

    with pytest.raises(CommitError, match="ujęcia 3"):
        await committer.plan([spec], "A/A1", photos=[PHOTO])


def test_nonsense_crop_names_the_part_it_came_from():
    with pytest.raises(CommitError, match="HC-SR04"):
        PartSpec.from_dict({"name": "HC-SR04", "crop": [0.1, 0.2]})


def test_missing_crop_keeps_the_old_behaviour():
    assert PartSpec.from_dict({"name": "HC-SR04"}).crop is None
    assert PartSpec.from_dict({"name": "HC-SR04", "crop": ""}).crop is None
    assert PartSpec.from_dict({"name": "HC-SR04"}).shot == 1


async def test_plan_lists_the_fragments_before_anything_is_written():
    """Plan jest jedynym miejscem, w którym da się zauważyć, że dwie płytki
    dostały tę samą ramkę — czyli że jedna z nich ma zdjęcie sąsiada."""
    committer = IntakeCommitter(FakeClient())

    plan = await committer.plan(two_boards(), "A/A1", photos=[PHOTO])
    text = plan.as_text()

    assert "Wycinki" in text
    assert "crop=0,0,0.5,1" in text
    assert "crop=0.5,0,0.5,1" in text
    assert "HC-SR04" in text and "DHT22" in text


async def test_crop_on_an_existing_part_is_reported_not_silently_dropped():
    """Zdjęcie dokładamy tylko przy nowej części. Agent wskazał wycinek
    i miałby prawo sądzić, że trafił do bazy — mówimy wprost, że nie."""
    known = {"@id": "/api/parts/1", "name": "HC-SR04"}
    client = FakeClient(parts=[known])
    committer = IntakeCommitter(client)

    spec = PartSpec.from_dict({"name": "HC-SR04", "crop": [0, 0, 0.5, 1]})
    plan = await committer.plan([spec], "A/A1", photos=[PHOTO])
    await committer.execute(plan)

    assert client.attachments() == []
    assert any("wycinek" in warning for warning in plan.warnings)


# ─── Narzędzie MCP: sprawdzenie ramki i doczytanie nadruku ───────────────────


def _item(tmp_path, shots=1):
    paths = []
    for index in range(shots):
        path = four_colour_photo(tmp_path / f"ujecie-{index}.jpg")
        paths.append(path.rename(tmp_path / f"kadr-{index}.jpg"))
    return IntakeItem(
        id=17,
        kind="photo",
        status="pending",
        created_at="2026-08-20T10:00:00+00:00",
        payload_path=str(paths[0]),
        shots=tuple(
            Shot(id=17 + index, payload_path=str(path))
            for index, path in enumerate(paths)
        ),
    )


@pytest.fixture
def zoom_queue(monkeypatch, tmp_path):
    def install(item):
        class Queue:
            async def get(self, item_id):
                return item

        monkeypatch.setattr(mcp_server, "get_queue", lambda: Queue())
        monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings())
        return item

    return install


async def test_intake_zoom_is_registered_with_the_coordinate_contract():
    tools = await mcp_server.mcp.list_tools()
    tool = next((t for t in tools if t.name == "intake_zoom"), None)

    assert tool is not None, "narzędzie bez rejestracji nie istnieje dla agenta"
    assert set(tool.input_schema["properties"]) >= {"item_id", "crop", "shot"}
    # Bez tego zdania agent nie wie, czy liczyć w pikselach czy w ułamkach.
    assert "ułamk" in (tool.description or "")


async def test_intake_zoom_returns_the_fragment_as_an_image(zoom_queue, tmp_path):
    zoom_queue(_item(tmp_path))

    result = await mcp_server.mcp.call_tool(
        "intake_zoom", {"item_id": 17, "crop": "0.5,0.5,0.5,0.5"}
    )

    images = [block for block in result.content if not hasattr(block, "text")]
    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    assert len(images) == 1
    assert "wycinek 0.5,0.5,0.5,0.5" in text


async def test_intake_zoom_refuses_a_shot_that_is_not_there(zoom_queue, tmp_path):
    zoom_queue(_item(tmp_path))

    result = await mcp_server.mcp.call_tool(
        "intake_zoom", {"item_id": 17, "crop": "0,0,0.5,0.5", "shot": 4}
    )

    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    assert "ujęć" in text and "4" in text


async def test_intake_zoom_explains_a_bad_crop(zoom_queue, tmp_path):
    zoom_queue(_item(tmp_path))

    result = await mcp_server.mcp.call_tool(
        "intake_zoom", {"item_id": 17, "crop": "cala plytka"}
    )

    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    assert "Zły wycinek" in text


async def test_intake_pending_tells_the_agent_about_grouped_shots():
    """Reguła musi być w opisie narzędzia, nie w dokumentacji — agent czyta
    opis, a bez niego pięć płytek skończy jako jedna część albo pięć części
    z tym samym zdjęciem."""
    tools = await mcp_server.mcp.list_tools()
    description = next(t for t in tools if t.name == "intake_pending").description

    assert "crop" in description
    assert "intake_zoom" in description
