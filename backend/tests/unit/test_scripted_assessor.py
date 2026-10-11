import pytest
from app.agent.fake_runner import _ORDER_RE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("my order #A12345 arrived broken", "A12345"),
        ("Order 98765", "98765"),
        ("order: ACME-2024-77", "ACME-2024-77"),
    ],
)
def test_order_number_extraction(text: str, expected: str) -> None:
    match = _ORDER_RE.search(text)
    assert match is not None
    assert match.group(1).upper() == expected


def test_no_order_number() -> None:
    assert _ORDER_RE.search("the lamp arrived cracked, what now?") is None


def test_order_number_with_filler_words() -> None:
    match = _ORDER_RE.search("Sorry for the delay! Order number is A12345. Photo attached.")
    assert match is not None and match.group(1) == "A12345"
