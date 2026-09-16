"""EnginePort 정식 계약 + 능력 seam 그물 (2026-09-03 검증 5팀 P1-3 승격).

- 코어 계약(EnginePort): 실어댑터 2종 + 더블 2종 전부가 만족해야 한다.
- 능력 확장(Probe/Health/PolledInit): **부재 자체가 신호**인 설계된 seam — 실어댑터는 전부
  지원, fake 는 probe/health/polled 부재(재발견 미주입·폴백 검증 경로), undeclared 는
  probe 만(무프로브 False 신고) — 이 비대칭이 바뀌면 데몬 정책이 조용히 바뀐 것이다.
- engine_wire_name: 클래스명 사전 → MODEL_ID 자기 선언 교체 후에도 와이어 값 바이트 불변.
"""

from senlyt_pi.adapters.fake_engine_adapter import FakeEnginePort
from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter
from senlyt_pi.app.daemon import engine_wire_name

CORE = ["aspirate", "dispense", "dispense_batch", "initialize", "run_op",
        "emergency_stop_all", "clear_estop", "close", "signal_stop"]


def test_core_contract_satisfied_by_all():
    for cls in (Sy01bEngineAdapter, TecanXCaliburEngineAdapter, FakeEnginePort,
                UndeclaredEngineAdapter):
        missing = [m for m in CORE if not callable(getattr(cls, m, None))]
        assert not missing, f"{cls.__name__} 코어 계약 누락: {missing}"


def test_capability_asymmetry_is_preserved():
    # 실어댑터 = 3능력 전부 / fake = 전부 부재 / undeclared = probe 만. (부재 = 정책 신호)
    caps = ("probe", "health_probe", "initialize_polled")
    for cls in (Sy01bEngineAdapter, TecanXCaliburEngineAdapter):
        assert all(callable(getattr(cls, c, None)) for c in caps), cls.__name__
    assert not any(callable(getattr(FakeEnginePort, c, None)) for c in caps)
    assert callable(getattr(UndeclaredEngineAdapter, "probe", None))
    assert not callable(getattr(UndeclaredEngineAdapter, "health_probe", None))
    assert not callable(getattr(UndeclaredEngineAdapter, "initialize_polled", None))


def test_engine_wire_name_uses_model_id_and_stays_byte_identical():
    assert engine_wire_name(FakeEnginePort()) == "fake"
    assert Sy01bEngineAdapter.MODEL_ID == "sy01b"
    assert TecanXCaliburEngineAdapter.MODEL_ID == "tecan_xcalibur"

    class _Mystery:  # 미지 더블 — 클래스명 폴백(침묵보다 이름).
        pass

    assert engine_wire_name(_Mystery()) == "_Mystery"
