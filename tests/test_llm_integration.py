import httpx
import pytest

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


@pytest.mark.skipif(not ollama_up(), reason="ollama not running")
async def test_real_model_buy_milk():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    async with OllamaClient(OLLAMA, "qwen3:8b") as c:
        models = await c.models()
        assert "qwen3:8b" in models
        interp, trace = await c.interpret("купи молоко в Рими", ctx, build_schema(ctx))
    assert interp.intent.value == "create"
    assert ctx.ref(interp.best.target).name == "Покупки"
