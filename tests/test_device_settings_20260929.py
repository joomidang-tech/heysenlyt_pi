"""기기 설정 한 벌(2026-09-29) — pi 측: 상시 설정 구독 · 유휴 재시작 · 하트비트 settingsHash · 캐시 확장 · 서버 parity.

잠그는 것:
  ① SettingsWatcher — 부팅 해시와 다른 해시 프레임이 오면 사유를 든다 · 같은 해시·해시 없는 프레임(구 서버)은 무시 ·
     부팅 때 스냅샷이 없었으면 첫 해시 프레임이 곧 변경.
  ② 데몬 — 사유가 있어도 **바쁘면**(제조·세척 실행 / 대기 큐) 재시작하지 않고, 유휴가 되면 정책 콜백 1회.
  ③ 하트비트 — appliedContractId · settingsHash(되돌려 보냄) · probeAddrs(프로브한 주소) 방출.
  ④ 오프라인 캐시 — 계약 · 용량 · 유효 튠을 저장·복원(손상 = None · 옛 캐시 호환).
  ⑤ 서버↔pi 스냅샷 parity — web `__tests__/lib/server/deviceProfile.test.ts` SNAPSHOT_PARITY_VECTORS 와 리터럴 동일.
"""

from __future__ import annotations

import json

from senlyt_pi.adapters.settings_source import (
    expected_pump_addrs,
    hardware_profile_from_snapshot,
    pump_model_from_settings,
    pump_tuning_from_settings,
    syringe_capacity_from_settings,
)
from senlyt_pi.adapters.settings_watcher import (
    SettingsWatcher,
    contract_id_from_settings,
    settings_hash_from_settings,
)
from senlyt_pi.config.server_target import ServerConfig
from senlyt_pi.core.wire_messages import Heartbeat
from senlyt_pi.persistence.hardware_profile_cache import (
    HardwareProfile,
    load_profile,
    save_profile,
)


def _cfg() -> ServerConfig:
    return ServerConfig(base_url="https://example.test")


def _frame(h: "str | None", contract: str = "sensorium-fragrance-1.0.0") -> dict:
    f: dict = {"hardware": {"contractId": contract, "pumpModel": "sy01b"}}
    if h is not None:
        f["settingsHash"] = h
    return f


class TestSettingsWatcher:
    def test_same_hash_or_no_hash_is_not_a_change(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash="aaaaaaaa11111111")
        w.observe(_frame("aaaaaaaa11111111"))
        w.observe(_frame(None))  # 구 서버 프레임 — 비교 안 함
        assert w.changed_reason() is None

    def test_different_hash_sets_reason_once(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash="aaaaaaaa11111111")
        w.observe(_frame("bbbbbbbb22222222", "sensorium-icad-0.1.0"))
        r = w.changed_reason()
        assert r is not None and "bbbbbbbb22222222" in r and "sensorium-icad-0.1.0" in r
        w.observe(_frame("cccccccc33333333"))
        assert w.changed_reason() == r  # 첫 사유 유지(재시작은 한 번)

    def test_boot_without_snapshot_first_hash_is_change(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash=None)
        w.observe(_frame("aaaaaaaa11111111"))
        assert w.changed_reason() is not None

    def test_run_once_reads_stream_frames(self):
        class _S:
            def events(self):
                yield "settings", json.dumps({"settings": _frame("dddddddd44444444")})

            def close(self):
                pass

        w = SettingsWatcher(
            _cfg(), "t", "fragrance", boot_settings_hash="aaaaaaaa11111111", open_stream=lambda *a, **k: _S()
        )
        assert w.run_once() is True
        assert w.changed_reason() is not None

    def test_readers(self):
        assert settings_hash_from_settings({"settingsHash": "NOT-HEX"}) is None
        assert contract_id_from_settings({"hardware": {"sensoriumVersion": "x+tecan"}}) == "x+tecan"


def _daemon(*, reason: "str | None", busy: bool, fired: list):
    from senlyt_pi.app.daemon import DaemonDeps, SenlytDaemon
    from senlyt_pi.persistence.idempotency_ledger import InMemoryIdempotencyLedger

    class _Watch:
        def changed_reason(self):
            return reason

    class _Sink:
        last = None

        def send_heartbeat(self, hb):
            _Sink.last = hb

        def report_status(self, r):
            pass

    d = SenlytDaemon(
        DaemonDeps(
            device_id="dev-A",
            command_source=type("S", (), {"commands": lambda s, i: iter(())})(),
            status_sink=_Sink(),
            engine=object(),  # type: ignore[arg-type]
            ledger=InMemoryIdempotencyLedger(),  # type: ignore[arg-type]
            heartbeat_interval_s=0,
            hw_watch_addrs=(1, 2, 3),
            settings_watch=_Watch(),
            on_settings_changed=lambda: fired.append(1),
            applied_contract_id="sensorium-fragrance-1.0.0",
            applied_settings_hash="aaaaaaaa11111111",
        )
    )
    if busy:
        d._sequencer._busy = True  # noqa: SLF001 — 제조·세척 실행 중
    return d, _Sink


class TestIdleRestart:
    def test_busy_waits_then_idle_restarts_once(self):
        fired: list = []
        d, _ = _daemon(reason="기기 설정 변경", busy=True, fired=fired)
        d._emit_heartbeat()
        assert fired == []  # 바쁘면 기다린다
        d._sequencer._busy = False  # noqa: SLF001
        d._emit_heartbeat()
        d._emit_heartbeat()
        assert fired == [1]  # 유휴 → 1회

    def test_no_reason_no_restart(self):
        fired: list = []
        d, _ = _daemon(reason=None, busy=False, fired=fired)
        d._emit_heartbeat()
        assert fired == []

    def test_heartbeat_carries_contract_hash_probe_addrs(self):
        fired: list = []
        d, sink = _daemon(reason=None, busy=False, fired=fired)
        d._emit_heartbeat()
        j = sink.last.to_json()
        assert j["appliedContractId"] == "sensorium-fragrance-1.0.0"
        assert j["settingsHash"] == "aaaaaaaa11111111"
        assert j["probeAddrs"] == [1, 2, 3]

    def test_old_heartbeat_omits_new_keys(self):
        j = Heartbeat(device_id="d", queue_depth=0).to_json()
        assert "settingsHash" not in j and "probeAddrs" not in j


class TestOfflineCache:
    def test_contract_capacity_tuning_roundtrip(self, tmp_path):
        snap = {
            "pumpPreset": {
                "pumpPresetId": "sy01b",
                "syringeCapacityMl": 1.25,
                "pumpMaxStartSpeedHz": 800,
                "pumpMaxTopSpeedHz": 800,
                "pumpMaxCutoffSpeedHz": 800,
                "pumpMaxSlope": 14,
            },
            "hardware": {"pumpModel": "sy01b", "contractId": "sensorium-expo-0.1.2", "valvePortCount": 12},
            "pumpPorts": {"1": {}, "2": {}},
        }
        prof = hardware_profile_from_snapshot("sy01b", snap)
        assert prof.contract_id == "sensorium-expo-0.1.2"
        assert prof.syringe_capacity_ml == 1.25
        assert prof.pump_tuning == {
            "pumpMaxStartSpeedHz": 800,
            "pumpMaxTopSpeedHz": 800,
            "pumpMaxCutoffSpeedHz": 800,
            "pumpMaxSlope": 14,
        }
        save_profile(tmp_path, prof, "https://example.test")
        back = load_profile(tmp_path, "https://example.test")
        assert back is not None
        assert (back.contract_id, back.syringe_capacity_ml, back.pump_tuning) == (
            prof.contract_id,
            prof.syringe_capacity_ml,
            prof.pump_tuning,
        )

    def test_old_cache_without_new_keys_still_valid(self, tmp_path):
        save_profile(tmp_path, HardwareProfile("sy01b", 12000, 12), "https://example.test")
        raw = json.loads((tmp_path / "hardware-profile.json").read_text())
        for k in ("contractId", "syringeCapacityMl", "pumpTuning"):
            raw.pop(k, None)
        (tmp_path / "hardware-profile.json").write_text(json.dumps(raw))
        back = load_profile(tmp_path, "https://example.test")
        assert back is not None and back.syringe_capacity_ml is None and back.pump_tuning is None

    def test_invalid_capacity_is_dropped(self, tmp_path):
        save_profile(tmp_path, HardwareProfile("sy01b", 12000, 12, syringe_capacity_ml=0.33), "https://e.test")
        back = load_profile(tmp_path, "https://e.test")
        assert back is not None and back.syringe_capacity_ml is None


# ⚠️ web __tests__/lib/server/deviceProfile.test.ts SNAPSHOT_PARITY_VECTORS 의 **서버 출력 프레임**을 옮긴 리터럴 — 한쪽만 고치지 말 것.
SNAPSHOT_PARITY_VECTORS = [
    {
        "frame": {
            "pumpPreset": {"pumpPresetId": "sy01b", "pumpFullStroke": 12000, "syringeCapacityMl": 0.5,
                           "pumpMaxStartSpeedHz": 900, "pumpMaxTopSpeedHz": 4000, "pumpMaxCutoffSpeedHz": 900,
                           "pumpMaxSlope": 14, "pumpSyringeTypeCode": 200},
            "hardware": {"pumpModel": "sy01b", "contractId": "sensorium-fragrance-1.0.0", "valvePortCount": 12},
            "pumpPorts": {"1": {}, "2": {}, "3": {}},
        },
        "mode": "fragrance",
        "expect": {"model": "sy01b", "capacity": 0.5, "v": 900, "V": 4000, "c": 900, "L": 14, "addrs": [1, 2, 3]},
    },
    {
        "frame": {
            "pumpPreset": {"pumpPresetId": "tecan_xcalibur", "pumpFullStroke": 3000, "syringeCapacityMl": 0.25,
                           "pumpMaxStartSpeedHz": 900, "pumpMaxTopSpeedHz": 1200, "pumpMaxCutoffSpeedHz": 900,
                           "pumpMaxSlope": 5, "pumpSyringeTypeCode": 0},
            "hardware": {"pumpModel": "tecan_xcalibur", "contractId": "sensorium-icad-0.1.0+tecan",
                         "valvePortCount": 12},
            "pumpPorts": {"1": {}, "2": {}, "3": {}, "4": {}},
        },
        "mode": "fragrance",
        "expect": {"model": "tecan_xcalibur", "capacity": 0.25, "v": 900, "V": 1200, "c": 900, "L": 5,
                   "addrs": [1, 2, 3, 4]},
    },
    {
        "frame": {
            "pumpPreset": {"pumpPresetId": "sy01b", "pumpFullStroke": 12000, "syringeCapacityMl": 1.25,
                           "pumpMaxStartSpeedHz": 800, "pumpMaxTopSpeedHz": 800, "pumpMaxCutoffSpeedHz": 800,
                           "pumpMaxSlope": 14, "pumpSyringeTypeCode": 200},
            "hardware": {"pumpModel": "sy01b", "contractId": "sensorium-expo-0.1.2", "valvePortCount": 12},
            "pumpPorts": {"1": {}, "2": {}},
        },
        "mode": "flavor",
        "expect": {"model": "sy01b", "capacity": 1.25, "v": 800, "V": 800, "c": 800, "L": 14, "addrs": [1, 2]},
    },
]


class TestServerParity:
    def test_vectors(self):
        for vec in SNAPSHOT_PARITY_VECTORS:
            f, e = vec["frame"], vec["expect"]
            model = pump_model_from_settings(f)
            assert model == e["model"]
            assert syringe_capacity_from_settings(f) == e["capacity"]
            tuned = pump_tuning_from_settings(f, model)
            assert tuned is not None
            assert (
                tuned.pump_max_start_speed_hz,
                tuned.pump_max_top_speed_hz,
                tuned.pump_max_cutoff_speed_hz,
                tuned.pump_max_slope,
            ) == (e["v"], e["V"], e["c"], e["L"])
            assert expected_pump_addrs(vec["mode"], f) == e["addrs"]
