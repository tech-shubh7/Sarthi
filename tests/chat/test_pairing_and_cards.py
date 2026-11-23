import sqlite3

from sarthi.chat.cards import display_company, job_card
from sarthi.chat.pairing import MAX_WRONG_GUESSES, new_code, owner_chat, redeem
from sarthi.db.chat import ChatStore
from sarthi.db.matches import MatchRecord
from tests.conftest import FakeClock
from tests.matching.conftest import make_job


def store(conn: sqlite3.Connection, clock: FakeClock) -> ChatStore:
    return ChatStore(conn, clock=clock)


def test_a_code_pairs_once(conn: sqlite3.Connection, clock: FakeClock) -> None:
    chats = store(conn, clock)
    code = new_code(chats, clock())
    assert len(code) == 12
    assert owner_chat(chats) is None
    assert redeem(chats, f"  {code.lower()} ", 111, clock())  # case and spaces do not matter
    assert owner_chat(chats) == 111
    assert not redeem(chats, code, 222, clock())  # used up
    assert owner_chat(chats) == 111
    assert code not in str(conn.execute("SELECT group_concat(value) FROM app_state").fetchone()[0])


def test_expired_code_fails(conn: sqlite3.Connection, clock: FakeClock) -> None:
    chats = store(conn, clock)
    code = new_code(chats, clock())
    clock.advance(16 * 60)
    assert not redeem(chats, code, 111, clock())
    assert owner_chat(chats) is None


def test_a_stranger_cannot_burn_the_code_with_one_guess(conn: sqlite3.Connection, clock: FakeClock) -> None:
    chats = store(conn, clock)
    code = new_code(chats, clock())
    for _ in range(MAX_WRONG_GUESSES - 1):
        assert not redeem(chats, "WRONGWRONG00", 666, clock())
    assert redeem(chats, code, 111, clock())


def test_too_many_wrong_guesses_cancel_the_code(conn: sqlite3.Connection, clock: FakeClock) -> None:
    chats = store(conn, clock)
    code = new_code(chats, clock())
    for _ in range(MAX_WRONG_GUESSES):
        redeem(chats, "WRONGWRONG00", 666, clock())
    assert not redeem(chats, code, 111, clock())
    assert not redeem(chats, "", 111, clock())


def test_card_shows_the_match_and_escapes_job_site_text() -> None:
    job = make_job(
        title="Backend <b>Engineer</b>",
        company="openai",
        location_text="Bengaluru & Remote",
        salary={"currency": "INR", "period": "year", "minimum": 1_200_000, "maximum": 1_800_000},
        url='https://example.com/jobs/1?a=1&b="x"',
    )
    match = MatchRecord(
        "j", "v", 78, "alert", "strong", None, ("Mentions python <script>",), ("Visa note",), True
    )
    card = job_card(job, match)
    assert card.startswith(
        "<b>Backend &lt;b&gt;Engineer&lt;/b&gt;</b>\nOpenai · Bengaluru &amp; Remote · Hybrid"
    )
    assert "INR 1,200,000–1,800,000 / year" in card
    assert "Match: <b>78%</b>" in card
    assert "✓ Mentions python &lt;script&gt;" in card
    assert "• Visa note" in card
    assert "<script>" not in card
    assert 'href="https://example.com/jobs/1?a=1&amp;b=%22x%22"' in card


def test_card_without_match_or_location() -> None:
    card = job_card(make_job(country_codes=[], remote_type="unknown", location_text=""), None)
    assert "Location not stated" in card
    assert "Match" not in card


def test_a_gap_is_shown_as_missing_not_as_a_match() -> None:
    # Seen live: "LangGraph ... missing from the resume" was shown with a check mark.
    match = MatchRecord("j", "v", 70, "alert", "strong", None, ("Python fits", "✗ No <b>Kafka</b>"), (), True)
    card = job_card(make_job(), match)
    assert "✓ Python fits" in card
    assert "✗ No &lt;b&gt;Kafka&lt;/b&gt;" in card
    assert "✓ ✗" not in card


def test_a_score_the_ai_never_checked_is_not_called_a_match() -> None:
    # Seen live: keyword-only scores of 100% while the AI's daily limit was spent.
    match = MatchRecord("j", "v", 100, "digest", "fair", None, ("Mentions python",), (), False)
    card = job_card(make_job(), match)
    assert "Keyword match: <b>100%</b> (not read by the AI yet)" in card
    assert "\nMatch:" not in card


def test_display_company() -> None:
    assert display_company("north-fulfil") == "North Fulfil"
    assert display_company("Razorpay") == "Razorpay"
    assert display_company("ACME Ltd") == "ACME Ltd"
