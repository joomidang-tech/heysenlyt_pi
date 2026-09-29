"""향연 4펌프(2026-09-29 · Tecan 12채널 × 4) — 부팅 인식·주기 감시가 프로브할 펌프 주소.

모드 파생([1,2,3])만 프로브하면 addr 4 가 pump_map 에 안 올라가 그 펌프 향료가 전부 unmapped drop 된다.
서버 설정 스냅샷 `pumpPorts` 키(기기 세대의 통 배치 · 펌프 대수만큼)를 합친다. 헤이센릿은 바이트 불변.
"""

from senlyt_pi.adapters.settings_source import default_pump_addrs, expected_pump_addrs


def _snap(*pumps: str) -> dict:
    return {"pumpPorts": {p: {"11": {"liquid": "output"}} for p in pumps}}


def test_mode_default_unchanged_without_snapshot():
    assert expected_pump_addrs("fragrance", None) == [1, 2, 3]
    assert expected_pump_addrs("flavor", None) == [1, 2]
    assert expected_pump_addrs(None, None) == [1, 2, 3]
    assert default_pump_addrs("FLAVOR") == [1, 2]


def test_heysenlyt_snapshot_is_byte_identical_to_mode_default():
    assert expected_pump_addrs("fragrance", _snap("1", "2", "3")) == [1, 2, 3]
    assert expected_pump_addrs("flavor", _snap("1", "2")) == [1, 2]


def test_icad_four_pumps_from_snapshot():
    assert expected_pump_addrs("fragrance", _snap("1", "2", "3", "4")) == [1, 2, 3, 4]


def test_broadcast_and_junk_keys_ignored():
    snap = {"pumpPorts": {"0": {}, "x": {}, "4": {}}}
    assert expected_pump_addrs("fragrance", snap) == [1, 2, 3, 4]


class _Bus4:
    """주소 1~4 가 응답하는 버스(향연 Tecan 4대)."""

    def probe(self, addr: int) -> bool:
        return addr in (1, 2, 3, 4)


def test_build_resolver_maps_fourth_pump_when_snapshot_declares_it():
    from senlyt_pi.app.bootstrap import build_resolver

    four = build_resolver(
        {"SENLYT_MODE": "fragrance"}, engine=_Bus4(), server_settings=_snap("1", "2", "3", "4")
    )
    assert sorted(four.pump_map) == [1, 2, 3, 4]
    # 헤이센릿(스냅샷 3대) — 4번이 응답해도 프로브하지 않는다(종전 동작).
    three = build_resolver(
        {"SENLYT_MODE": "fragrance"}, engine=_Bus4(), server_settings=_snap("1", "2", "3")
    )
    assert sorted(three.pump_map) == [1, 2, 3]


# ── 오프라인 캐시 부팅(서버 스냅샷 없음) — 캐시 pumpAddrs 로 향연 4번 펌프까지 프로브 (2026-09-29) ──────────
from senlyt_pi.adapters.settings_source import (  # noqa: E402
    expected_pump_addrs_with_source,
    hardware_profile_from_snapshot,
)
from senlyt_pi.persistence.hardware_profile_cache import (  # noqa: E402
    HardwareProfile,
    cache_path,
    load_profile,
    save_profile,
)

_URL = "https://example.test"
_ICAD_SNAPSHOT = {
    "hardware": {"pumpModel": "tecan_xcalibur", "sensoriumVersion": "sensorium-icad-0.1.0+tecan"},
    "pumpPorts": {"1": {}, "2": {}, "3": {}, "4": {}},
}


def test_offline_cache_with_pump_addrs_probes_four(tmp_path):
    save_profile(tmp_path, hardware_profile_from_snapshot("tecan_xcalibur", _ICAD_SNAPSHOT), _URL)
    cached = load_profile(tmp_path, _URL)
    assert cached is not None and cached.pump_addrs == (1, 2, 3, 4)
    addrs, src = expected_pump_addrs_with_source("fragrance", None, cached)
    assert addrs == [1, 2, 3, 4] and src == "cache"


def test_offline_without_cache_falls_back_to_mode_default():
    assert expected_pump_addrs_with_source("fragrance", None, None) == ([1, 2, 3], "mode-default")
    assert expected_pump_addrs_with_source("flavor", None, None) == ([1, 2], "mode-default")


def test_old_cache_without_pump_addrs_is_still_valid_and_uses_mode_default(tmp_path):
    import json
    cache_path(tmp_path).write_text(json.dumps({
        "pumpModel": "sy01b", "pumpFullStroke": 12000, "valvePortCount": 12, "serverBaseUrl": _URL,
    }), encoding="utf-8")
    cached = load_profile(tmp_path, _URL)
    assert cached is not None and cached.pump_addrs == ()
    assert expected_pump_addrs_with_source("fragrance", None, cached) == ([1, 2, 3], "mode-default")


def test_corrupted_pump_addrs_falls_back_to_mode_default(tmp_path):
    import json
    for bad in (["4"], [0], [99], [True], "1,2,3,4", {"4": 1}):
        cache_path(tmp_path).write_text(json.dumps({
            "pumpModel": "tecan_xcalibur", "pumpFullStroke": 3000, "valvePortCount": 12,
            "serverBaseUrl": _URL, "pumpAddrs": bad,
        }), encoding="utf-8")
        cached = load_profile(tmp_path, _URL)
        assert cached is not None and cached.pump_addrs == (), bad  # 프로파일은 살리고 주소만 버린다
        assert expected_pump_addrs_with_source("fragrance", None, cached) == ([1, 2, 3], "mode-default")
    cache_path(tmp_path).write_text("{not json", encoding="utf-8")  # 파일 전체 손상 = 캐시 무효
    assert load_profile(tmp_path, _URL) is None
    assert expected_pump_addrs_with_source("fragrance", None, None)[1] == "mode-default"


def test_online_snapshot_wins_over_cache():
    stale = HardwareProfile(pump_model="tecan_xcalibur", pump_full_stroke=3000, valve_port_count=12, pump_addrs=(1, 2, 3, 4))
    heysenlyt_snap = {"pumpPorts": {"1": {}, "2": {}, "3": {}}}
    assert expected_pump_addrs_with_source("fragrance", heysenlyt_snap, stale) == ([1, 2, 3], "snapshot")
    assert expected_pump_addrs_with_source("fragrance", _ICAD_SNAPSHOT, None) == ([1, 2, 3, 4], "snapshot")


def test_heysenlyt_cache_roundtrip_is_three_pumps(tmp_path):
    save_profile(tmp_path, hardware_profile_from_snapshot("sy01b", {"pumpPorts": {"1": {}, "2": {}, "3": {}}}), _URL)
    cached = load_profile(tmp_path, _URL)
    assert cached is not None and cached.pump_addrs == (1, 2, 3)
    assert expected_pump_addrs_with_source("fragrance", None, cached) == ([1, 2, 3], "cache")
