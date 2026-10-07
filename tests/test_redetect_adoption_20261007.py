"""AUTODET-17(2026-10-07) — Undeclared 재감지가 부팅과 같은 채택 규칙을 쓴다.

부팅이 거부한 지문(형식만 Runze · 선언 불일치)으로 재감지가 재기동하면, 다시 부팅해도 같은 판정이라
60초마다 재시작이 끝없이 반복된다(검증 r5 pi-app P1-1). 재기동 조건 = "부팅하면 기종이 채택되는가".
"""

import pytest

from senlyt_pi.adapters.pump_model_detect import DetectResult
from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter
from senlyt_pi.app.bootstrap import adoptable_detected_model
from senlyt_pi.app.senlytd import redetected_model_to_adopt
from senlyt_pi.obs.log import StructuredLogger
import test_pump_model_autodetect_20260914 as autodetect
from test_pump_model_autodetect_20260914 import RUNZE, TECAN

TECAN_SNAP = {
    "pumpPreset": {"pumpPresetId": "tecan_xcalibur", "pumpFullStroke": 3000, "syringeCapacityMl": 0.5},
    "hardware": {
        "sensoriumVersion": "sensorium-fragrance-1.0.0+tecan",
        "pumpModel": "tecan_xcalibur",
        "valvePortCount": 12,
    },
}
NO_DECL = {"pumpPreset": {"syringeCapacityMl": 0.5}}

WEAK_RUNZE = DetectResult(model="sy01b", responding=(1,), fingerprints={1: "3.10"}, strong=False)
STRONG_RUNZE = DetectResult(model="sy01b", responding=(1,), fingerprints={1: RUNZE}, strong=True)
TECAN_FMT = DetectResult(model="tecan_xcalibur", responding=(1,), fingerprints={1: TECAN}, strong=False)
MIXED = DetectResult(model=None, responding=(1, 2), fingerprints={1: RUNZE, 2: TECAN}, mixed=True)
UNREADABLE = DetectResult(model=None, responding=(1,), unreadable=True)


@pytest.mark.parametrize(
    ("det", "declared", "expected"),
    [
        (WEAK_RUNZE, "tecan_xcalibur", None),
        (WEAK_RUNZE, None, None),
        (WEAK_RUNZE, "sy01b", "sy01b"),
        (STRONG_RUNZE, "tecan_xcalibur", "sy01b"),
        (TECAN_FMT, "sy01b", "tecan_xcalibur"),
        (TECAN_FMT, None, "tecan_xcalibur"),
        (MIXED, None, None),
        (UNREADABLE, "sy01b", None),
    ],
)
def test_adoption_rule(det, declared, expected):
    assert adoptable_detected_model(det, declared) == expected


class TestRedetectMatchesBoot:
    """부팅이 Undeclared 로 조립한 같은 지문은 재감지에서도 채택되지 않는다 = 재기동 없음."""

    def _components(self, tmp_path, det, *, snap):
        return autodetect.TestBootAssembly()._components(tmp_path, det, snap=snap)

    @pytest.mark.parametrize(("det", "snap"), [(WEAK_RUNZE, TECAN_SNAP), (WEAK_RUNZE, NO_DECL)])
    def test_rejected_at_boot_is_not_restart_trigger(self, tmp_path, det, snap):
        comp = self._components(tmp_path, det, snap=snap)
        assert isinstance(comp.engine, UndeclaredEngineAdapter)
        assert adoptable_detected_model(det, comp.declared_pump_model) is None

    def test_declared_model_is_carried_to_daemon(self, tmp_path):
        comp = self._components(tmp_path, WEAK_RUNZE, snap=TECAN_SNAP)
        assert comp.declared_pump_model == "tecan_xcalibur"
        assert comp.hardware_profile is None  # 부팅은 선언을 버렸지만 재감지 판정용 선언은 남는다.

    def test_adoptable_after_boot_still_restarts(self, tmp_path):
        """펌프가 늦게 켜진 경우(부팅 응답 0 → 선언 조립)와 달리, 실측 정확값이 나오면 재감지는 재기동한다."""
        comp = self._components(tmp_path, WEAK_RUNZE, snap=TECAN_SNAP)
        assert adoptable_detected_model(STRONG_RUNZE, comp.declared_pump_model) == "sy01b"
        assert adoptable_detected_model(TECAN_FMT, comp.declared_pump_model) == "tecan_xcalibur"


@pytest.mark.parametrize(
    ("det", "declared", "expected"),
    [
        (WEAK_RUNZE, "tecan_xcalibur", None),  # 사고 조건 — 재기동하면 무한 반복
        (WEAK_RUNZE, None, None),
        (STRONG_RUNZE, "tecan_xcalibur", "sy01b"),  # 정상 회복 — 재기동
        (TECAN_FMT, None, "tecan_xcalibur"),
        (MIXED, None, None),
        (None, None, None),  # 감지 실패
    ],
)
def test_redetect_thread_decision(det, declared, expected):
    """재감지 스레드가 실제로 부르는 판정 함수(senlytd) — 재기동 기종 = 부팅 채택 규칙."""
    got = redetected_model_to_adopt({}, [1], declared, StructuredLogger(), scan=lambda: ("/dev/ttyUSB0", det))
    assert got == expected
