"""테칸 축 계약 — 조립 통합 + fail-closed 축 가드 + byte-parity 벡터 (2026-09-01).

이 파일이 고정하는 계약(검증팀 P0 재발 방지망):
  1. **조립 통합** — 서버 settings(pumpPresetId) → clamp → SyringeSpec 축 → 어댑터 프레임까지
     한 줄로: tecan 설정 + SENLYT_ENGINE=tecan 이면 100µL 가 `A600`(3000축·N0R·U 부재)으로
     나가고, 기본(sy01b)이면 `A2400`(12000축·U200,5R)이 **1바이트 불변**으로 나간다.
  2. **축 가드** — 설정축 ≠ 어댑터축(이중 키 불일치)이면 stroke 소비 연산 3종이 시리얼 송신 0
     으로 -1001(PERMANENT) 거부. 단 **복구 경로(estop·initialize·plunger_home·initialize_polled)
     는 가드 밖** — v1.1.0 벽돌 사고(복구 명령이 막힘) 재발 방지(검증 P0-1 범위 판정).
  3. **byte-parity 벡터** — 아래 PARITY_VECTORS 는 heysenlyt-web
     `__tests__/lib/server/pumpGuard.test.ts` §parity 배열과 **리터럴 동일** 유지(한쪽만 고치면
     리뷰에서 걸리게 상호 경로를 주석으로 못박음).
"""

from __future__ import annotations

from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.app.bootstrap import build_engine, build_resolver
from senlyt_pi.core.pump_guard import (
    AXIS_MISMATCH_RAW_CODE,
    EngineErrorClass,
    SyringeSpec,
    clamp_pump_preset,
    classify_engine_error_code,
)
from senlyt_pi.ports.engine_port import (
    OP_ESTOP,
    OP_INITIALIZE,
    OP_PLUNGER_FULL,
    OP_PLUNGER_HOME,
    EngineBatchCommand,
    EngineDispenseCommand,
    EngineOpCommand,
)


def status_frame(error_code: int = 0, *, ready: bool = True) -> bytes:
    b = (0x20 if ready else 0x00) | (error_code & 0x0F)
    return b"/0" + bytes([b]) + b"\x03"


class FakeSerial:
    def __init__(self):
        self.written: list[str] = []
        self._buf = bytearray()

    def write(self, data: bytes) -> int:
        self.written.append(data.decode("ascii"))
        self._buf.extend(status_frame(0, ready=True))
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        out, self._buf = bytes(self._buf[:size]), bytearray(self._buf[size:])
        return out

    def close(self) -> None:
        pass


TECAN_SETTINGS = {"pumpPreset": {"pumpPresetId": "tecan_xcalibur", "syringeCapacityMl": 0.5}}
SY01B_SETTINGS = {"pumpPreset": {"pumpPresetId": "sy01b", "syringeCapacityMl": 0.5}}


def _dispense_cmd(spec: SyringeSpec, volume_ul: float) -> EngineDispenseCommand:
    return EngineDispenseCommand(
        pump_addr=1, volume_ul=volume_ul, steps=spec.steps_for_volume_ul(volume_ul),
        spec=spec, in_port=3, out_port=11,
    )


# ── 1. 조립 통합 — settings → resolver 축 → 어댑터 프레임 ─────────────────────


class TestAssembledAxis:
    def test_tecan_settings_and_env_produce_3000_axis_frames(self):
        env = {"SENLYT_ENGINE": "tecan", "PUMP_ADDRESSES": "fragrance:1,2,3"}
        engine = build_engine(env)
        assert isinstance(engine, TecanXCaliburEngineAdapter)
        resolver = build_resolver(env, engine=engine, server_settings=TECAN_SETTINGS)
        spec = resolver.pump_map[1]
        assert spec.pump_full_stroke == 3000  # 설정축이 clamp 를 관통(전량폐기 P0 봉합).
        assert spec.pump_full_stroke == engine.preset.pump_full_stroke  # 이중 키 일치.
        fake = FakeSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        assert a.dispense(_dispense_cmd(spec, 100.0)).raw_error_code == 0
        joined = "".join(fake.written)
        assert "A600R" in joined  # 3000×100÷500 = 600 (12000축이면 2400 = 4배).
        assert "N0R" in joined and not any("U" in w for w in fake.written)

    def test_default_assembly_remains_sy01b_12000_byte_identical(self):
        env = {"PUMP_ADDRESSES": "flavor:1,2"}  # SENLYT_ENGINE 미지정 · 서버 settings 없음.
        resolver = build_resolver(env, server_settings=None)
        spec = resolver.pump_map[1]
        assert spec.pump_full_stroke == 12000
        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        assert a.dispense(_dispense_cmd(spec, 100.0)).raw_error_code == 0
        joined = "".join(fake.written)
        assert "A2400R" in joined and "U200,5R" in joined  # 기존 동작 1바이트 불변.

    def test_settings_fetch_failure_falls_back_to_sy01b_axis(self):
        # 상태표 케이스 5 — 스냅샷 실패 시 설정축은 sy01b 폴백(어댑터가 tecan 이면 가드가 거부).
        env = {"SENLYT_ENGINE": "tecan", "PUMP_ADDRESSES": "fragrance:1,2,3"}
        resolver = build_resolver(env, server_settings=None)
        assert resolver.pump_map[1].pump_full_stroke == 12000


# ── 2. fail-closed 축 가드 — stroke 소비 3종 거부 · 복구 경로 통과 ─────────────


class TestAxisGuard:
    SPEC_12000 = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=0.5)
    SPEC_3000 = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)

    def _mismatched(self):
        """상태표 케이스 4(무성 1/4 토출 방향) — tecan 설정축 spec + sy01b 어댑터."""
        fake = FakeSerial()
        return Sy01bEngineAdapter(serial_factory=lambda *_a: fake), fake, self.SPEC_3000

    def test_dispense_rejected_without_serial(self):
        a, fake, spec = self._mismatched()
        r = a.dispense(_dispense_cmd(spec, 100.0))
        assert r.raw_error_code == AXIS_MISMATCH_RAW_CODE
        assert fake.written == []  # 시리얼 송신 0 — 모션 자체가 없다.
        assert classify_engine_error_code(r.raw_error_code) is EngineErrorClass.PERMANENT

    def test_batch_rejected(self):
        a, fake, spec = self._mismatched()
        cmd = EngineBatchCommand(
            pump_addr=1, out_port=11, dispense_speed_hz=None, slope=None, spec=spec,
            aspirations=((3, 600, 100.0, None),),
        )
        assert a.dispense_batch(cmd).raw_error_code == AXIS_MISMATCH_RAW_CODE
        assert fake.written == []

    def test_plunger_full_rejected_but_home_passes(self):
        a, fake, spec = self._mismatched()
        full = EngineOpCommand(pump_addr=1, op=OP_PLUNGER_FULL, spec=spec, valve_port=12)
        assert a.run_op(full).raw_error_code == AXIS_MISMATCH_RAW_CODE
        assert fake.written == []
        # plunger_home(A0) = stroke 무관 배출·보관 자세 — 축이 틀려도 안전·통과해야 한다.
        home = EngineOpCommand(pump_addr=1, op=OP_PLUNGER_HOME, spec=spec, valve_port=11)
        assert a.run_op(home).raw_error_code == 0
        assert any("A0R" in w for w in fake.written)

    def test_recovery_paths_not_guarded(self):
        # 복구 경로(estop·initialize·initialize_polled)는 stroke 를 안 쓰므로 가드 밖 —
        #   막으면 v1.1.0 벽돌 사고(복구 명령이 에러 때문에 실패)를 축으로 재현한다(P0-1).
        a, fake, spec = self._mismatched()
        assert a.run_op(EngineOpCommand(pump_addr=1, op=OP_ESTOP, spec=spec)).raw_error_code == 0
        assert a.run_op(
            EngineOpCommand(pump_addr=1, op=OP_INITIALIZE, spec=spec,
                            init_in_port=12, init_out_port=11)
        ).raw_error_code == 0
        res = a.initialize_polled([1], spec, 12, 11)
        assert res == {1: 0}

    def test_matched_axis_passes(self):
        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        assert a.dispense(_dispense_cmd(self.SPEC_12000, 100.0)).raw_error_code == 0


# ── 3. byte-parity 벡터 — web pumpGuard.test.ts §parity 와 리터럴 동일 유지 ───
#   ⚠️ 이 배열은 heysenlyt-web `__tests__/lib/server/pumpGuard.test.ts` 의 PARITY_VECTORS 와
#   **리터럴 동일**해야 한다(형식: [presetIdInput, capacityMl, volumeUl, 기대 stroke, 기대 steps]).
#   한쪽만 고치면 안 된다 — 두 파일 상호 참조 주석이 리뷰 그물이다.
PARITY_VECTORS = [
    ("sy01b", 0.5, 100.0, 12000, 2400),
    ("sy01b", 0.5, 125.0, 12000, 3000),
    ("sy01b", 1.25, 500.0, 12000, 4800),
    ("tecan_xcalibur", 0.5, 100.0, 3000, 600),
    ("tecan_xcalibur", 0.5, 125.0, 3000, 750),
    ("tecan_xcalibur", 0.5, 500.0, 3000, 3000),
    ("tecan_xcalibur", 1.25, 500.0, 3000, 1200),
    ("unknown-id", 0.5, 100.0, 12000, 2400),
    # 타입 강제(비문자열·대문자) — 양 언어 정규화 없이 sy01b 폴백이어야 한다(리뷰 갭).
    #   (py True ↔ ts true · 언어 리터럴만 다르고 값은 동일 축)
    (123, 0.5, 100.0, 12000, 2400),
    (True, 0.5, 100.0, 12000, 2400),
    ("TECAN_XCALIBUR", 0.5, 100.0, 12000, 2400),
    ("custom", 0.5, 100.0, 12000, 2400),
    (None, 0.5, 100.0, 12000, 2400),
]


class TestByteParityVectors:
    def test_vectors(self):
        for preset_id, cap, vol, want_stroke, want_steps in PARITY_VECTORS:
            cfg = None if preset_id is None else {"pumpPresetId": preset_id}
            preset = clamp_pump_preset(cfg)
            assert preset.pump_full_stroke == want_stroke, (preset_id, cap, vol)
            spec = SyringeSpec(pump_full_stroke=preset.pump_full_stroke, syringe_capacity_ml=cap)
            assert spec.steps_for_volume_ul(vol) == want_steps, (preset_id, cap, vol)


# ── 4. 부팅 축 자가진단 + settings 재시도 (검증 P1-B·P1-2) ────────────────────


class TestBootAxisDiagnosis:
    @staticmethod
    def _identity_store(tmp_path):
        from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore

        store = DeviceIdentityStore(tmp_path / "identity.json")
        store.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
        return store

    def _boot(self, tmp_path, *, engine, fetcher):
        from senlyt_pi.app.bootstrap import build_components
        from senlyt_pi.config.server_target import SENLYT_ENV_KEY
        from senlyt_pi.obs.log import StructuredLogger

        records: list[dict] = []
        logger = StructuredLogger(service="test", sink=records.append)
        env = {SENLYT_ENV_KEY: "v1_1_0", "SENLYT_ENGINE": "tecan"}
        build_components(
            env, engine=engine, logger=logger,
            identity_store=self._identity_store(tmp_path), register=False,
            fetch_settings=True, settings_fetcher=fetcher,
        )
        return records

    def test_mismatch_warns_with_both_axes_in_message(self, tmp_path):
        # tecan 어댑터 + settings 부재(폴백 12000) = 축 불일치 — WARN 에 두 축 숫자가 남아야
        #   한다(스냅샷 실패 tecan 기기의 유일한 조기 경보 — 검증 P1-B).
        calls = {"n": 0}

        def none_fetcher(*_a):
            calls["n"] += 1
            return None

        engine = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        records = self._boot(tmp_path, engine=engine, fetcher=none_fetcher)
        warns = [r for r in records if "축 불일치" in str(r.get("message", ""))]
        assert warns, records
        assert warns[0]["detail"]["settingsStroke"] == 12000
        assert warns[0]["detail"]["adapterStroke"] == 3000
        # R3 P2-②: 수치는 **메시지 본문**에도 있어야 한다 — 서버 trace allowlist 는 detail 을
        #   admin 도달 전에 폐기하므로, 본문 인라인이 운영자에게 실제 도달하는 유일한 경로다.
        msg = str(warns[0]["message"])
        assert "12000" in msg and "3000" in msg, msg
        # settings 재시도(P1-2) — tecan 은 3회 시도(순단 흡수).
        assert calls["n"] == 3

    def test_matched_axis_no_warn_and_single_fetch_for_success(self, tmp_path):
        def tecan_fetcher(*_a):
            return TECAN_SETTINGS

        engine = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: FakeSerial())
        records = self._boot(tmp_path, engine=engine, fetcher=tecan_fetcher)
        assert not [r for r in records if "축 불일치" in str(r.get("message", ""))]


# ── 5. 불일치 복구 경로에서 기종 전용 셋업 프레임 봉인 (검증 P1-3) ─────────────


class TestMismatchSuppressesModelSpecificFrames:
    def test_initialize_on_mismatched_adapter_homes_without_u(self):
        # 설정 tecan(3000축 spec) + sy01b 어댑터 — 복구(initialize)는 통과하되(벽돌 방지)
        #   XCalibur 실물에 위험한 `U…`(NVM 기록) 프레임은 봉인돼야 한다.
        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        spec3000 = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        r = a.run_op(
            EngineOpCommand(pump_addr=1, op=OP_INITIALIZE, spec=spec3000,
                            init_in_port=12, init_out_port=11)
        )
        assert r.raw_error_code == 0  # 복구는 산다.
        assert any("Z" in w for w in fake.written)  # 홈은 나간다.
        assert not any("U" in w for w in fake.written), fake.written  # U 봉인.

    def test_matched_sy01b_still_sends_u(self):
        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        spec12000 = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=0.5)
        a.run_op(EngineOpCommand(pump_addr=1, op=OP_INITIALIZE, spec=spec12000,
                                 init_in_port=12, init_out_port=11))
        assert any("U200,5R" in w for w in fake.written)  # 정상 축 = 기존 바이트 불변.


# ── 6. XCalibur 셋업 마감 — N0R 검증 송신 + readback 관측 (검증 P0-2) ──────────


class TestTecanFinalizeSetup:
    def test_initialize_polled_sends_verified_n0_and_readbacks_before_cache(self):
        fake = FakeSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        res = a.initialize_polled([1], spec, 12, 11)
        assert res == {1: 0}
        joined = "".join(fake.written)
        # 캐시 등록 직전 마감: 검증 N0R(발사분과 별개 1발 더) + 관측 readback 2종.
        assert joined.count("N0R") >= 2
        assert "?76" in joined and "/1&\r" in fake.written
        # 실제 제조 경로 재현: 캐시 등록 후 dispense 는 _ensure_ready 를 스킵해도 3000축.
        assert a.dispense(_dispense_cmd(spec, 100.0)).raw_error_code == 0
        assert "A600R" in "".join(fake.written)

    def test_finalize_failure_blocks_cache_registration(self):
        # N0R 검증 송신이 에러(err3)면 그 펌프는 캐시에 등록되지 않는다(정직한 실패).
        class N0FailSerial(FakeSerial):
            def write(self, data: bytes) -> int:
                frame = data.decode("ascii")
                self.written.append(frame)
                err = 3 if "N0R" in frame else 0
                b = (0x20) | (err & 0x0F)
                self._buf.extend(b"/0" + bytes([b]) + b"\x03")
                return len(data)

        fake = N0FailSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        res = a.initialize_polled([1], spec, 12, 11)
        assert res[1] == 3  # 마감 실패가 그대로 보고된다.
        assert 1 not in a._initialized  # 캐시 미등록 — 다음 시도 재셋업.

    def test_n0_lost_frame_is_honest_failure_not_silent_success(self):
        # R3 P0-A 그물: N0R 만 유실(무응답)되고 폴(Q)은 idle 을 보고하는 링크 — N0 은 모션이
        #   없어 폴이 "적용됨/유실됨"을 구분하지 못하므로, ack_tolerant 관대함은 곧 무성 1/8
        #   토출의 거짓 성공이 된다. 마감은 실패해야 하고 캐시 등록도 없어야 한다.
        #   (이 테스트가 `_settle(..., ack_tolerant=True)` 재도입 변이를 죽인다.)
        class N0LostSerial(FakeSerial):
            def write(self, data: bytes) -> int:
                frame = data.decode("ascii")
                self.written.append(frame)
                if "N0R" in frame:
                    return len(data)  # 응답 없음 — 프레임이 링크에서 사라진 상황.
                self._buf.extend(b"/0\x20\x03")  # 그 외 전부 idle·err0 정상 ACK.
                return len(data)

        fake = N0LostSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        res = a.initialize_polled([1], spec, 12, 11)
        assert res[1] != 0, res  # 거짓 성공 금지 — 유실은 실패로 남는다.
        assert 1 not in a._initialized

    def test_n0_busy_nak_is_resent_and_recovers(self):
        # R3 P2-①: 마감 N0R 이 err15(busy NAK)로 한 번 튕겨도 poll 경로가 재전송해 회복해야
        #   한다 — `_settle` 의 poll 인자를 지우는 변이(즉답 단판정)는 여기서 {1:15} 로 죽는다.
        class N0BusyOnceSerial(FakeSerial):
            def __init__(self):
                super().__init__()
                self._n0_seen = 0

            def write(self, data: bytes) -> int:
                frame = data.decode("ascii")
                self.written.append(frame)
                err = 0
                if "N0R" in frame:
                    self._n0_seen += 1
                    # 발사분(phase 1)은 통과시키고, 마감 검증 1발째만 busy NAK.
                    if self._n0_seen == 2:
                        err = 15
                b = (0x20) | (err & 0x0F)
                self._buf.extend(b"/0" + bytes([b]) + b"\x03")
                return len(data)

        fake = N0BusyOnceSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        res = a.initialize_polled([1], spec, 12, 11)
        assert res == {1: 0}, res  # busy 1회는 재전송으로 회복.
        assert 1 in a._initialized
        assert "".join(fake.written).count("N0R") >= 3  # 발사 + 검증 + 재전송.

    def test_finalize_sealed_on_axis_mismatch_no_model_frames(self):
        # R3 P2-1·P2-2: env tecan + 설정 sy01b(12000축 spec) — 복구(홈)는 살되 기종 전용
        #   프레임(N0R·?76·&)은 pre-init·finalize 양쪽 모두에서 봉인돼야 한다.
        fake = FakeSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec12000 = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=0.5)
        res = a.initialize_polled([1], spec12000, 12, 11)
        assert res == {1: 0}  # 복구는 산다(벽돌 방지).
        joined = "".join(fake.written)
        assert any("Z" in w for w in fake.written)  # 홈은 나간다.
        assert "N0R" not in joined and "?76" not in joined and "&" not in joined, fake.written
        # 토출은 축 가드가 거부(-1001) — 봉인 통과(0)가 오토출로 이어지지 않는다.
        assert a.dispense(_dispense_cmd(spec12000, 100.0)).raw_error_code == -1001

    def test_readback_observed_once_per_address(self):
        # R3 P2-3: ?76/& 관측은 주소당 1회 — 캐시 무효 후 재셋업에도 다시 붙지 않는다.
        fake = FakeSerial()
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake)
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=0.5)
        a.initialize_polled([1], spec, 12, 11)
        a._initialized.discard(1)  # 오버로드 복구 등으로 캐시 무효화된 상황 재현.
        a.initialize_polled([1], spec, 12, 11)
        joined = "".join(fake.written)
        assert joined.count("?76") == 1, joined
        # 검증 N0R 은 매 셋업 유지(readback 게이트와 독립) — 2회 셋업이면 발사+검증 각 2회.
        assert joined.count("N0R") >= 4


# ── 8. probe `&` 관측 채집 게이트 — sy01b 기본 경로 1바이트 불변 (R3 P2-4) ──────


class TestProbeFirmwareCaptureGate:
    def test_sy01b_probe_sends_no_ampersand(self):
        fake = FakeSerial()
        a = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        assert a.probe(1) is True
        assert not any("&" in w for w in fake.written), fake.written  # 미실측 프레임 0.

    def test_tecan_probe_captures_ampersand_once(self):
        fake = FakeSerial()
        # 채집은 로그가 목적이라 로거가 있어야 발동한다(무로거=무채집·무해).
        from senlyt_pi.obs.log import StructuredLogger

        logger = StructuredLogger(service="test", sink=lambda _r: None)
        a = TecanXCaliburEngineAdapter(serial_factory=lambda *_a: fake, logger=logger)
        assert a.probe(1) is True
        assert a.probe(1) is True  # 재프로브에도 채집은 1회.
        assert "".join(fake.written).count("&") == 1, fake.written


# ── 7. 크로스레포 parity 자동 대조 (검증 P1-C — 모노레포 컨텍스트 한정) ────────


class TestCrossRepoParity:
    def test_web_parity_vectors_literal_identical(self):
        """형제 워크트리의 web pumpGuard.test.ts 에서 PARITY_VECTORS 를 파싱해 값 대조.

        OneContext 워크트리에선 두 레포가 나란히 체크아웃되므로 여기서 자동 대조가 성립한다
        — "pi 표와 pi 벡터를 함께 고치면 양쪽 CI 가 다 초록인 채 갈라진다"(검증 P1-C)의
        모노레포 그물. pi 단독 CI(형제 부재)에선 skip — 그 경우의 강제력은 여전히 사람 리뷰
        + 이 테스트가 도는 OneContext 검증 라운드다(HTML 보고서 잔여 항목).
        """
        import json
        import re
        from pathlib import Path

        web_test = (
            Path(__file__).resolve().parents[2]
            / "heysenlyt-web" / "__tests__" / "lib" / "server" / "pumpGuard.test.ts"
        )
        if not web_test.exists():
            import pytest

            pytest.skip("형제 heysenlyt-web 미체크아웃 — 모노레포 컨텍스트에서만 대조")
        src = web_test.read_text(encoding="utf-8")
        m = re.search(r"PARITY_VECTORS[^=]*=\s*\[(.*?)\n\];", src, re.S)
        assert m, "web PARITY_VECTORS 블록을 찾지 못함 — 상호 참조 주석 확인"
        rows = re.findall(r"^\t\[(.*?)\],\s*$", m.group(1), re.M)
        web_vectors = []
        for row in rows:
            # TS 리터럴 → JSON 등가(true/null 는 JSON 과 동일 표기 · 문자열은 쌍따옴표).
            web_vectors.append(tuple(json.loads(f"[{row}]")))

        # web 쪽에서 벡터가 "선언만 되고 단언에 안 쓰이는" 퇴화 방지(R3 P3-2·P2-③) — 개수
        #   세기는 주석 언급으로도 차므로(vacuous), **사용 구문**(for…of / forEach)을 직접 본다.
        assert re.search(r"of PARITY_VECTORS|PARITY_VECTORS\s*\.\s*forEach", src), (
            "web PARITY_VECTORS 가 단언 루프에 사용되지 않음"
        )

        def norm(v):
            # 타입 동반 대조(R3 P3-1): 파이썬 `==` 는 True==1·100==100.0 이라 타입 강제 행이
            #   무력화된다 → 원소별 json.dumps 리터럴("true"≠"1"·"100"≠"100.0")로 비교한다.
            return tuple(json.dumps(x) for x in v)

        py_vectors = [tuple(json.loads(json.dumps(v))) for v in PARITY_VECTORS]
        assert len(web_vectors) == len(py_vectors), (len(web_vectors), len(py_vectors))
        for i, (w, p) in enumerate(zip(web_vectors, py_vectors)):
            assert norm(w) == norm(p), f"행 {i} 불일치: web={w} pi={p}"
