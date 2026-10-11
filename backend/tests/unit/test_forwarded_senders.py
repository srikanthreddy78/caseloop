from app.forwarded_orders import sender_allowed
from app.models import Business


def _biz(*senders: str) -> Business:
    return Business(settings={"order_email_senders": list(senders)})


def test_nothing_is_trusted_until_senders_are_listed() -> None:
    assert not sender_allowed(_biz(), "orders@shopify.com")
    assert not sender_allowed(_biz("shopify.com"), "")
    assert not sender_allowed(_biz("shopify.com"), "not-an-address")


def test_address_and_domain_matches() -> None:
    biz = _biz("orders@shopify.com", "amazon.com")
    assert sender_allowed(biz, "Orders@Shopify.com")
    assert not sender_allowed(biz, "noreply@shopify.com")
    assert sender_allowed(biz, "auto-confirm@amazon.com")
    assert sender_allowed(biz, "ship-confirm@mail.amazon.com")
    assert not sender_allowed(biz, "x@notamazon.com")
    assert not sender_allowed(biz, "x@amazon.com.evil.example")


def test_dkim_pass_only_counts_for_listed_domains() -> None:
    biz = _biz("orders@shopify.com", "amazon.com")
    assert not sender_allowed(biz, "noreply@shopify.com", {"dkim": "fail"})
    assert not sender_allowed(biz, "noreply@shopify.com", {"dkim": "pass"})  # domain not listed
    assert sender_allowed(biz, "noreply@amazon.com", {"dkim": "pass"})
    assert not sender_allowed(biz, "x@evil.example", {"dkim": "pass"})
    # Richer shapes that name the signing domain.
    assert sender_allowed(
        biz, "x@evil.example", {"dkim": [{"result": "pass", "domain": "amazon.com"}]}
    )
    assert not sender_allowed(biz, "x@evil.example", {"dkim": {"result": "pass", "domain": "e.x"}})
    assert not sender_allowed(biz, "x@evil.example", "pass")
