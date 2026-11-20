"""Real AI calls with the models in your .env. Skipped by default; run with: uv run pytest -m live --no-cov

Needs at least one working model in SARTHI_LLM_FAST_MODELS (e.g. a running Ollama, or a provider key).
"""

from pathlib import Path

import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.providers import resolve
from sarthi.ai.scoring import LlmMatchScorer
from sarthi.config import Settings
from sarthi.db.ai_store import AiStore
from sarthi.db.connection import connect
from tests.matching.conftest import make_job, make_profile

pytestmark = pytest.mark.live

PROJECT_ENV = Path(__file__).parents[2] / ".env"


async def test_configured_model_scores_a_good_and_a_bad_fit(tmp_path: Path) -> None:
    settings = Settings(_env_file=PROJECT_ENV)
    endpoints = resolve(settings.llm_fast_models, settings)
    if not endpoints:
        pytest.skip("no AI model configured in .env")
    conn = connect(tmp_path / "live.db")
    client = AiClient(endpoints, AiStore(conn))
    scorer = LlmMatchScorer(client)
    profile = make_profile().model_copy(
        update={"resume_text": "4 years building Python and Go payment APIs on AWS."}
    )
    try:
        good = await scorer.score(make_job(), profile)
        if good is None:
            pytest.skip(
                f"no configured AI model answered ({', '.join(e.label for e in endpoints)}); is it running?"
            )
        bad = await scorer.score(
            make_job(title="Senior iOS Engineer", description="Swift, SwiftUI, 8 years of iOS."), profile
        )
    finally:
        await client.aclose()
        conn.close()
    assert bad is not None
    assert good.score > bad.score
    assert good.reasons
