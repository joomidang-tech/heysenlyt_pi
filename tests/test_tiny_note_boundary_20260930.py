"""web·pi 반 스텝 경계 교차(2026-09-30 · 극소 향료 정책) — web `__tests__/lib/server/tinyNotePolicy.test.ts` ⑤ 와 **같은 값**.

web 은 반 스텝 미만(0스텝) 향료를 빼거나 1스텝으로 올려 봉투에 싣지 않는다. pi 는 이중 방어로 같은 경계에서
`derived_zero_steps` 거부를 유지한다 — 둘의 경계가 어긋나면 web 이 통과시킨 양을 pi 가 거부(또는 그 반대)한다.
"""

from __future__ import annotations

import pytest

from senlyt_pi.core.pump_guard import SyringeSpec

HALF_STEP_CASES = [
    (1.0, 1 / 6 - 1e-9, True),
    (1.0, 1 / 6 + 1e-9, False),
    (1.0, 1 / 3, False),
    (5.0, 5 / 6 - 1e-9, True),
    (5.0, 5 / 6 + 1e-9, False),
    (5.0, 5 / 3, False),
]


@pytest.mark.parametrize("cap, volume_ul, tiny", HALF_STEP_CASES)
def test_half_step_boundary_matches_web(cap, volume_ul, tiny):
    spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=cap)
    # recipe_resolver 의 derived_zero_steps 판정식 그대로
    zero = spec.steps_for_volume_ul(volume_ul) < 1 and volume_ul * spec.steps_per_ml / 1000 < 0.5
    assert zero is tiny


@pytest.mark.parametrize("cap", [1.0, 5.0])
def test_one_step_round_up_is_exactly_one_step(cap):
    # web roundUpOneStep 이 싣는 양 = 1스텝 µL(용량µL ÷ 3000) → pi 가 정확히 1스텝으로 낸다.
    spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=cap)
    assert spec.steps_for_volume_ul(cap * 1000 / 3000) == 1
