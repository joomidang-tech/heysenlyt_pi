"""매뉴얼 대조 검증 반영(2026-10-01 · Cavro XCalibur 20733085-C).

① Tecan 속도 Hz = 반 증분/초(§3.5.3 p.3-37 · 부록 B.2 Case 1 t = 2·A/V) — 모션 대기 상한이 실제 주행시간을 덮는다.
   SY-01B 는 종전 해석 그대로(동작 불변).
② 배치 흡입 구간·정비 이동도 같은 상한 파생을 쓴다(40초 고정이면 저속 이동이 정상 주행 중 실패).
③ 무재시작 설정 적용에서 튠이 없으면 그 기종 **제조사 기본값**(부팅 조립과 같은 규칙) — 표 상한(V6000)이 아니다.
"""

from __future__ import annotations

from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.app.bootstrap import derive_hot_settings
from senlyt_pi.core.pump_guard import PUMP_TUNING_DEFAULTS, SyringeSpec


class _NullSerial:
    in_waiting = 0

    def write(self, data: bytes) -> int:
        return len(data)

    def read(self, size: int = 1) -> bytes:
        return b""

    def close(self) -> None:
        pass


def _tecan() -> TecanXCaliburEngineAdapter:
    return TecanXCaliburEngineAdapter(serial_factory=lambda *_a: _NullSerial())


def _sy01b() -> Sy01bEngineAdapter:
    return Sy01bEngineAdapter(serial_factory=lambda *_a: _NullSerial())


def test_tecan_deadline_covers_manual_travel_time_at_low_speed() -> None:
    a = _tecan()
    # 풀스트로크 3000 증분 @100Hz — 매뉴얼 주행시간 2·3000/100 = 60초. 상한은 그 1.5배 + 5 = 95초.
    assert a._motion_deadline_s(3000, 100) >= 60.0
    assert a._motion_deadline_s(3000, 100) == 3000 * 2 / 100 * 1.5 + 5.0
    # 빠른 모션은 종전 하한(40초) 그대로 — 하방 0.
    assert a._motion_deadline_s(3000, 1400) == a.motion_timeout_s


def test_sy01b_deadline_unchanged() -> None:
    a = _sy01b()
    assert a._motion_deadline_s(12000, 100) == max(a.motion_timeout_s, 12000 / 100 * 1.5 + 5.0)


def test_hot_apply_without_tuning_falls_back_to_manufacturer_defaults() -> None:
    spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)
    hs = derive_hot_settings({}, pump_map={1: spec}, pump_model="tecan_xcalibur", mode="fragrance")
    d = PUMP_TUNING_DEFAULTS["tecan_xcalibur"]
    assert hs.engine_preset is not None
    assert hs.engine_preset.pump_max_top_speed_hz == d["pumpMaxTopSpeedHz"] == 1400
    assert hs.engine_preset.pump_max_slope == d["pumpMaxSlope"]


def test_maintenance_plunger_moves_use_derived_deadline(monkeypatch) -> None:
    """관제 정비 「끝까지」·「홈으로」 — 저속 튠(V100)에서도 풀스트로크 주행시간을 덮는다(실행 검증 P1 · 2026-10-01)."""
    import dataclasses  # noqa: PLC0415

    from senlyt_pi.ports.engine_port import EngineOpCommand  # noqa: PLC0415

    a = _tecan()
    a.preset = dataclasses.replace(a.preset, pump_max_top_speed_hz=100)
    seen: list[tuple[str, float]] = []

    def fake_settle(addr, cmd, timeout_s, poll=False, **_kw):  # noqa: ANN001
        seen.append((cmd, timeout_s))
        return 0

    monkeypatch.setattr(a, "_settle", fake_settle)
    monkeypatch.setattr(a, "_ensure_ready", lambda *_a, **_k: 0, raising=False)
    monkeypatch.setattr(a, "_axis_guard", lambda *_a: None, raising=False)
    spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)
    for op, target in (("plunger_full", "A3000R"), ("plunger_home", "A0R")):
        seen.clear()
        a.run_op(EngineOpCommand(pump_addr=1, op=op, spec=spec))
        moves = [t for c, t in seen if c.endswith(target)]
        assert moves, (op, seen)
        # 3000 증분 @100Hz = 매뉴얼 60초 → 상한 95초(종전 40초 고정).
        assert moves[0] == 3000 * 2 / 100 * 1.5 + 5.0, (op, moves)


def test_batch_aspirate_multi_segment_uses_each_segment_steps(monkeypatch) -> None:
    """구간 상한은 누적 위치가 아니라 **이번 구간 steps** 로 · 속도 None 은 preset V 로(전송 속도와 같은 축)."""
    from senlyt_pi.ports.engine_port import EngineBatchCommand  # noqa: PLC0415

    a = _tecan()
    seen: list[tuple[str, float]] = []

    def fake_settle(addr, cmd, timeout_s, poll=False, **_kw):  # noqa: ANN001
        seen.append((cmd, timeout_s))
        return 0

    monkeypatch.setattr(a, "_settle", fake_settle)
    monkeypatch.setattr(a, "_ensure_ready", lambda *_a, **_k: 0, raising=False)
    monkeypatch.setattr(a, "_axis_guard", lambda *_a: None, raising=False)
    cmd = EngineBatchCommand(
        pump_addr=1,
        aspirations=((2, 1000, 333.0, 100), (3, 1500, 500.0, None)),
        out_port=11,
        dispense_speed_hz=1400,
        slope=7,
        spec=SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0),
    )
    a.dispense_batch(cmd)
    first = [t for c, t in seen if c.endswith("A1000R")]
    second = [t for c, t in seen if c.endswith("A2500R")]
    assert first == [max(a.motion_timeout_s, 1000 * 2 / 100 * 1.5 + 5.0)]
    v = a.preset.pump_max_top_speed_hz
    assert second == [max(a.motion_timeout_s, 1500 * 2 / v * 1.5 + 5.0)]


def test_hot_apply_without_tuning_sy01b_also_manufacturer_defaults() -> None:
    spec = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=0.5)
    hs = derive_hot_settings({}, pump_map={1: spec}, pump_model="sy01b", mode="fragrance")
    d = PUMP_TUNING_DEFAULTS["sy01b"]
    assert hs.engine_preset is not None
    assert hs.engine_preset.pump_max_top_speed_hz == d["pumpMaxTopSpeedHz"] == 4000
    assert hs.engine_preset.pump_max_slope == d["pumpMaxSlope"]
