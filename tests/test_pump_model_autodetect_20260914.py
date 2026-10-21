"""펌프 기종 자동 인식(2026-09-14) — 부팅 감지 → 조립 · 유휴 상시 재관측 → 재기동 정책 · 하트비트 출처.

사용자 요구: "pi 는 Runze 든 Tecan 이든 스스로 인식해 바로 그 기종으로 동작 · admin 에 실제 형태가 보인다 ·
센소리움을 바꿔도 재시작 불필요". 조립의 1순위 근거가 선언 → 실물 지문으로 옮겨진 계약을 잠근다.
"""

from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
from senlyt_pi.adapters.pump_model_detect import DetectResult, detect_pump_model
from senlyt_pi.adapters.serial_port_discovery import SerialPortInfo
from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter, classify_fingerprint
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter
from senlyt_pi.app.bootstrap import build_components, build_resolver
from senlyt_pi.config.server_target import SENLYT_ENV_KEY
from senlyt_pi.core.wire_messages import Heartbeat
from test_sy01b_engine_adapter import FakeSerial, status_frame

_CH340 = [SerialPortInfo(device="/dev/ttyUSB0", vid=0x1A86, pid=0x7523)]
SY01B_SNAP = {
    "pumpPreset": {"pumpPresetId": "sy01b", "pumpFullStroke": 12000, "syringeCapacityMl": 0.5},
    "hardware": {"sensoriumVersion": "sensorium-fragrance-1.0.0", "pumpModel": "sy01b", "valvePortCount": 12},
}
TECAN = "30064809 C"
RUNZE = "8.33"


def _fp(text: str) -> bytes:
    return b"/0`" + text.encode() + b"\x03"


def _store(tmp_path) -> DeviceIdentityStore:
    store = DeviceIdentityStore(tmp_path / "identity.json")
    store.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
    return store


class TestClassify:
    def test_known_and_unknown(self):
        assert classify_fingerprint(TECAN) == "tecan_xcalibur"
        assert classify_fingerprint(RUNZE) == "sy01b"
        assert classify_fingerprint(" 9.01 ") == "sy01b"
        assert classify_fingerprint("30071234 D") == "tecan_xcalibur"
        assert classify_fingerprint("???") is None
        assert classify_fingerprint("") is None
        assert classify_fingerprint(None) is None


class TestDetect:
    """임시 어댑터로 `?`→`&` 만 읽고 닫는다 — 어느 기종에도 모션·NVM 프레임 0."""

    def _run(self, fps: list[str], addrs=(1, 2, 3)):
        responses: list[bytes] = []
        for fp in fps:
            responses += [status_frame(0, ready=True), _fp(fp)]
        fake = FakeSerial(responses=responses)
        det = detect_pump_model("/dev/ttyUSB0", list(addrs), serial_factory=lambda *_a: fake)
        return det, fake

    def test_uniform_runze(self):
        det, fake = self._run([RUNZE, RUNZE, RUNZE])
        assert det.model == "sy01b" and det.responding == (1, 2, 3) and det.source == "detected"
        assert det.strong  # 실측 정확값.
        assert self._run(["9.01", "9.01", "9.01"])[0].strong is False  # 형식만.
        assert det.fingerprints == {1: RUNZE, 2: RUNZE, 3: RUNZE}
        joined = "".join(fake.written)
        assert "U" not in joined and "N0R" not in joined and "Z" not in joined  # read-only 만.
        assert fake.closed  # finally close — 진짜 어댑터와 이중 open 금지.

    def test_uniform_tecan(self):
        det, _ = self._run([TECAN, TECAN, TECAN])
        assert det.model == "tecan_xcalibur"

    def test_mixed_bus_is_not_assembled(self):
        det, _ = self._run([RUNZE, TECAN, RUNZE])
        assert det.model is None and det.mixed and det.source == "mixed"

    def test_unreadable_fingerprint_is_undetected(self):
        det, _ = self._run(["???", RUNZE, RUNZE])
        assert det.model is None and det.unreadable and det.source == "undetected"

    def test_no_responding_pumps(self):
        fake = FakeSerial(default=b"")  # 전부 빈 프레임 = 무응답.
        det = detect_pump_model("/dev/ttyUSB0", [1, 2], serial_factory=lambda *_a: fake)
        assert det.model is None and det.responding == () and det.source == "no_pumps"


class TestBootAssembly:
    """감지 > 선언 > 캐시 > Undeclared — 선언 sy01b 인 스냅샷에 실물 Tecan 이 꽂혀 있으면 Tecan 으로 조립한다."""

    def _components(self, tmp_path, det: "DetectResult | None", *, snap=SY01B_SNAP):
        return build_components(
            {SENLYT_ENV_KEY: "v1_2_0"},
            identity_store=_store(tmp_path),
            register=False,
            fetch_settings=True,
            settings_fetcher=lambda cfg, tok, mode: dict(snap),
            port_lister=lambda: list(_CH340),
            pump_detector=lambda port, addrs: det,
        )

    def test_detected_tecan_beats_declared_sy01b(self, tmp_path):
        det = DetectResult(model="tecan_xcalibur", responding=(1, 2, 3), fingerprints={1: TECAN, 2: TECAN, 3: TECAN})
        comp = self._components(tmp_path, det)
        assert isinstance(comp.engine, TecanXCaliburEngineAdapter)
        assert comp.hardware_source == "detected"
        assert comp.hardware_profile is not None
        assert comp.hardware_profile.source == "detected"
        assert comp.hardware_profile.pump_full_stroke == 3000  # 어댑터와 같은 출처(축 가드 구조적 불발).
        assert comp.hardware_profile.valve_port_count == 12  # 포트 수는 선언 승계.
        assert comp.detected_pump_addrs == (1, 2, 3)
        # pump_map 도 감지 결과로 — 2차 스캔 없이, stroke 는 감지 기종 프리셋(스냅샷 12000 이 아니다).
        r = build_resolver(
            {}, engine=comp.engine, server_settings=comp.server_settings, mode="fragrance",
            hardware_profile=comp.hardware_profile, known_pump_addrs=comp.detected_pump_addrs,
        )
        assert sorted(r.pump_map) == [1, 2, 3]
        assert all(sp.pump_full_stroke == 3000 for sp in r.pump_map.values())

    def test_detected_runze_matches_declaration(self, tmp_path):
        det = DetectResult(model="sy01b", responding=(1, 2, 3), fingerprints={1: RUNZE, 2: RUNZE, 3: RUNZE})
        comp = self._components(tmp_path, det)
        assert isinstance(comp.engine, Sy01bEngineAdapter) and not isinstance(comp.engine, TecanXCaliburEngineAdapter)
        assert comp.hardware_source == "detected"

    def test_unreadable_is_undeclared_not_declaration_fallback(self, tmp_path):
        """검증 P0-1 — 응답은 하는데 지문을 못 읽으면 선언(sy01b) 추측으로 조립하지 않는다(U-NVM 방지)."""
        det = DetectResult(model=None, responding=(1, 2, 3), fingerprints={}, unreadable=True)
        comp = self._components(tmp_path, det)
        assert isinstance(comp.engine, UndeclaredEngineAdapter)
        assert comp.hardware_source == "undetected"
        assert "지문" in comp.engine.initialize().detail

    def test_mixed_is_undeclared(self, tmp_path):
        det = DetectResult(model=None, responding=(1, 2), fingerprints={1: RUNZE, 2: TECAN}, mixed=True)
        comp = self._components(tmp_path, det)
        assert isinstance(comp.engine, UndeclaredEngineAdapter)
        assert comp.hardware_source == "mixed"
        assert "혼합" in comp.engine.initialize().detail

    def test_no_pumps_falls_back_to_declaration(self, tmp_path):
        """응답 0 = 펌프 전원이 늦게 켜진 흔한 경우 — 종전 경로(선언) 그대로. 재발견 재기동이 다시 감지로 데려온다."""
        det = DetectResult(model=None, responding=())
        comp = self._components(tmp_path, det)
        assert isinstance(comp.engine, Sy01bEngineAdapter)
        assert comp.hardware_source == "snapshot"
        assert comp.detected_pump_addrs == ()  # 스캔은 했고 0 — 2차 스캔 생략(늦은 펌프는 재발견 재기동).

    def test_weak_runze_fingerprint_against_tecan_declaration_is_fail_closed(self, tmp_path):
        """검증 P0-1 — 미지 XCalibur 로트가 `&`="3.10" 을 내면 Runze 정규식에 걸린다. 선언이 tecan 인데 형식만으로
        sy01b 를 채택하면 U…R 이 NVM 에 나간다 → Undeclared. 실측 정확값(8.33)이면 선언을 이긴다."""
        tecan_snap = {
            "pumpPreset": {"pumpPresetId": "tecan_xcalibur", "pumpFullStroke": 3000, "syringeCapacityMl": 0.5},
            "hardware": {"sensoriumVersion": "sensorium-fragrance-1.0.0+tecan", "pumpModel": "tecan_xcalibur", "valvePortCount": 12},
        }
        weak = DetectResult(model="sy01b", responding=(1, 2, 3), fingerprints={1: "3.10", 2: "3.10", 3: "3.10"}, strong=False)
        comp = self._components(tmp_path, weak, snap=tecan_snap)
        assert isinstance(comp.engine, UndeclaredEngineAdapter)
        assert comp.hardware_source == "undetected"
        strong = DetectResult(model="sy01b", responding=(1, 2, 3), fingerprints={1: RUNZE, 2: RUNZE, 3: RUNZE}, strong=True)
        comp2 = self._components(tmp_path, strong, snap=tecan_snap)
        assert isinstance(comp2.engine, Sy01bEngineAdapter) and not isinstance(comp2.engine, TecanXCaliburEngineAdapter)
        # 선언 sy01b + Tecan 형식(좁은 정규식)은 채택 — sy01b 조립이 곧 U-NVM 위험이라 Tecan 쪽으로 기우는 게 안전.
        tecan_fmt = DetectResult(model="tecan_xcalibur", responding=(1,), fingerprints={1: "30071234 D"}, strong=False)
        assert isinstance(self._components(tmp_path, tecan_fmt).engine, TecanXCaliburEngineAdapter)
        # 형식만이라도 선언과 같으면 채택(선언 sy01b + "9.01").
        same = DetectResult(model="sy01b", responding=(1,), fingerprints={1: "9.01"}, strong=False)
        assert isinstance(self._components(tmp_path, same).engine, Sy01bEngineAdapter)
        # 선언 없음 + Tecan 좁은 정규식(30071234 D) 은 채택 / 선언 없음 + Runze 형식만은 fail-closed.
        no_decl = {"pumpPreset": {"syringeCapacityMl": 0.5}}
        tw = DetectResult(model="tecan_xcalibur", responding=(1,), fingerprints={1: "30071234 D"}, strong=False)
        assert isinstance(self._components(tmp_path, tw, snap=no_decl).engine, TecanXCaliburEngineAdapter)
        rw_ = DetectResult(model="sy01b", responding=(1,), fingerprints={1: "3.10"}, strong=False)
        assert isinstance(self._components(tmp_path, rw_, snap=no_decl).engine, UndeclaredEngineAdapter)

    def test_detection_skipped_without_real_boot(self, tmp_path):
        """fetch_settings=False(조립 self-test·조립 테스트)는 포트를 열지 않는다 — 감지기 미호출."""
        calls: list[int] = []

        def _det(port, addrs):
            calls.append(1)
            return DetectResult(model="tecan_xcalibur", responding=(1,))

        comp = build_components(
            {SENLYT_ENV_KEY: "v1_2_0"}, identity_store=_store(tmp_path), register=False,
            port_lister=lambda: list(_CH340), pump_detector=_det,
        )
        assert calls == []
        assert isinstance(comp.engine, UndeclaredEngineAdapter)


class TestIdleWatchAndPolicy:
    def _daemon(self, engine, on_changed, watch=(1,)):
        from senlyt_pi.app.daemon import DaemonDeps, SenlytDaemon
        from senlyt_pi.persistence.idempotency_ledger import InMemoryIdempotencyLedger

        class _Sink:
            def send_heartbeat(self, hb):
                pass

            def report_status(self, r):
                pass

        return SenlytDaemon(
            DaemonDeps(
                device_id="dev-A",
                command_source=type("S", (), {"commands": lambda s, i: iter(())})(),
                status_sink=_Sink(),
                engine=engine,  # type: ignore[arg-type]
                ledger=InMemoryIdempotencyLedger(),  # type: ignore[arg-type]
                heartbeat_interval_s=0,
                hw_watch_addrs=watch,
                on_pump_model_changed=on_changed,
                hardware_source="detected",
            )
        )

    def test_model_change_fires_policy_once_after_two_consecutive_observations(self):
        class _Eng:
            MODEL_ID = "sy01b"
            fps = {1: TECAN}

            def health_probe(self, addr):
                return "ok"

            def observe_fingerprint(self, addr):
                return self.fps.get(addr)

            def pump_fingerprints(self):
                return dict(self.fps)

        fired: list[int] = []
        d = self._daemon(_Eng(), lambda: fired.append(1))
        d._refresh_hw_health()
        assert fired == []  # 1회 관측 = 재확인 대기(깨진 프레임 1회로 재기동하지 않는다).
        d._refresh_hw_health()
        assert fired == [1]
        d._refresh_hw_health()
        assert fired == [1]  # 프로세스당 1회 잠금 — 재기동 루프 방지.

    def test_match_resets_streak_and_mixed_never_fires(self):
        class _Eng:
            MODEL_ID = "sy01b"
            fps = {1: TECAN, 2: RUNZE}

            def health_probe(self, addr):
                return "ok"

            def observe_fingerprint(self, addr):
                return self.fps.get(addr)

            def pump_fingerprints(self):
                return dict(self.fps)

        fired: list[int] = []
        eng = _Eng()
        d = self._daemon(eng, lambda: fired.append(1))
        d._refresh_hw_health()
        d._refresh_hw_health()
        assert fired == []  # 혼합은 재기동으로 해결되지 않는다.
        eng.fps = {1: TECAN}
        d._refresh_hw_health()
        eng.fps = {1: RUNZE}  # 자기 기종으로 돌아오면 연속 카운트 리셋.
        d._refresh_hw_health()
        eng.fps = {1: TECAN}
        d._refresh_hw_health()
        assert fired == []

    def test_observe_fingerprint_rearms_setup_gate_on_change(self):
        """전원 채로 펌프만 갈아끼우면 링크 리셋이 없어 `_fp_checked` 가 남는다 — 재관측이 값 변화를 보면 그 주소만 discard."""
        fake = FakeSerial(responses=[_fp(RUNZE), _fp(TECAN), _fp(TECAN)])
        eng = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        assert eng.observe_fingerprint(1) == RUNZE
        eng._fp_checked.add(1)
        assert eng.observe_fingerprint(1) == TECAN
        assert 1 not in eng._fp_checked  # 재무장.
        eng._fp_checked.add(1)
        assert eng.observe_fingerprint(1) == TECAN
        assert 1 in eng._fp_checked  # 같은 값이면 유지.
        # prev None(게이트가 3회 판독 실패로 fail-open 확정한 주소)도 첫 지문에서 재무장(검증 P1-1).
        fake2 = FakeSerial(responses=[_fp(TECAN)])
        eng2 = Sy01bEngineAdapter(serial_factory=lambda *_a: fake2)
        eng2._fp_checked.add(1)
        eng2._fp_fails[1] = 3
        assert eng2.observe_fingerprint(1) == TECAN
        assert 1 not in eng2._fp_checked and 1 not in eng2._fp_fails

    def test_initialize_rearms_model_gate(self):
        """검증 P1-B — 매 제조 stage 0 `initialize()` 가 `_fp_checked` 도 비워 제조 직전 게이트가 `&` 를 다시 읽는다."""
        fake = FakeSerial()
        eng = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        eng._fp_checked.add(1)
        eng._fp_fails[1] = 2
        eng.initialize()
        assert 1 not in eng._fp_checked and 1 not in eng._fp_fails

    def test_probe_retries_ampersand_up_to_three_times(self):
        """검증 P1-A — `&` 잡음 1~2회는 재시도로 흡수(기기 전체 Undeclared 방지). 성공 즉시 중단."""
        fake = FakeSerial(responses=[status_frame(0, ready=True), b"", b"/0`\x03", _fp(RUNZE)])
        eng = Sy01bEngineAdapter(serial_factory=lambda *_a: fake)
        assert eng.probe(1) is True
        assert "".join(fake.written).count("&") == 3
        assert eng.pump_fingerprints() == {1: RUNZE}

    def test_silent_address_fingerprint_is_forgotten(self):
        """검증 P1-2 — 무응답 주소의 낡은 지문은 유휴 감시가 버린다(영구 '혼합' 방지)."""
        class _Eng:
            MODEL_ID = "sy01b"
            fps = {1: TECAN, 2: RUNZE}
            health = {1: "ok", 2: "silent"}

            def health_probe(self, addr):
                return self.health[addr]

            def observe_fingerprint(self, addr):
                return self.fps.get(addr)

            def forget_fingerprint(self, addr):
                self.fps.pop(addr, None)

            def pump_fingerprints(self):
                return dict(self.fps)

        fired: list[int] = []
        eng = _Eng()
        d = self._daemon(eng, lambda: fired.append(1), watch=(1, 2))
        d._refresh_hw_health()
        assert 2 not in eng.fps  # 낡은 Runze 지문 evict → 혼합 아님.
        d._refresh_hw_health()
        assert fired == [1]  # Tecan 만 남아 2회 연속 → 재기동.


class TestHeartbeatSource:
    def test_pump_model_source_emitted_when_present(self):
        assert Heartbeat(device_id="d", queue_depth=0, pump_model_source="detected").to_json()["pumpModelSource"] == "detected"
        assert "pumpModelSource" not in Heartbeat(device_id="d", queue_depth=0).to_json()

    def test_undeclared_engine_wire_name(self):
        from senlyt_pi.app.daemon import engine_wire_name

        assert engine_wire_name(UndeclaredEngineAdapter()) == "undeclared"
