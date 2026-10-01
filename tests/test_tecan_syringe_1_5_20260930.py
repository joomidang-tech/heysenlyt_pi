"""Tecan XCalibur 시린지 1mL·5mL 지원(2026-09-30) — 매뉴얼(Cavro XCalibur 20733085-C) 근거 계약 잠금.

① 기종별 지원 목록(서버 pumpGuard 와 같은 값) ② 목록 밖 용량 = 모든 모션 거부(사유에 기대값·실제값)
③ 오프라인 부팅(감지 경로) — 캐시 용량·계약·튠 승계 + 캐시 용량이면 가드 ON ④ 확정 프레임만 캐시
⑤ 누적 스텝 반올림 — 합 = round(누적 µL) ≤ 풀스트로크 · 풀스트로크 초과는 실행 전 거부
⑥ 로그 필드 — 설정 변경 전→후 · 제조 시작/종료 요약 · 용량 거부 기대/실제 · 부팅 자가진단.
"""

from __future__ import annotations

import os

import pytest

from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
from senlyt_pi.adapters.pump_model_detect import DetectResult
from senlyt_pi.adapters.serial_port_discovery import SerialPortInfo
from senlyt_pi.adapters.settings_source import snapshot_settings_confirmed
from senlyt_pi.adapters.settings_watcher import SettingsWatcher, summarize_settings_change
from senlyt_pi.app import bootstrap as bootstrap_mod
from senlyt_pi.app.bootstrap import build_components, build_resolver
from senlyt_pi.app.dispatcher import Dispatcher
from senlyt_pi.config.server_target import SENLYT_ENV_KEY, ServerConfig
from senlyt_pi.core.pump_guard import (
    SUPPORTED_SYRINGE_CAPACITIES_ML,
    SyringeSpec,
    is_supported_syringe_capacity,
)
from senlyt_pi.core.wire_messages import RecipeStep
from senlyt_pi.persistence.hardware_profile_cache import HardwareProfile, load_profile
from senlyt_pi.pipeline.recipe_resolver import RecipeResolver, RecipeValidationError
from senlyt_pi.pipeline.pump_sequencer import plan_error_ul, recipe_plan

TECAN = "30064809 C"
_CH340 = [SerialPortInfo(device="/dev/ttyUSB0", vid=0x1A86, pid=0x7523)]
URL_ENV = {SENLYT_ENV_KEY: "v1_2_0"}


def _snap(cap, *, status="confirmed", h="aaaaaaaaaaaaaaaa", tune=(900, 1400, 900, 7), addrs=(1, 2, 3, 4)):
    v, V, c, L = tune
    return {
        "pumpPreset": {
            "pumpPresetId": "tecan_xcalibur", "pumpFullStroke": 3000, "syringeCapacityMl": cap,
            "pumpMaxStartSpeedHz": v, "pumpMaxTopSpeedHz": V, "pumpMaxCutoffSpeedHz": c, "pumpMaxSlope": L,
        },
        "pumpPorts": {str(a): {} for a in addrs},
        "settingsHash": h,
        "hardware": {
            "sensoriumVersion": "sensorium-icad-0.1.0+tecan", "contractId": "sensorium-icad-0.1.0+tecan",
            "pumpModel": "tecan_xcalibur", "valvePortCount": 12, "pumps": 4, "settingsStatus": status,
        },
    }


class _Probe:
    def probe(self, addr):  # noqa: D401 — 가짜 엔진(모든 주소 응답)
        return True


class TestSupportedList:
    def test_same_values_as_server(self):
        # 서버 pumpGuard.ts SYRINGE_CAPACITY_OPTIONS_BY_MODEL 과 같은 값(웹 테스트가 반대편을 잠근다).
        assert SUPPORTED_SYRINGE_CAPACITIES_ML["tecan_xcalibur"] == (1.0, 5.0)
        assert len(SUPPORTED_SYRINGE_CAPACITIES_ML["sy01b"]) == 9
        assert is_supported_syringe_capacity("tecan_xcalibur", 1.0)
        assert not is_supported_syringe_capacity("tecan_xcalibur", 0.5)
        assert not is_supported_syringe_capacity("tecan_xcalibur", None)
        assert is_supported_syringe_capacity(None, 0.5)  # 기종 모름 = 판정 안 함(종전)


class TestUnsupportedBlocksMotion:
    def _resolver(self, cap):
        prof = HardwareProfile(pump_model="tecan_xcalibur", pump_full_stroke=3000, valve_port_count=12, source="detected")
        return build_resolver({}, engine=_Probe(), server_settings=_snap(cap), mode="fragrance",
                              hardware_profile=prof, known_pump_addrs=[1, 2, 3, 4])

    def test_tecan_05_is_blocked_with_expected_and_actual(self):
        r = self._resolver(0.5)
        assert r.capacity_block is not None
        assert "지원 [1.0, 5.0]mL" in r.capacity_block and "실제 0.5mL" in r.capacity_block
        assert r.capacity_source == "snapshot"

    @pytest.mark.parametrize("cap", [1.0, 5.0])
    def test_tecan_1_and_5_pass(self, cap):
        r = self._resolver(cap)
        assert r.capacity_block is None
        assert all(sp.syringe_capacity_ml == cap and sp.pump_full_stroke == 3000 for sp in r.pump_map.values())

    def test_dispatcher_rejects_every_command_and_logs_fields(self):
        logs = []

        class L:
            def warn(self, msg, **kw):
                logs.append((msg, kw))

        r = self._resolver(0.5)
        d = Dispatcher(device_id="d", command_source=None, sequencer=None, interpret=lambda c: [],
                       logger=L(), capacity_block=r.capacity_block, capacity_source=r.capacity_source)
        assert d._capacity_mismatch(None) == r.capacity_block  # 선언 없는 봉투도 거부
        assert d._cap_detail["reason"] == "syringe_unsupported"
        assert d._cap_detail["capacitySource"] == "snapshot"

    def test_capacity_mismatch_detail_has_expected_actual(self):
        spec = SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)
        d = Dispatcher(device_id="d", command_source=None, sequencer=None, interpret=lambda c: [],
                       pump_map={1: spec}, capacity_source="snapshot")
        assert d._capacity_mismatch(5.0) is not None
        assert d._cap_detail == {"reason": "capacity_mismatch", "expectedMl": 5.0, "actualMl": 1.0,
                                 "pumpAddr": 1, "capacitySource": "snapshot"}


class TestOfflineBoot:
    def _store(self, tmp_path):
        s = DeviceIdentityStore(tmp_path / "identity.json")
        s.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
        return s

    def test_detection_branch_carries_cache_capacity_and_guard_is_on(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bootstrap_mod.time, "sleep", lambda s: None)
        state = tmp_path / "state"
        cfg_url = None

        # 1) 온라인 부팅으로 캐시를 남긴다(확정 프레임 · 5mL)
        det = DetectResult(model="tecan_xcalibur", responding=(1, 2, 3, 4),
                           fingerprints={1: TECAN, 2: TECAN, 3: TECAN, 4: TECAN})
        env = {**URL_ENV, "SENLYT_STATE_DIR": str(state)}
        comp = build_components(env, identity_store=self._store(tmp_path), register=False, fetch_settings=True,
                                settings_fetcher=lambda c, t, m: _snap(5.0, tune=(900, 1000, 900, 7)),
                                port_lister=lambda: list(_CH340), pump_detector=lambda p, a: det)
        cfg_url = comp.server_config.base_url
        cached = load_profile(state, cfg_url)
        assert cached is not None and cached.syringe_capacity_ml == 5.0

        # 2) 오프라인 부팅(스냅샷 없음) — 감지 경로가 캐시 용량·계약·튠을 이어받는다
        comp2 = build_components(env, identity_store=self._store(tmp_path), register=False, fetch_settings=True,
                                 settings_fetcher=lambda c, t, m: None,
                                 port_lister=lambda: list(_CH340), pump_detector=lambda p, a: det)
        hp = comp2.hardware_profile
        assert hp.source == "detected"
        assert hp.syringe_capacity_ml == 5.0
        assert hp.contract_id == "sensorium-icad-0.1.0+tecan"
        assert hp.pump_tuning is not None and hp.pump_tuning["pumpMaxTopSpeedHz"] == 1000
        r = build_resolver({}, engine=_Probe(), server_settings=None, mode="fragrance",
                           hardware_profile=hp, known_pump_addrs=[1, 2, 3, 4])
        assert r.capacity_source == "cache"
        assert r.capacity_from_settings is True  # 캐시 용량이면 가드 ON(2026-09-30)
        assert all(sp.syringe_capacity_ml == 5.0 for sp in r.pump_map.values())
        assert r.capacity_block is None

    def test_tecan_without_any_capacity_is_blocked(self):
        prof = HardwareProfile(pump_model="tecan_xcalibur", pump_full_stroke=3000, valve_port_count=12, source="detected")
        r = build_resolver({}, engine=_Probe(), server_settings=None, mode="fragrance",
                           hardware_profile=prof, known_pump_addrs=[1])
        assert r.capacity_source == "default"
        assert r.capacity_block is not None and "미확인" in r.capacity_block

    def test_sy01b_default_capacity_is_not_blocked(self):
        # 리뷰 P1(2026-09-30) — SY-01B 는 모드 기본 추정(0.5)이 지원 목록 안이라 용량을 저장하지 않아도 돈다(web 과 일치).
        prof = HardwareProfile(pump_model="sy01b", pump_full_stroke=12000, valve_port_count=12, source="detected")
        r = build_resolver({}, engine=_Probe(), server_settings=None, mode="fragrance",
                           hardware_profile=prof, known_pump_addrs=[1, 2, 3])
        assert r.capacity_source == "default"
        assert r.capacity_block is None

    def test_detected_model_change_drops_cached_capacity(self):
        # 리뷰 P2-2 — 캐시(Tecan 1.0)와 다른 기종(SY-01B)이 감지되면 캐시 용량을 이어받지 않는다(랙 교체).
        from senlyt_pi.app import bootstrap as bs
        src = open(bs.__file__, encoding="utf-8").read()
        assert "hardware_profile.pump_model == _det.model\n                            else None\n                        ),\n                        pump_tuning" in src.replace("\r", "")


class TestConfirmedOnlyCache:
    @pytest.mark.parametrize("status,want", [("confirmed", True), ("missing", False), ("stale", False),
                                             ("lookup-failed", False), ("test-device", True)])
    def test_status(self, status, want):
        assert snapshot_settings_confirmed(_snap(1.0, status=status)) is want

    def test_old_server_frame_is_cached(self):
        s = _snap(1.0)
        del s["hardware"]["settingsStatus"]
        assert snapshot_settings_confirmed(s) is True


def _batch(cap, vols):
    return [RecipeStep.from_json({
        "kind": "batchSyringe", "idx": 0, "stage": 0, "pumpAddr": 1, "outPort": 11,
        "dispenseSpeedHz": 3000, "slope": 7,
        "aspirations": [{"flavor": f"l{i}", "inPort": 2 + i, "volume": v, "aspirateSpeedHz": 2000}
                        for i, v in enumerate(vols)],
    })]


class TestCumulativeSteps:
    @pytest.mark.parametrize("cap", [1.0, 5.0])
    def test_adversarial_fractional_steps_stay_within_stroke(self, cap):
        u = cap * 1000 / 3000
        vols = [1000.6 * u, 1000.6 * u, 998.5 * u]  # 흡입마다 반올림하면 3001
        rr = RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=cap)})
        res = rr.resolve(_batch(cap, vols))
        total = sum(a.steps for a in res.steps[0].aspirations)
        assert total == 3000
        planned, commanded = plan_error_ul(res)
        assert abs(commanded - planned) <= (cap * 1000 / 3000) / 2 + 1e-9  # 오차 ≤ 반 증분(누적되지 않는다)

    def test_over_full_stroke_is_rejected_before_motion(self):
        # 기구 검증용 3mL(1 증분 = 1µL 로 부동소수 없이 경계를 정확히 만든다 — 지원 목록 판정은 이 층 밖).
        rr = RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=3.0)})
        # 2999.5 증분(→반올림 3000) + 반 증분(단독 1스텝) — µL 합은 5000(용량 안)인데 두 번째 흡입의 누적 증가가 0 이라
        #   최소 1스텝이 붙어 3001 이 된다(웹은 1스텝 미만 조각을 만들지 않아 실제로는 안 온다 — 이건 pi 의 마지막 자물쇠) → 펌웨어 err3(부분 토출) 전에 실행 전 거부해야 한다.
        with pytest.raises(RecipeValidationError) as ei:
            rr.resolve(_batch(3.0, [2999.5, 0.5]))
        assert ei.value.reason == "batch_over_capacity"

    def test_plan_lists_pump_port_liquid_ul_steps(self):
        rr = RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=1.0)})
        res = rr.resolve(_batch(1.0, [100.0, 25.5]))
        plan = recipe_plan(res)
        assert plan == [
            {"pump": 1, "port": 2, "liquid": "l0", "ul": 100.0, "steps": 300, "stage": 0},
            {"pump": 1, "port": 3, "liquid": "l1", "ul": 25.5, "steps": 77, "stage": 0},
        ]


class TestSettingsChangeLog:
    def test_changes_before_after(self):
        before = _snap(1.0, h="aaaaaaaaaaaaaaaa")
        after = _snap(5.0, h="bbbbbbbbbbbbbbbb", tune=(900, 1000, 900, 7))
        ch = {c["field"]: (c["from"], c["to"]) for c in summarize_settings_change(before, after)}
        assert ch["syringeCapacityMl"] == (1.0, 5.0)
        assert ch["pumpTuning"] == ("v900V1400c900L7", "v900V1000c900L7")
        assert "pumpAddrs" not in ch

    def test_watcher_logs_fields(self):
        logs = []

        class L:
            def info(self, msg, **kw):
                logs.append((msg, kw))

        cfg = ServerConfig(base_url="https://example.invalid")
        w = SettingsWatcher(cfg, "tok", "fragrance", boot_settings_hash="aaaaaaaaaaaaaaaa",
                            boot_settings=_snap(1.0), logger=L())
        w.observe(_snap(5.0, h="bbbbbbbbbbbbbbbb"))
        msg, kw = logs[-1]
        assert "재시작 없이" in msg  # (2026-09-30) 설정 변경 = 무재시작 적용 대기
        assert kw["appliedHash"] == "aaaaaaaaaaaaaaaa" and kw["newHash"] == "bbbbbbbbbbbbbbbb"
        assert {"field": "syringeCapacityMl", "from": 1.0, "to": 5.0} in kw["changes"]


class TestManufactureSummaryLogs:
    """제조 1건 = 시작 요약(INFO) · 스테이지(DEBUG) · 종료 요약(INFO/WARN) — Cloud Logging 문턱(INFO) 이상에 요약이 뜬다."""

    def _seq(self, tmp_path, recs):
        from senlyt_pi.adapters.fake_engine_adapter import FakeEnginePort
        from senlyt_pi.obs.log import StructuredLogger
        from senlyt_pi.persistence.file_idempotency_ledger import FileIdempotencyLedger
        from senlyt_pi.pipeline.pump_sequencer import PumpSequencer

        log = StructuredLogger(device_id="d", sink=recs.append, stream=open(os.devnull, "w"))
        ctr = iter(range(10_000))
        seq = PumpSequencer(
            ledger=FileIdempotencyLedger.open(tmp_path / "l.log"),
            engine=FakeEnginePort(),
            resolver=RecipeResolver({1: SyringeSpec(pump_full_stroke=3000, syringe_capacity_ml=5.0)}),
            request_id_gen=lambda: f"r{next(ctr)}",
            now_iso=lambda: "2026-09-30T00:00:00.000Z",
            logger=log,
        )
        seq.log_context = {"contractId": "sensorium-icad-0.1.0+tecan", "settingsHash": "aaaaaaaaaaaaaaaa",
                           "syringeCapacityMl": 5.0, "fullStroke": 3000, "stepMode": "N0", "capacitySource": "snapshot"}
        return seq

    def test_start_and_end_summary_fields(self, tmp_path):
        recs: list = []
        seq = self._seq(tmp_path, recs)
        rep = seq.submit(command_id="ord-1:2", trace_id="t", steps=_batch(5.0, [100.0, 30.0]))
        assert rep.outcome.value == "completed"
        start = next(r for r in recs if r["message"] == "제조 시작 요약")
        end = next(r for r in recs if r["message"] == "제조 종료 요약")
        assert start["severity"] == "INFO" and end["severity"] == "INFO"
        sd, ed = start["detail"], end["detail"]
        assert start["orderId"] == "ord-1" and sd["attempt"] == 2
        for k in ("contractId", "settingsHash", "syringeCapacityMl", "fullStroke", "stepMode", "capacitySource"):
            assert k in sd
        assert sd["plan"][0] == {"pump": 1, "port": 2, "liquid": "l0", "ul": 100.0, "steps": 60, "stage": 0}
        assert sd["strokes"] == 1 and sd["stageCount"] == 1
        for k in ("outcome", "stepsDone", "stepN", "stageCount", "totalSteps", "totalMs", "plannedUl", "commandedUl", "errorUl"):
            assert k in ed
        assert ed["totalSteps"] == 78  # round(130µL × 0.6)
        assert abs(ed["errorUl"]) <= 5000 / 3000 / 2 + 1e-9
        # 스테이지별 로그는 두지 않는다(리뷰 2026-09-30 — 시작·종료 요약만)
        assert not any(r["message"] == "스테이지 완료" for r in recs)

    def test_validation_reject_is_warn_with_reason(self, tmp_path):
        recs: list = []
        seq = self._seq(tmp_path, recs)
        seq.submit(command_id="ord-2:1", trace_id="t", steps=_batch(5.0, [0.1]))
        rej = next(r for r in recs if r["message"].startswith("제조 거부"))
        assert rej["severity"] == "WARN"
        assert rej["detail"]["reason"] == "derived_zero_steps" and rej["detail"]["volumeUl"] == 0.1


class TestBootSelfCheckFields:
    def test_fields_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bootstrap_mod.time, "sleep", lambda s: None)
        recs: list = []
        from senlyt_pi.obs.log import StructuredLogger

        log = StructuredLogger(device_id="d", sink=recs.append, stream=open(os.devnull, "w"))
        store = DeviceIdentityStore(tmp_path / "identity.json")
        store.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
        det = DetectResult(model="tecan_xcalibur", responding=(1,), fingerprints={1: TECAN})
        build_components(URL_ENV, identity_store=store, register=False, fetch_settings=True, logger=log,
                         settings_fetcher=lambda c, t, m: _snap(1.0), port_lister=lambda: list(_CH340),
                         pump_detector=lambda p, a: det)
        boot = next(r for r in recs if r["message"].startswith("하드웨어 자가진단"))
        d = boot["detail"]
        assert d["syringeCapacityMl"] == 1.0 and d["capacitySource"] == "snapshot"
        assert d["fullStroke"] == 3000 and d["stepMode"] == "N0"
        assert d["contractId"] == "sensorium-icad-0.1.0+tecan" and d["settingsHash"] == "aaaaaaaaaaaaaaaa"
