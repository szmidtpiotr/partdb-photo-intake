"""Kubełki ilości klasy B (#29, ADR-007)."""

import pytest

from bom_agent.quantity import UNCOUNTED, UnknownBucket, bucket_of, parse_bucket


@pytest.mark.parametrize(
    ("word", "amount"),
    [
        ("mało", 5),
        ("MAŁO", 5),
        ("malo", 5),
        ("sporo", 50),
        ("dużo", 200),
        ("duzo", 200),
        ("pełno", 200),
        ("  kilka  ", 5),
    ],
)
def test_bucket_words_map_to_the_middle_of_their_range(word, amount):
    """Przelicznik jest wspólny, żeby „dużo" znaczyło to samo w każdej
    sesji — inaczej zrzut stanu przestaje być porównywalny."""
    assert parse_bucket(word) == (amount, True)


def test_nothing_left_is_exact_knowledge_not_an_estimate():
    """„Nie ma" wiem dokładnie; przybliżeniem jest dopiero „sporo"."""
    assert parse_bucket("brak") == (0, False)


def test_unknown_word_is_refused_instead_of_silently_becoming_zero():
    """Ciche zero zapisałoby brak części, która leży w kuwecie."""
    with pytest.raises(UnknownBucket, match="paręnaście"):
        parse_bucket("paręnaście")


@pytest.mark.parametrize(
    ("amount", "word"),
    [(0, UNCOUNTED), (5, "mało"), (9, "mało"), (10, "sporo"), (100, "sporo")],
)
def test_amount_renders_back_as_a_bucket_word(amount, word):
    assert bucket_of(amount) == word


def test_over_a_hundred_is_the_top_bucket():
    assert bucket_of(101) == "dużo"
    assert bucket_of(5000) == "dużo"
