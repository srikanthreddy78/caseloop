from app.agent.tools import scrub_pii


def test_card_and_ssn_are_removed() -> None:
    text = "card 4111 1111 1111 1111 and ssn 123-45-6789, order 55512"
    out = scrub_pii(text)
    assert "4111" not in out and "123-45-6789" not in out
    assert "order 55512" in out
