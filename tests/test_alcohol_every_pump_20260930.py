"""향연 알코올 통 = 펌프마다 필수(2026-09-30 사용자 결정) — pi 이중 방어.

서버가 `alcohol_missing` 으로 먼저 막지만, pi 도 스냅샷 통 배치로 **빈 구멍에서 알코올을 빠는** 흡입을 거부한다.
헤이센릿(향장향·식향)은 옛 관례(P1 = 알코올 · 명시 없는 옛 배치)를 존중해 "다른 액체가 꽂힌 구멍"만 거부한다.
"""

from __future__ import annotations

import pytest

from senlyt_pi.adapters.settings_source import alcohol_carrier_rule_from_settings
from senlyt_pi.core.pump_guard import SyringeSpec
from senlyt_pi.core.wire_messages import RecipeStep
from senlyt_pi.pipeline.recipe_resolver import RecipeResolver, RecipeValidationError


def _port(liquid, enabled=True):
    return {"liquid": liquid, "enabled": enabled, "aspirateSpeedHz": None, "dispenseSpeedHz": None}


def _snap(contract, p1):
    return {
        "hardware": {"contractId": contract},
        "pumpPorts": {"1": {"1": p1, "2": _port("bergamot"), "11": _port("output"), "12": _port("air")}},
    }


def _alcohol_batch(in_port=1):
    return [RecipeStep.from_json({
        "kind": "batchSyringe", "idx": 0, "stage": 0, "pumpAddr": 1, "outPort": 11,
        "dispenseSpeedHz": 3000, "slope": 7,
        "aspirations": [{"flavor": "alcohol", "inPort": in_port, "volume": 225.0, "aspirateSpeedHz": 2000}],
    })]


def _resolver(snap):
    rr = RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)})
    rr.port_liquids, rr.alcohol_strict = alcohol_carrier_rule_from_settings(snap)
    return rr


class TestRule:
    def test_strictness_by_contract(self):
        assert alcohol_carrier_rule_from_settings(_snap("sensorium-icad-0.1.0+tecan", _port("alcohol")))[1] is True
        assert alcohol_carrier_rule_from_settings(_snap("sensorium-fragrance-1.0.0", _port("alcohol")))[1] is False
        assert alcohol_carrier_rule_from_settings(_snap("sensorium-expo-0.1.2+tecan", _port("alcohol")))[1] is False
        assert alcohol_carrier_rule_from_settings({}) == (None, False)

    def test_disabled_and_empty_read_as_none(self):
        table, _ = alcohol_carrier_rule_from_settings(_snap("sensorium-icad-0.1.0", _port("alcohol", False)))
        assert table[1][1] is None
        table, _ = alcohol_carrier_rule_from_settings(_snap("sensorium-icad-0.1.0", _port(None)))
        assert table[1][1] is None


class TestResolver:
    def test_icad_explicit_alcohol_passes(self):
        res = _resolver(_snap("sensorium-icad-0.1.0+tecan", _port("alcohol"))).resolve(_alcohol_batch())
        assert res.steps[0].aspirations[0].steps == 675

    @pytest.mark.parametrize("p1", [_port(None), _port("alcohol", False)])
    def test_icad_empty_or_disabled_port_rejected(self, p1):
        with pytest.raises(RecipeValidationError) as ei:
            _resolver(_snap("sensorium-icad-0.1.0+tecan", p1)).resolve(_alcohol_batch())
        assert ei.value.reason == "alcohol_port_missing"

    def test_icad_missing_port_key_rejected(self):
        snap = _snap("sensorium-icad-0.1.0", _port("alcohol"))
        del snap["pumpPorts"]["1"]["1"]
        with pytest.raises(RecipeValidationError) as ei:
            _resolver(snap).resolve(_alcohol_batch())
        assert ei.value.reason == "alcohol_port_missing"

    @pytest.mark.parametrize("contract", ["sensorium-icad-0.1.0", "sensorium-fragrance-1.0.0"])
    def test_other_liquid_in_port_rejected_everywhere(self, contract):
        with pytest.raises(RecipeValidationError) as ei:
            _resolver(_snap(contract, _port("vanilla"))).resolve(_alcohol_batch())
        assert ei.value.reason == "alcohol_port_conflict"

    def test_heysenlyt_legacy_empty_p1_still_allowed(self):
        # 헤이센릿 옛 배치(P1 에 명시 없음 = 물리 관례상 알코올) — 회귀 금지.
        res = _resolver(_snap("sensorium-fragrance-1.0.0", _port(None))).resolve(_alcohol_batch())
        assert res.steps[0].aspirations[0].flavor == "alcohol"

    def test_no_snapshot_keeps_old_behavior(self):
        rr = RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)})
        assert rr.resolve(_alcohol_batch()).steps[0].aspirations[0].steps == 675
