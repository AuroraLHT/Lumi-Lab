"""Unit coverage for the recipe helpers that parse human input."""

import pytest

from lumi.experiment.recipes import _ask_float

pytestmark = pytest.mark.asyncio


def _provider(answers):
    """A value_provider that hands back queued answers and records the prompts it saw."""
    queue = list(answers)
    seen: list[str] = []

    async def provide(prompt: str) -> str:
        seen.append(prompt)
        return queue.pop(0)

    provide.seen = seen  # type: ignore[attr-defined]
    return provide


async def test_ask_float_returns_a_clean_value():
    provide = _provider(["1.75"])
    assert await _ask_float(provide, "power: ") == 1.75
    assert provide.seen == ["power: "]


async def test_ask_float_reprompts_on_a_non_number_instead_of_raising():
    provide = _provider(["abc", "", "2.5"])
    value = await _ask_float(provide, "power: ")
    assert value == 2.5
    # The original prompt is preserved; the complaint is folded in ahead of it.
    assert provide.seen[0] == "power: "
    assert "not a number" in provide.seen[1] and provide.seen[1].endswith("power: ")
    assert "value is required" in provide.seen[2] and provide.seen[2].endswith("power: ")


async def test_ask_float_reprompts_when_out_of_range():
    provide = _provider(["300", "-5", "120"])
    value = await _ask_float(provide, "gain: ", minimum=0, maximum=240)
    assert value == 120
    assert "outside [0, 240]" in provide.seen[1]
    assert "outside [0, 240]" in provide.seen[2]


async def test_ask_float_accepts_the_range_boundaries():
    assert await _ask_float(_provider(["0"]), "g: ", minimum=0, maximum=240) == 0
    assert await _ask_float(_provider(["240"]), "g: ", minimum=0, maximum=240) == 240


async def test_ask_float_tolerates_a_none_answer():
    provide = _provider([None, "3"])
    assert await _ask_float(provide, "power: ") == 3
