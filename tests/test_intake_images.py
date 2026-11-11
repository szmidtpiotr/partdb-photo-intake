"""Przygotowanie zdjęć dla agenta (#22)."""

from PIL import Image

from bom_agent.intake.images import MAX_EDGE, prepare_for_agent


def photo(tmp_path, size=(4080, 3072), name="foto.jpg", exif_orientation=None):
    path = tmp_path / name
    image = Image.new("RGB", size, (40, 60, 80))
    if exif_orientation is not None:
        exif = image.getexif()
        exif[274] = exif_orientation  # 274 = Orientation
        image.save(path, "JPEG", exif=exif)
    else:
        image.save(path, "JPEG")
    return path


def test_downscales_to_the_agreed_edge(tmp_path):
    prepared = prepare_for_agent(photo(tmp_path))

    assert max(prepared.width, prepared.height) == MAX_EDGE
    assert prepared.source_width == 4080
    # Zdjęcie z telefonu to ~16 700 tokenów; po zmniejszeniu ma być rząd 2,5 tys.
    assert prepared.approx_tokens < 3000


def test_exif_rotation_is_applied(tmp_path):
    """Telefon zapisuje kadr poziomo ze znacznikiem obrotu. Bez jego
    zastosowania agent czyta sitodruk położony na boku."""
    prepared = prepare_for_agent(photo(tmp_path, exif_orientation=6))

    assert prepared.source_width > prepared.source_height  # zapisany poziomo
    assert prepared.height > prepared.width  # pokazany pionowo


def test_small_photo_is_not_upscaled(tmp_path):
    prepared = prepare_for_agent(photo(tmp_path, size=(640, 480)))

    assert (prepared.width, prepared.height) == (640, 480)


def test_output_is_a_valid_jpeg(tmp_path):
    import io

    prepared = prepare_for_agent(photo(tmp_path))
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.format == "JPEG"
        assert (out.width, out.height) == (prepared.width, prepared.height)


def test_rgba_source_is_converted(tmp_path):
    """JPEG nie zna przezroczystości — bez konwersji zapis by wybuchł."""
    path = tmp_path / "z-alfa.png"
    Image.new("RGBA", (2000, 1500), (10, 20, 30, 128)).save(path)

    prepared = prepare_for_agent(path)
    assert prepared.data[:2] == b"\xff\xd8"  # nagłówek JPEG
