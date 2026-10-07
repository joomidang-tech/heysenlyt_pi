"""기기 설정 한 벌(2026-09-29) — pi 측: 상시 설정 구독 · 유휴 재시작 · 하트비트 settingsHash · 캐시 확장 · 서버 parity.

잠그는 것:
  ① SettingsWatcher — 적용 해시와 다른 해시 프레임이 오면 적용 대기 · 같은 해시·해시 없는 프레임(구 서버)은 무시 ·
     부팅 때 스냅샷이 없었으면 첫 해시 프레임이 곧 대기. (2026-09-30 재시작 없는 적용 = test_settings_hot_apply_20260930)
  ② (폐기 2026-09-30) 유휴 재시작 — 설정 변경은 재시작하지 않는다.
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
    """(2026-09-30 무재시작 단일 규칙) 해시가 적용 값과 다르면 적용 대기 — 재시작 사유는 더 없다(test_settings_hot_apply 참고)."""

    def test_same_hash_or_no_hash_is_not_pending(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash="aaaaaaaa11111111")
        w.observe(_frame("aaaaaaaa11111111"))
        w.observe(_frame(None))  # 구 서버 프레임 — 비교 안 함
        assert w.pending_settings() is None

    def test_different_hash_is_pending(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash="aaaaaaaa11111111")
        f = _frame("bbbbbbbb22222222", "sensorium-icad-0.1.0")
        w.observe(f)
        assert w.pending_settings() is f
        w.mark_applied(f)
        assert w.pending_settings() is None and w.applied_settings_hash() == "bbbbbbbb22222222"

    def test_boot_without_snapshot_first_hash_is_pending(self):
        w = SettingsWatcher(_cfg(), "t", "fragrance", boot_settings_hash=None)
        w.observe(_frame("aaaaaaaa11111111"))
        assert w.pending_settings() is not None

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
        assert w.pending_settings() is not None

    def test_readers(self):
        assert settings_hash_from_settings({"settingsHash": "NOT-HEX"}) is None
        assert contract_id_from_settings({"hardware": {"sensoriumVersion": "x+tecan"}}) == "x+tecan"


def _daemon():
    from senlyt_pi.app.daemon import DaemonDeps, SenlytDaemon
    from senlyt_pi.persistence.idempotency_ledger import InMemoryIdempotencyLedger

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
            applied_contract_id="sensorium-fragrance-1.0.0",
            applied_settings_hash="aaaaaaaa11111111",
        )
    )
    return d, _Sink


class TestHeartbeat:
    def test_heartbeat_carries_contract_hash_probe_addrs(self):
        d, sink = _daemon()
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
                           "pumpMaxStartSpeedHz": 900, "pumpMaxTopSpeedHz": 1400, "pumpMaxCutoffSpeedHz": 900,
                           "pumpMaxSlope": 7, "pumpSyringeTypeCode": 0},
            "hardware": {"pumpModel": "tecan_xcalibur", "contractId": "sensorium-icad-0.1.0+tecan",
                         "valvePortCount": 12},
            "pumpPorts": {"1": {}, "2": {}, "3": {}, "4": {}},
        },
        "mode": "fragrance",
        "expect": {"model": "tecan_xcalibur", "capacity": 0.25, "v": 900, "V": 1400, "c": 900, "L": 7,
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
