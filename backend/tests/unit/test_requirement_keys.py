import pytest
from app.agent.requirements import canonical_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("order_number", "order_number"),
        ("Order ID", "order_number"),
        ("orderNo", "order_number"),
        ("order", "order_number"),
        ("damage_photo", "photos"),
        ("photos_of_damage", "photos"),
        ("picture", "photos"),
        ("shipping_address", "shipping_address"),
        ("delivery address", "shipping_address"),
        ("delivered_date", "delivery_date"),
        ("tracking_number", "tracking_number"),
        ("preferred_remedy", "preferred_remedy"),
        ("refund_or_replacement", "preferred_remedy"),
        ("bank statement showing both charges", "duplicate_charge_details"),
        ("something_else_entirely", "something_else_entirely"),
    ],
)
def test_canonical_key(raw: str, expected: str) -> None:
    assert canonical_key(raw) == expected
