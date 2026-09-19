"""Structured plan steps resolved without the model (app.conversation.steps)."""

from app.conversation.plan import StepField, StepSpec
from app.conversation.steps import to_interpretation
from app.validation.semantic import SemanticValidator
from tests.helpers import ctx_and_snapshot


def step(**kw) -> StepSpec:
    kw.setdefault("text", "шаг")
    return StepSpec(**kw)


def test_a_row_with_fields_resolves_to_the_same_answer_the_model_would_give():
    ctx, snap = ctx_and_snapshot()
    interp = to_interpretation(step(
        action="create", target="Задачи", title="Купить билеты",
        fields=[StepField(name="Приоритет", value="A"), StepField(name="Срок", value="2026-09-12"),
                StepField(name="Теги", value="дом, работа")]), ctx)
    result = SemanticValidator().validate(interp, ctx, snap)
    best = result.best
    assert result.intent == "create" and best.target.name == "Задачи"
    by_name = {f.field.name: f for f in best.fields.values() if f.status == "value"}
    assert by_name["Задача"].value == "Купить билеты"
    assert by_name["Приоритет"].value.name == "A"
    assert [o.name for o in by_name["Теги"].value] == ["дом", "работа"]
    assert str(by_name["Срок"].value.start) == "2026-09-12"
    assert all(f.confidence == 1.0 for f in by_name.values())


def test_names_are_matched_loosely_and_targets_also_by_path_or_label():
    ctx, _ = ctx_and_snapshot()
    for name in ("Покупки", "покупки", "«Покупки»", "Дом / Покупки", "Покупки [database]"):
        interp = to_interpretation(step(action="create", target=name, title="Хлеб"), ctx)
        assert interp is not None and interp.best.target == ctx.target_key("ds-buy"), name


def test_a_checkbox_a_number_and_an_append_with_content():
    ctx, snap = ctx_and_snapshot()
    interp = to_interpretation(step(
        action="create", target="Покупки", title="Молоко",
        fields=[StepField(name="Куплено", value="да"), StepField(name="Количество", value="2")]),
        ctx)
    best = SemanticValidator().validate(interp, ctx, snap).best
    values = {f.field.name: f.value for f in best.fields.values() if f.status == "value"}
    assert values["Куплено"] is True and values["Количество"] == 2

    appended = to_interpretation(step(action="append", target="Идеи", content="## План\n- раз"),
                                 ctx)
    assert appended.intent.value == "append" and appended.best.content.startswith("## План")


def test_a_web_step_carries_the_query_and_the_media():
    ctx, _ = ctx_and_snapshot()
    interp = to_interpretation(step(action="append", target="Идеи", web_query="тории",
                                    web_media="text_and_images"), ctx)
    assert interp.best.web_query == "тории" and interp.best.web_media == "text_and_images"


def test_what_does_not_resolve_is_left_to_the_model():
    ctx, _ = ctx_and_snapshot()
    assert to_interpretation(step(text="что-нибудь"), ctx) is None  # action "free"
    assert to_interpretation(step(action="create", target="Нет такой базы"), ctx) is None
    assert to_interpretation(step(action="create", target="Покупки",
                                  fields=[StepField(name="Ширина", value="5")]), ctx) is None
    assert to_interpretation(step(action="create", target="Покупки",
                                  fields=[StepField(name="Магазин", value="Селвер")]), ctx) is None
    assert to_interpretation(step(action="create", target="Покупки",
                                  fields=[StepField(name="Количество", value="много")]),
                             ctx) is None
