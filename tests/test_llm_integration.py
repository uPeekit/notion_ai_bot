from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.llm.context import ContextBuilder
from app.llm.ollama import OllamaClient
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

pytestmark = pytest.mark.integration
OLLAMA = "http://127.0.0.1:11434"


def ollama_up() -> bool:
    try:
        return httpx.get(f"{OLLAMA}/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _model() -> str:
    if Path(".env").exists():
        try:
            return Settings().llm_model
        except Exception:
            return "llama3.1:8b"
    return "llama3.1:8b"


MODEL = _model()


@pytest.mark.skipif(not ollama_up(), reason="ollama not running")
async def test_real_model_buy_milk():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    async with OllamaClient(OLLAMA, MODEL) as c:
        models = await c.models()
        if MODEL not in models:
            pytest.skip(f"{MODEL} not pulled")
        interp, trace = await c.interpret("купи молоко в Рими", ctx, build_schema(ctx))
    assert interp.intent.value == "create"
    assert ctx.ref(interp.best.target).name == "Покупки"
