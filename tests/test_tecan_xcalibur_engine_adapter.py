"""Tecan XCalibur 어댑터 — 기종별 차이 계약 검증 (시리얼 없이 seam 으로).

이 테스트가 지키는 것 (매뉴얼 = 00_research "Manual Operating Cavro XCalibur 20733085-C.txt"):
  1. **U 절대 금지** — 어떤 경로(_setup·broadcast·polled 초기화)에서도 `U` 프레임이 안 나간다
     (XCalibur 의 U = NVM 설정 기록 · Table 3-5 — 오용 시 밸브타입/보드레이트가 바뀐다)
  2. **N0 고정** — 셋업마다 표준 모드(3000 증분)를 명시한다(펌웨어 기본값 의존 금지)
  3. **상태 폴 = Q** — busy 판정은 [Q]만 유효(§3.6) · `?` 를 폴에 쓰지 않는다
  4. **속도 하한** — v/V/c 가 50 밑으로 내려가지 않는다(범위 밖 = err 3 · §3.5.3)
  5. **스텝 파생 = 3000 축** — 0.5mL 100µL → 600 steps (12000 축이면 2400 = 4배 과다·물리 한계 초과)
  6. **sy01b 기계 상속 불변** — 토출 사이클(I→A→O→A0)·estop TR·에러 전파는 부모와 동일
"""

from __future__ import annotations

import pytest

from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import (
    TECAN_MIN_SPEED_HZ,
    TecanXCaliburEngineAdapter,
)
from senlyt_pi.core.pump_guard import PUMP_PRESETS, SyringeSpec
from senlyt_pi.ports.engine_port import (
    OP_INITIALIZE,
    OP_PLUNGER_FULL,
    EngineDispenseCommand,
    EngineOpCommand,
)

# Tecan 축: 표준 모드(N0) 풀스트로크 3000.
SPEC_05 = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)


def status_frame(error_code: int = 0, *, ready: bool = True) -> bytes:
    """`/0{상태바이트}` + ETX — 펌프 응답 모사(부모 테스트와 동일 프레임 구조)."""
    b = (0x20 if ready else 0x00) | (error_code & 0x0F)
    return b"/0" + bytes([b]) + b"\x03"


class FakeSerial:
    """시리얼 seam — 보낸 프레임을 기록하고, 스크립트된 응답을 돌려준다(부모 테스트 미러)."""

    def __init__(self, responses: list[bytes] | None = None, *, default: bytes | None = None):
        self.written: list[str] = []
        self._responses = list(responses or [])
        self._default = default if default is not None else status_frame(0, ready=True)
        self._buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written.append(data.decode("ascii"))
        self._buf.extend(self._responses.pop(0) if self._responses else self._default)
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        out, self._buf = bytes(self._buf[:size]), bytearray(self._buf[size:])
        return out

    def close(self) -> None:
        self.closed = True


def adapter_with(fake: FakeSerial, **kw) -> TecanXCaliburEngineAdapter:
    return TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake, **kw)


def cmd(**over) -> EngineDispenseCommand:
    base = dict(pump_addr=1, volume_ul=100.0, steps=600, spec=SPEC_05, in_port=3, out_port=11)
    base.update(over)
    return EngineDispenseCommand(**base)  # type: ignore[arg-type]


# ── 1. U 금지 + N0 고정 (기종별 차이 1·2) ────────────────────────────────────────────


class TestNoStallCurrentCommand:
    def test_setup_sends_n0_never_u(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        assert a.dispense(cmd()).raw_error_code == 0
        joined = "".join(fake.written)
        assert "N0R" in joined  # 표준 모드 명시(3000 축 고정)
        # U 프레임이 어디에도 없다 — XCalibur 의 U 는 NVM 설정 기록(오용 금지).
        assert not any("U" in w for w in fake.written), fake.written

    def test_initialize_polled_sends_n0_never_u(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        res = a.initialize_polled([1, 2], SPEC_05, 12, 11)
        assert set(res) == {1, 2}
        assert not any("U" in w for w in fake.written), fake.written
        assert any("N0R" in w for w in fake.written)

    def test_initialize_broadcast_sends_n0_never_u(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        # 브로드캐스트 홈 고정대기(HOME_SETTLE_S=30s)를 테스트에서 기다리지 않도록 폴 경로 대신
        # 시퀀스만 검증 — stop 이벤트로 조기 이탈시키면 발사 프레임 검증이 안 되므로,
        # 여기서는 짧은 스텝 간격 검증 대신 발사 프레임만 확인한다(느린 테스트 회피).
        import senlyt_pi.adapters.sy01b_engine_adapter as sy

        orig_gap, orig_settle = sy.BROADCAST_STEP_GAP_S, sy.HOME_SETTLE_S
        sy.BROADCAST_STEP_GAP_S, sy.HOME_SETTLE_S = 0.01, 0.01
        try:
            res = a.initialize_broadcast([1], SPEC_05, 12, 11)
        finally:
            sy.BROADCAST_STEP_GAP_S, sy.HOME_SETTLE_S = orig_gap, orig_settle
        assert res == {1: 0}
        assert not any("U" in w for w in fake.written), fake.written
        assert any(w.startswith("/_N0R") for w in fake.written)

    def test_run_op_initialize_never_u(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        r = a.run_op(
            EngineOpCommand(
                pump_addr=1, op=OP_INITIALIZE, spec=SPEC_05, init_in_port=12, init_out_port=11
            )
        )
        assert r.raw_error_code == 0
        assert not any("U" in w for w in fake.written), fake.written


# ── 2. 상태 폴 = Q (기종별 차이 3) ──────────────────────────────────────────────────


class TestStatusPollUsesQ:
    def test_health_probe_sends_question_mark_not_q(self):
        # 관찰 전용 프로브는 `?`(위치 리포트) — Q 는 latched 오버로드를 소진한다(§3.6.3
        #   "[Q] clears the error" · 2026-09-01 검증 P1-a 로 Q→? 반전). 판정 폴은 Q 유지.
        fake = FakeSerial()
        a = adapter_with(fake)
        assert a.health_probe(1) == "ok"
        assert fake.written == ["/1?\r"]

    def test_motion_poll_sends_q_not_question_mark(self):
        # 이동 즉답 = busy → 폴 1회(busy) → 폴 2회(ready). 폴 프레임이 Q 여야 한다.
        fake = FakeSerial(
            responses=[
                status_frame(0, ready=True),  # N0R
                status_frame(0, ready=True),  # Z 즉답(ready — 폴 생략돼도 무방)
                status_frame(0, ready=True),  # I{out} 주차
                status_frame(0, ready=True),  # I3 회전
                status_frame(0, ready=False),  # A600 즉답(busy)
                status_frame(0, ready=False),  # 폴 1 — busy
                status_frame(0, ready=True),  # 폴 2 — 완료
                status_frame(0, ready=True),  # O11
                status_frame(0, ready=True),  # A0
            ]
        )
        a = adapter_with(fake)
        assert a.dispense(cmd()).raw_error_code == 0
        polls = [w for w in fake.written if w in ("/1Q\r", "/1?\r")]
        assert polls, "상태 폴이 한 번은 나가야 한다"
        assert all(p == "/1Q\r" for p in polls), fake.written


# ── 3. 속도 하한 (기종별 차이 4) ────────────────────────────────────────────────────


class TestSpeedFloors:
    def test_tiny_requested_speed_floors_to_min(self):
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        s = a._speed_cmd(10, 3)  # 요청 top 10Hz — XCalibur V 하한은 5 지만 교집합 바닥 50 적용
        assert s == f"v{TECAN_MIN_SPEED_HZ}V{TECAN_MIN_SPEED_HZ}c{TECAN_MIN_SPEED_HZ}L3"

    def test_default_speed_within_ranges(self):
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        s = a._speed_cmd(None, None)  # 프리셋 상한 경로
        assert s == "v1000V6000c2700L20"  # v≤1000 · V≤6000 · c≤2700 (§3.5.3 상한)

    def test_monotonic_band_preserved(self):
        # v ≤ c ≤ V (§3.5.4) — top 을 하한으로 끌어올려도 단조성이 깨지지 않는다.
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        import re

        for req in (1, 49, 50, 51, 500, 2699, 2701, 6000, 9999):
            m = re.fullmatch(r"v(\d+)V(\d+)c(\d+)L(\d+)", a._speed_cmd(req, None))
            v, top, c, _ = (int(g) for g in m.groups())
            assert TECAN_MIN_SPEED_HZ <= v <= top
            assert v <= c <= top
            assert top <= 6000 and c <= 2700 and v <= 1000


# ── 4. 스텝 파생 = 3000 축 ───────────────────────────────────────────────────


class TestStrokeDerivation:
    def test_preset_default_is_tecan(self):
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        assert a.preset.pump_preset_id == "tecan_xcalibur"
        assert a.preset.pump_full_stroke == 3000

    def test_steps_for_volume_on_3000_axis(self):
        # 0.5mL 시린지 100µL → 3000×100÷500 = 600 steps (sy01b 12000 축이면 2400 = 물리 한계 초과).
        assert SPEC_05.steps_for_volume_ul(100.0) == 600
        assert SPEC_05.steps_for_volume_ul(500.0) == 3000  # 풀스트로크 상한과 일치

    def test_init_force_derivation_matches_manual_table(self):
        # §3.4.2 Table 3-6: ≥1.0mL=Full(0) · 250/500µL=Half(1) · 50/100µL=Third(2).
        assert SyringeSpec(3000, 0.5).init_command_with(12, 11) == "Z1,12,11R"
        assert SyringeSpec(3000, 1.0).init_command_with(12, 11) == "Z0,12,11R"
        assert SyringeSpec(3000, 0.1).init_command_with(12, 11) == "Z2,12,11R"


# ── 5. sy01b 기계 상속 불변 ──────────────────────────────────────────────────


class TestInheritedMachinery:
    def test_dispense_cycle_order(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        assert a.dispense(cmd(pump_addr=2, in_port=3, out_port=11, steps=600)).raw_error_code == 0
        moves = [w for w in fake.written if any(t in w for t in ("I3", "A600", "O11", "A0R"))]
        for w in moves:
            assert w.startswith("/2") and w.endswith("\r"), w
        seq = [next(t for t in ("I3", "A600", "O11", "A0R") if t in w) for w in moves]
        assert seq == ["I3", "A600", "O11", "A0R"]

    def test_estop_sends_tr_and_latches(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        a.emergency_stop_all([1, 2])
        assert "/1TR\r" in fake.written and "/2TR\r" in fake.written

    def test_error_code_propagates_honestly(self):
        # 이동 즉답 err9(플런저 오버로드) → 거짓 성공 없이 그대로 전파(EP-03).
        fake = FakeSerial(
            responses=[
                status_frame(0, ready=True),  # N0R
                status_frame(0, ready=True),  # Z
                status_frame(0, ready=True),  # 주차
                status_frame(0, ready=True),  # I3
                status_frame(9, ready=False),  # A600 즉답 = 오버로드
            ],
            default=status_frame(9, ready=False),
        )
        a = adapter_with(fake)
        assert a.dispense(cmd()).raw_error_code == 9

    def test_q_is_reconnect_resend_safe(self):
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        assert "Q" in a._resend_safe and "TR" in a._resend_safe

    def test_plunger_full_targets_3000(self):
        fake = FakeSerial()
        a = adapter_with(fake)
        r = a.run_op(EngineOpCommand(pump_addr=1, op=OP_PLUNGER_FULL, spec=SPEC_05, valve_port=12))
        assert r.raw_error_code == 0
        assert any("A3000R" in w for w in fake.written), fake.written


# ── 6. sy01b 회귀 가드 — seam 도입 후에도 sy01b 는 U 를 그대로 보낸다 ─────────


class TestSy01bRegressionGuard:
    def test_sy01b_still_sends_stall_current(self):
        from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter

        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=0.5)
        assert (
            a.dispense(
                EngineDispenseCommand(
                    pump_addr=1, volume_ul=100.0, steps=2400, spec=spec, in_port=3, out_port=2
                )
            ).raw_error_code
            == 0
        )
        joined = "".join(fake.written)
        assert "U200,5R" in joined  # 스톨전류 그대로(기종별 차이 지점(seam) 의 sy01b 기본값 불변)
        assert "N0R" not in joined  # N 은 Tecan 전용
        assert "?" in joined and "Q" not in joined.replace("QR", "")  # 폴은 여전히 `?`
