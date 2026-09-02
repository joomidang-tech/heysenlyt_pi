"""하드웨어 선언(센소리움 단일 SoT) 계약 — 판독·캐시·resolver 축 (2026-09-02).

부팅 상태모델 D3 의 재료 검증: 스냅샷 엄격 판독 / 캐시 3축 SoT(용량 비캐시) / 캐시 부팅의
stroke·포트 상한이 pump_map 까지 닿는가(R-P0-4 — 안 닿으면 tecan 캐시 부팅이 영구 -1001).
"""

from senlyt_pi.adapters.settings_source import (
    pump_model_from_settings,
    valve_port_count_from_settings,
)
from senlyt_pi.app.bootstrap import build_resolver
from senlyt_pi.persistence.hardware_profile_cache import (
    HardwareProfile,
    load_profile,
    save_profile,
)

TECAN_SNAP = {
    "pumpPreset": {"pumpPresetId": "tecan_xcalibur", "pumpFullStroke": 3000, "syringeCapacityMl": 0.5},
    "hardware": {"sensoriumVersion": "sensorium-fragrance-1.0.0+tecan", "pumpModel": "tecan_xcalibur", "valvePortCount": 15, "source": "device"},
}


class TestStrictReaders:
    def test_pump_model_reads_declaration_channel_only(self):
        # R6 P0-1 — 판독 채널은 hardware(서버 주입 선언 블록) 단일. pumpPreset.pumpPresetId 는
        #   함대 문서라 병합 스킵에도 항상 "sy01b" 가 차 있어 "선언 없음"을 표현할 수 없다.
        assert pump_model_from_settings(TECAN_SNAP) == "tecan_xcalibur"
        assert pump_model_from_settings({"hardware": {"pumpModel": "sy01b"}}) == "sy01b"
        # ⛔ **no-merge 스냅샷(병합 스킵 = 순단·레거시 토큰·기기문서 부재) = None(Undeclared)** —
        #   구현이 pumpPreset 을 읽으면 이 벡터가 "sy01b" 로 통과해 Tecan 실물에 U-NVM 이 샌다.
        no_merge = {"pumpPreset": {"pumpPresetId": "sy01b", "syringeCapacityMl": 0.5}}
        assert pump_model_from_settings(no_merge) is None
        # ⛔ clamp 미사용 — 미지값·변조는 None(Undeclared)이지 sy01b 정규화가 아니다(U-NVM 급소).
        assert pump_model_from_settings({"hardware": {"pumpModel": "SY01B"}}) is None
        assert pump_model_from_settings({"hardware": {"pumpModel": "future-model"}}) is None
        assert pump_model_from_settings({"hardware": {}}) is None
        assert pump_model_from_settings(None) is None

    def test_valve_port_count_allowlist(self):
        assert valve_port_count_from_settings(TECAN_SNAP) == 15
        assert valve_port_count_from_settings({"hardware": {"valvePortCount": 12}}) == 12
        # 12|15 밖·불량·부재 = None(→ 12 기본) — 추측 확장 금지.
        for bad in (0, -1, 100, "15", True, None):
            assert valve_port_count_from_settings({"hardware": {"valvePortCount": bad}}) is None
        assert valve_port_count_from_settings({}) is None


class TestProfileCache:
    def test_roundtrip_and_url_binding(self, tmp_path):
        p = HardwareProfile(pump_model="tecan_xcalibur", pump_full_stroke=3000, valve_port_count=15)
        save_profile(tmp_path, p, "https://senlyt.com")
        loaded = load_profile(tmp_path, "https://senlyt.com")
        assert loaded is not None and loaded.pump_model == "tecan_xcalibur"
        assert loaded.pump_full_stroke == 3000 and loaded.valve_port_count == 15
        # 타 서버 URL 캐시는 무효 — URL 교체 재설치에서 옛 서버 선언 오용 방지.
        assert load_profile(tmp_path, "https://other.example") is None

    def test_corruption_and_unknown_model_invalid(self, tmp_path):
        from senlyt_pi.persistence.hardware_profile_cache import cache_path

        save_profile(
            tmp_path,
            HardwareProfile(pump_model="sy01b", pump_full_stroke=12000, valve_port_count=12),
            "https://senlyt.com",
        )
        # truncation — 손상 = 무효(추측 조립 금지).
        cache_path(tmp_path).write_text("{\"pumpModel\": \"sy01b\"", encoding="utf-8")
        assert load_profile(tmp_path, "https://senlyt.com") is None
        # 미지 모델 = 무효.
        cache_path(tmp_path).write_text(
            '{"pumpModel":"future","pumpFullStroke":3000,"valvePortCount":15,"serverBaseUrl":"https://senlyt.com"}',
            encoding="utf-8",
        )
        assert load_profile(tmp_path, "https://senlyt.com") is None


# 포트 상한 판정 파리티 벡터(T-4) — ⚠️ web __tests__/lib/server/portLayout.test.ts 의
#   PORT_PARITY_VECTORS 와 **문자 그대로 동일**(행 순서·개수 포함). 아래 크로스레포 테스트가
#   형제 워크트리의 그 파일을 파싱해 자동 대조한다(값·판정 동시 잠금).
PORT_PARITY_VECTORS: list[tuple[int, int, bool]] = [
    (1, 12, True),
    (12, 12, True),
    (13, 12, False),
    (15, 12, False),
    (13, 15, True),
    (15, 15, True),
    (16, 15, False),
    (0, 12, False),
    (0, 15, False),
    (-1, 15, False),
]


class TestPortParity:
    def test_is_port_valid_matches_vectors(self):
        from senlyt_pi.pipeline.recipe_resolver import _is_port_valid

        for port, max_port, expected in PORT_PARITY_VECTORS:
            assert _is_port_valid(port, max_port) is expected, (port, max_port)

    def test_web_vectors_literal_identical(self):
        # 모노레포 그물(기존 PARITY_VECTORS 와 같은 방식) — 형제 부재 시 skip.
        import json
        import re
        from pathlib import Path

        web_test = (
            Path(__file__).resolve().parents[2]
            / "heysenlyt-web" / "__tests__" / "lib" / "server" / "portLayout.test.ts"
        )
        if not web_test.exists():
            import pytest

            pytest.skip("형제 heysenlyt-web 미체크아웃 — 모노레포 컨텍스트에서만 대조")
        src = web_test.read_text(encoding="utf-8")
        m = re.search(r"PORT_PARITY_VECTORS[^=]*=\s*\[(.*?)\n\];", src, re.S)
        assert m, "web PORT_PARITY_VECTORS 블록을 찾지 못함"
        rows = re.findall(r"^\t\[(.*?)\],\s*$", m.group(1), re.M)
        assert re.search(r"of PORT_PARITY_VECTORS", src), "web 벡터가 단언 루프에 미사용"
        web = [tuple(json.loads(f"[{r}]")) for r in rows]
        py = [tuple(json.loads(json.dumps(list(v)))) for v in PORT_PARITY_VECTORS]
        assert len(web) == len(py), (len(web), len(py))
        for i, (w, p) in enumerate(zip(web, py)):
            assert tuple(json.dumps(x) for x in w) == tuple(json.dumps(x) for x in p), f"행 {i}"


class TestResolverFromCache:
    def test_cache_stroke_and_ports_reach_pump_map(self):
        # R-P0-4 — 스냅샷 부재 + tecan 캐시: stroke 3000 이 pump_map 에, 15 가 상한에 닿아야
        #   캐시 부팅이 영구 -1001 로 죽지 않는다. 용량은 비캐시 → 모드 기본 0.5 + 가드 OFF.
        profile = HardwareProfile(pump_model="tecan_xcalibur", pump_full_stroke=3000, valve_port_count=15)
        r = build_resolver(
            {"PUMP_ADDRESSES": "fragrance:1,2,3"}, server_settings=None, hardware_profile=profile
        )
        assert r.pump_map[1].pump_full_stroke == 3000
        assert r.pump_map[1].syringe_capacity_ml == 0.5  # 용량 비캐시(모드 기본).
        assert r.capacity_from_settings is False  # 용량 가드 자동 OFF(오거부 방지).
        assert r.valve_port_count == 15

    def test_snapshot_wins_over_cache(self):
        profile = HardwareProfile(pump_model="sy01b", pump_full_stroke=12000, valve_port_count=12)
        r = build_resolver(
            {"PUMP_ADDRESSES": "fragrance:1,2,3"},
            server_settings=TECAN_SNAP,
            hardware_profile=profile,
        )
        assert r.pump_map[1].pump_full_stroke == 3000  # 스냅샷 > 캐시.
        assert r.valve_port_count == 15
        assert r.capacity_from_settings is True

    def test_no_declaration_keeps_legacy_defaults(self):
        r = build_resolver({"PUMP_ADDRESSES": "flavor:1,2"}, server_settings=None)
        assert r.pump_map[1].pump_full_stroke == 12000
        assert r.valve_port_count == 12


# 폴백에 **실제 도달하는** 형상(R6.5 M3 SURVIVED 교훈) — pumpPreset 이 있으면
#   full_stroke_from_settings 가 clamp 로 프리셋 표 수치(3000)를 이미 돌려줘 `or` 폴백이
#   실행되지 않는다. 폴백을 겨누려면 **pumpPreset 자체가 없어야** 한다(hardware 블록만
#   실린 부분 스냅샷 = 병합 순단·부분 프레임에서 실제로 가능한 형상).
_TECAN_HW_ONLY = {
    "hardware": {
        "sensoriumVersion": "sensorium-fragrance-1.0.0+tecan",
        "pumpModel": "tecan_xcalibur",
        "valvePortCount": 15,
        "source": "device",
    },
}


class TestModelAwareStrokeFallback:
    def test_helper_fallback_is_declared_model_preset(self):
        # R6.5 P2 — 폴백이 sy01b 12000 고정이면 "tecan 선언 + pumpPreset 부재" 프레임에서
        #   {tecan, 12000} 프로파일이 캐시로 남아, 다음 오프라인 부팅이 어댑터 3000 vs
        #   spec 12000 = 영구 -1001 로 죽는다. 폴백은 **선언된 모델의 프리셋 기본**.
        #   헬퍼가 bootstrap·senlytd 양쪽의 유일한 조립 지점이라 이 그물 하나가 둘을 덮는다.
        from senlyt_pi.adapters.settings_source import hardware_profile_from_snapshot

        p = hardware_profile_from_snapshot("tecan_xcalibur", _TECAN_HW_ONLY)
        assert p.pump_model == "tecan_xcalibur"
        assert p.pump_full_stroke == 3000  # PUMP_PRESETS["tecan_xcalibur"] — 12000 이 아니다.
        assert p.valve_port_count == 15
        assert p.sensorium_version == "sensorium-fragrance-1.0.0+tecan"
        # sy01b 선언은 sy01b 기본으로 — 폴백이 모델을 따라간다는 대칭 확인.
        assert hardware_profile_from_snapshot("sy01b", {"hardware": {"pumpModel": "sy01b"}}).pump_full_stroke == 12000

    def test_bootstrap_uses_helper_for_snapshot_profile(self, tmp_path):
        # build_components 스냅샷 경로가 헬퍼를 경유하는지 — pumpPreset 부재 프레임으로
        #   폴백까지 관통 확인(픽스처에 pumpPreset 을 넣으면 clamp 가 3000 을 만들어
        #   폴백 미도달 = 변이 무방비가 된다).
        from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
        from senlyt_pi.app.bootstrap import build_components
        from senlyt_pi.config.server_target import SENLYT_ENV_KEY

        store = DeviceIdentityStore(tmp_path / "identity.json")
        store.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
        comp = build_components(
            {SENLYT_ENV_KEY: "v1_2_0"},
            identity_store=store,
            register=False,
            fetch_settings=True,
            settings_fetcher=lambda cfg, tok, mode: dict(_TECAN_HW_ONLY),
        )
        assert comp.hardware_profile is not None
        assert comp.hardware_profile.pump_model == "tecan_xcalibur"
        assert comp.hardware_profile.pump_full_stroke == 3000  # sy01b 12000 이 아니다.
        assert comp.hardware_profile.valve_port_count == 15
