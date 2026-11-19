"""Деление текста документа на фрагменты (KNOW-02)."""

from __future__ import annotations

from itertools import pairwise

from engine import documents


def test_paragraphs_are_kept_together_and_pages_never_mix():
    pages = [
        {"page": "1", "text": "Первый абзац.\n\nВторой абзац."},
        {"page": "2", "text": "Абзац второй страницы."},
    ]
    found = documents.chunk_pages(pages)
    assert [(page, text) for page, _, text in found] == [
        ("1", "Первый абзац.\n\nВторой абзац."),
        ("2", "Абзац второй страницы."),
    ]


def test_long_text_is_cut_by_words_with_overlap():
    words = " ".join(f"слово{number}" for number in range(1000))
    found = documents.chunk_pages([{"page": "7", "text": words}])
    assert len(found) > 3 and all(page == "7" for page, _, _ in found)
    assert all(len(text) <= documents.CHUNK for _, _, text in found)
    for (_, _, left), (_, _, right) in pairwise(found):
        tail = left.split()[-1]
        assert tail in right  # фраза на стыке попадает в оба фрагмента
    assert "слово999" in found[-1][2] and "слово0" in found[0][2]
