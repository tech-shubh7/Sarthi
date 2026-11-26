"""The company in a letter's greeting reads the way a person would write it."""

import pytest

from sarthi.documents.build import letter_company
from sarthi.documents.truth import style_problems


@pytest.mark.parametrize(
    ("given", "greeting"),
    [
        ("VY SYSTEMS PRIVATE LIMITED", "VY Systems"),
        ("Example Labs Pvt. Ltd.", "Example Labs"),
        ("Acme Inc.", "Acme"),
        ("TekskyES", "TekskyES"),
        ("IBM", "IBM"),
        ("PwC Acceleration Center India", "PwC Acceleration Center India"),
        ("Limited", "Limited"),
    ],
)
def test_greeting_company(given: str, greeting: str) -> None:
    assert letter_company(given) == greeting


def test_hedging_openers_are_style_problems() -> None:
    assert style_problems("The closest thing I have to what you are asking for is the chatbot I built.")
