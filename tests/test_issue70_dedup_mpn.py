"""TDD: Issue #70 — sam zgodny MPN nie scala modułów dzielących ten sam układ.

MPN identyfikuje układ scalony, nie moduł (ADR-019). Dwie różne płytki na tym
samym `TP4056` muszą zostać dwiema częściami — albo, przy niepewności, trafić
„do rozstrzygnięcia", a nie scalić się po cichu.
"""

from bom_agent.intake.commit import IntakeCommitter, PartSpec


def _committer() -> IntakeCommitter:
    # `match()` nie dotyka klienta Part-DB — pracuje na gotowej liście części.
    return IntakeCommitter(client=None)


# Stan bazy z sesji 2026-08-15: ładowarka TP4056 *z zabezpieczeniem* DW01A.
CHARGER_WITH_PROTECTION = {
    "@id": "/api/parts/126",
    "name": "Ładowarka TP4056 z zabezpieczeniem DW01A",
    "manufacturer_product_number": "TP4056",
}


# ─── Test główny — regresja #70 ──────────────────────────────────────────────


def test_same_mpn_different_name_is_not_a_confident_merge():
    """Mini ładowarka FC-75 (sam TP4056) nie może dokleić się do części #126
    (ładowarka z DW01A) tylko dlatego, że dzielą MPN `TP4056`."""
    spec = PartSpec.from_dict({"name": "Mini ładowarka FC-75", "mpn": "TP4056"})

    hit, similar = _committer().match(spec, [CHARGER_WITH_PROTECTION])

    assert hit is None, "sam MPN nie jest tożsamością modułu (ADR-019)"
    assert CHARGER_WITH_PROTECTION in similar, (
        "różny moduł z tym samym MPN → kandydat do rozstrzygnięcia, nie ciche scalenie"
    )


def test_same_mpn_different_name_stops_the_commit():
    """Rozbieżna nazwa przy zgodnym MPN wstrzymuje zapis — plan pyta."""
    from bom_agent.intake.commit import PlannedPart

    planned = PlannedPart(
        spec=PartSpec.from_dict({"name": "Mini ładowarka FC-75", "mpn": "TP4056"}),
        existing_part=None,
        similar=[CHARGER_WITH_PROTECTION],
    )
    assert planned.undecided
    assert planned.action == "DO ROZSTRZYGNIĘCIA"


# ─── Backward compatibility ──────────────────────────────────────────────────


def test_same_normalized_name_still_merges():
    """Nazwa nadal jest tożsamością: ten sam moduł zapisany drugi raz scala się."""
    existing = {
        "@id": "/api/parts/1",
        "name": "Ładowarka TP4056 z zabezpieczeniem DW01A",
    }
    spec = PartSpec.from_dict(
        {"name": "ładowarka tp4056 z zabezpieczeniem dw01a", "mpn": "TP4056"}
    )

    hit, similar = _committer().match(spec, [existing])

    assert hit is existing, "zgodna znormalizowana nazwa → pewne trafienie"
    assert similar == []


def test_same_mpn_and_same_name_still_merges():
    """Zgodny MPN obok zgodnej nazwy nie przeszkadza — to nadal ta sama część."""
    spec = PartSpec.from_dict(
        {"name": "Ładowarka TP4056 z zabezpieczeniem DW01A", "mpn": "TP4056"}
    )

    hit, _ = _committer().match(spec, [CHARGER_WITH_PROTECTION])

    assert hit is CHARGER_WITH_PROTECTION


def test_force_new_still_bypasses_the_mpn_candidate():
    """`force_new` przechodzi obok podobnych — świadomy wybór człowieka."""
    spec = PartSpec.from_dict(
        {"name": "Mini ładowarka FC-75", "mpn": "TP4056", "force_new": True}
    )

    hit, similar = _committer().match(spec, [CHARGER_WITH_PROTECTION])

    assert hit is None
    assert similar == []
