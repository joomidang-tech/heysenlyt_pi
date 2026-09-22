"""기종별 속도 튠(§6-3a 둘째 판 · 2026-09-22) — 제조사 기본값 = 초깃값 · 표 = 상한, 운영자 "적용" 값(v·V·c·L)만
매뉴얼 범위 안에서 인정.

이 파일이 고정하는 계약:
  1. **byte-parity 벡터** — TUNING_PARITY_VECTORS 는 heysenlyt-web `__tests__/lib/server/pumpGuard.test.ts`
     의 같은 이름 배열과 **리터럴 동일**(한쪽만 고치면 리뷰에서 걸리게 상호 경로를 주석으로 못박음).
  2. **튠 없음 = 제조사 기본값** — 튠 없음/무효/기종 불일치 = 그 기종 PUMP_TUNING_DEFAULTS(SY-01B v900·V4000·c900·L14 /
     XCalibur v900·V1400·c900·L7). 스트로크·U 는 어떤 입력에도 표 값. 미지 기종(custom)만 입력 그대로.
  3. **기종 교차 차단** — 스냅샷 pumpPresetId ≠ 실물 기종이면 튠을 버린다(pump_tuning_from_settings → None),
     build_engine 도 preset.pump_preset_id ≠ pump_model 이면 무시한다(P1-6 정신).
  4. **조립 통합** — sy01b 스냅샷에 V 4000 이 실려 오면 어댑터 `_speed_cmd` 의 top 이 4000 으로 제한된다.
"""

from __future__ import annotations

from senlyt_pi.adapters.settings_source import pump_tuning_from_settings
from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.app.bootstrap import build_engine
from senlyt_pi.core.pump_guard import (
    PUMP_PRESETS,
    PUMP_TUNING_BOUNDS,
    PUMP_TUNING_DEFAULTS,
    TUNING_KEYS,
    apply_pump_tuning,
    clamp_pump_tuning,
)

# ── byte-parity 벡터(튠) — 서버 TS 와 리터럴 동일 유지 ──────────────────────────────
#   ⚠️ heysenlyt-web `__tests__/lib/server/pumpGuard.test.ts` TUNING_PARITY_VECTORS 와 **리터럴 동일**.
#   형식: (model, 입력 튠(None=없음), 기대 (v, V, c, L)). 스트로크·U 는 항상 표 값이라 벡터에 없다.
TUNING_PARITY_VECTORS: list[tuple[str, dict | None, tuple[int, int, int, int]]] = [
    ("sy01b", None, (900, 4000, 900, 14)),
    ("sy01b", {}, (900, 4000, 900, 14)),
    ("tecan_xcalibur", None, (900, 1400, 900, 7)),
    ("sy01b", {"pumpMaxTopSpeedHz": 6000}, (900, 6000, 900, 14)),
    ("sy01b", {"pumpMaxTopSpeedHz": 800}, (800, 800, 800, 14)),
    (
        "sy01b",
        {
            "pumpMaxStartSpeedHz": 99999,
            "pumpMaxTopSpeedHz": 99999,
            "pumpMaxCutoffSpeedHz": 99999,
            "pumpMaxSlope": 99,
        },
        (1000, 6000, 5400, 20),
    ),
    ("sy01b", {"pumpMaxStartSpeedHz": 0, "pumpMaxSlope": 0}, (1, 4000, 900, 1)),
    ("sy01b", {"pumpMaxTopSpeedHz": 499}, (499, 499, 499, 14)),
    ("tecan_xcalibur", {"pumpMaxTopSpeedHz": 5}, (50, 50, 50, 7)),
    ("sy01b", {"pumpMaxCutoffSpeedHz": 2000, "pumpMaxStartSpeedHz": 3000}, (1000, 4000, 2000, 14)),
    ("sy01b", {"pumpMaxCutoffSpeedHz": 100, "pumpMaxStartSpeedHz": 800}, (800, 4000, 800, 14)),
    ("tecan_xcalibur", {"pumpMaxCutoffSpeedHz": 5400}, (900, 1400, 1400, 7)),
    ("tecan_xcalibur", {"pumpMaxStartSpeedHz": 10, "pumpMaxTopSpeedHz": 1}, (50, 50, 50, 7)),
    ("sy01b", {"pumpMaxTopSpeedHz": 4500.5}, (900, 4501, 900, 14)),
    ("sy01b", {"pumpMaxTopSpeedHz": "4000"}, (900, 4000, 900, 14)),
    ("sy01b", {"pumpMaxTopSpeedHz": True}, (900, 4000, 900, 14)),
]


class TestParity:
    def test_vectors_match_server(self):
        for model, tuning, (v, top, c, slope) in TUNING_PARITY_VECTORS:
            out = apply_pump_tuning(PUMP_PRESETS[model], tuning)
            assert (
                out.pump_max_start_speed_hz,
                out.pump_max_top_speed_hz,
                out.pump_max_cutoff_speed_hz,
                out.pump_max_slope,
            ) == (v, top, c, slope), (model, tuning)
            assert out.pump_full_stroke == PUMP_PRESETS[model].pump_full_stroke
            assert out.pump_syringe_type_code == PUMP_PRESETS[model].pump_syringe_type_code
            assert out.pump_preset_id == model

    def test_bounds_match_server_and_contain_table(self):
        # 매뉴얼 범위 — SY-01B §4.5.3 / XCalibur §3.5.3·G.5. V.min 은 v·c 하한으로 올림(XCalibur 5 → 50).
        assert PUMP_TUNING_BOUNDS["sy01b"] == {
            "pumpMaxStartSpeedHz": (1, 1000),
            "pumpMaxTopSpeedHz": (1, 6000),
            "pumpMaxCutoffSpeedHz": (1, 5400),
            "pumpMaxSlope": (1, 20),
        }
        assert PUMP_TUNING_BOUNDS["tecan_xcalibur"] == {
            "pumpMaxStartSpeedHz": (50, 1000),
            "pumpMaxTopSpeedHz": (50, 6000),
            "pumpMaxCutoffSpeedHz": (50, 2700),
            "pumpMaxSlope": (1, 20),
        }
        attr = {
            "pumpMaxStartSpeedHz": "pump_max_start_speed_hz",
            "pumpMaxTopSpeedHz": "pump_max_top_speed_hz",
            "pumpMaxCutoffSpeedHz": "pump_max_cutoff_speed_hz",
            "pumpMaxSlope": "pump_max_slope",
        }
        for model, bounds in PUMP_TUNING_BOUNDS.items():
            table = PUMP_PRESETS[model]
            for k in TUNING_KEYS:
                lo, hi = bounds[k]
                assert lo <= getattr(table, attr[k]) <= hi
                # 상한 = 표(파생) — 튠은 내리기만(검증 P1-3).
                assert hi == getattr(table, attr[k])
            # V 하한 ≥ v·c 하한 — 단조성 결과가 v·c 자기 하한(XCalibur err3 영역) 아래로 못 내려간다(검증 P0-1).
            assert bounds["pumpMaxTopSpeedHz"][0] >= max(
                bounds["pumpMaxStartSpeedHz"][0], bounds["pumpMaxCutoffSpeedHz"][0]
            )
            # 제조사 기본값은 자기 범위 안.
            for k in TUNING_KEYS:
                lo, hi = bounds[k]
                assert lo <= PUMP_TUNING_DEFAULTS[model][k] <= hi


class TestNoSideEffect:
    def test_none_or_empty_is_manufacturer_default(self):
        """튠 없음/무효 = 제조사 기본값(둘째 판) — 표 상한이 아니다. 스트로크·U·id 는 표 그대로."""
        p = PUMP_PRESETS["sy01b"]
        for tuning in (None, {}, {"pumpMaxTopSpeedHz": "4000"}):
            out = apply_pump_tuning(p, tuning)
            assert (
                out.pump_max_start_speed_hz,
                out.pump_max_top_speed_hz,
                out.pump_max_cutoff_speed_hz,
                out.pump_max_slope,
            ) == (900, 4000, 900, 14)
            assert (out.pump_preset_id, out.pump_full_stroke, out.pump_syringe_type_code) == ("sy01b", 12000, 200)
        t = apply_pump_tuning(PUMP_PRESETS["tecan_xcalibur"], None)
        assert (t.pump_max_start_speed_hz, t.pump_max_top_speed_hz, t.pump_max_cutoff_speed_hz, t.pump_max_slope) == (
            900, 1400, 900, 7,
        )
        assert (t.pump_full_stroke, t.pump_syringe_type_code) == (3000, 0)

    def test_unknown_preset_id_is_untouched(self):
        """custom/레거시 id 엔 튠을 얹지 않는다 — 서버 applyPumpTuning 과 같은 착지(검증 하네스에서 갈렸던 지점)."""
        from senlyt_pi.core.pump_guard import PumpPreset as _P

        custom = _P("custom", 12000, 1000, 6000, 5400, 20, 200)
        assert apply_pump_tuning(custom, {"pumpMaxTopSpeedHz": 0}) is custom

    def test_locked_axes_stay_table(self):
        out = apply_pump_tuning(
            PUMP_PRESETS["sy01b"],
            {"pumpFullStroke": 99999, "pumpSyringeTypeCode": 0, "pumpMaxTopSpeedHz": 4000},
        )
        assert out.pump_full_stroke == 12000
        assert out.pump_syringe_type_code == 200
        assert out.pump_max_top_speed_hz == 4000

    def test_clamp_pump_tuning_drops_unknown_and_non_numeric(self):
        assert clamp_pump_tuning(
            "sy01b", {"pumpMaxTopSpeedHz": 99999, "pumpFullStroke": 1, "foo": 1, "pumpMaxSlope": "x"}
        ) == {"pumpMaxTopSpeedHz": 6000}
        assert clamp_pump_tuning("sy01b", {}) is None
        assert clamp_pump_tuning("sy01b", None) is None
        assert clamp_pump_tuning("custom", {"pumpMaxTopSpeedHz": 1}) is None


class TestFrameFloor:
    """어느 튠이든 실제 프레임의 v·c 가 기종 하한 아래로 못 내려간다(검증 P0-1 · 프레임 층 방어)."""

    def test_min_tuned_frame_stays_in_manual_range(self):
        for model, cls in (("sy01b", Sy01bEngineAdapter), ("tecan_xcalibur", TecanXCaliburEngineAdapter)):
            snap = {
                "pumpPreset": {"pumpPresetId": model, "pumpMaxTopSpeedHz": 1},
                "hardware": {"pumpModel": model, "valvePortCount": 12},
            }
            tuned = pump_tuning_from_settings(snap, model)
            engine = build_engine({}, on_pi=lambda: True, pump_model=model, pump_preset=tuned)
            assert isinstance(engine, cls)
            frame = engine._speed_cmd(6000, 14)
            # V 1 → 하한: SY-01B 는 매뉴얼 1, XCalibur 는 50(v·c 하한으로 올린 V.min).
            lo = PUMP_TUNING_BOUNDS[model]["pumpMaxTopSpeedHz"][0]
            slope = min(14, PUMP_TUNING_DEFAULTS[model]["pumpMaxSlope"])  # 요청 L14 vs 기본값 상한(XCalibur 7)
            assert frame == f"v{lo}V{lo}c{lo}L{slope}", (model, frame)

    def test_speed_cmd_floors_start_and_cutoff_even_with_hostile_preset(self):
        """프리셋이 어떻게 오든 프레임 v·c 는 MIN_SPEED_HZ 위 — 어댑터가 마지막 방어선."""
        from senlyt_pi.core.pump_guard import PumpPreset as _P

        hostile = _P("tecan_xcalibur", 3000, 5, 5, 5, 20, 0)
        eng = TecanXCaliburEngineAdapter(preset=hostile)
        frame = eng._speed_cmd(6000, 14)
        assert frame == f"v{eng.MIN_SPEED_HZ}V{eng.MIN_SPEED_HZ}c{eng.MIN_SPEED_HZ}L14"


SY_SNAP_TUNED = {
    "pumpPreset": {
        "pumpPresetId": "sy01b",
        "pumpFullStroke": 12000,
        "pumpMaxStartSpeedHz": 1000,
        "pumpMaxTopSpeedHz": 4000,
        "pumpMaxCutoffSpeedHz": 4000,
        "pumpMaxSlope": 20,
        "pumpSyringeTypeCode": 200,
        "syringeCapacityMl": 0.5,
    },
    "hardware": {"sensoriumVersion": "sensorium-fragrance-1.0.0", "pumpModel": "sy01b", "valvePortCount": 12},
}


class TestSettingsSource:
    def test_tuned_snapshot_for_same_model(self):
        p = pump_tuning_from_settings(SY_SNAP_TUNED, "sy01b")
        assert p is not None
        assert p.pump_max_top_speed_hz == 4000
        assert p.pump_max_cutoff_speed_hz == 4000
        assert p.pump_full_stroke == 12000  # 스냅샷이 스트로크를 뭐라 해도 표 값.

    def test_model_mismatch_returns_none(self):
        """선언(sy01b) ≠ 실물(tecan) 부팅 — sy01b 상한을 Tecan 에 꽂지 않는다(기종 교차 차단)."""
        assert pump_tuning_from_settings(SY_SNAP_TUNED, "tecan_xcalibur") is None

    def test_missing_axes_fall_back_to_manufacturer_default(self):
        snap = {
            "pumpPreset": {"pumpPresetId": "sy01b", "pumpMaxTopSpeedHz": 6000, "syringeCapacityMl": 0.5},
            "hardware": {"pumpModel": "sy01b", "valvePortCount": 12},
        }
        p = pump_tuning_from_settings(snap, "sy01b")
        assert p == apply_pump_tuning(PUMP_PRESETS["sy01b"], {"pumpMaxTopSpeedHz": 6000})
        assert (p.pump_max_start_speed_hz, p.pump_max_top_speed_hz, p.pump_max_cutoff_speed_hz, p.pump_max_slope) == (
            900, 6000, 900, 14,
        )

    def test_unmerged_frame_without_hardware_block_is_ignored(self):
        """병합 스킵 프레임(hardware 없음)은 pumpPresetId 가 늘 채워져 있어도 소비하지 않는다(검증 P2-4 · R6 P0-1 채널)."""
        snap = {"pumpPreset": {"pumpPresetId": "sy01b", "pumpMaxTopSpeedHz": 4000}}
        assert pump_tuning_from_settings(snap, "sy01b") is None

    def test_absent_or_malformed(self):
        assert pump_tuning_from_settings(None, "sy01b") is None
        assert pump_tuning_from_settings({}, "sy01b") is None
        assert pump_tuning_from_settings({"pumpPreset": "x"}, "sy01b") is None
        assert pump_tuning_from_settings(SY_SNAP_TUNED, "unknown") is None


class TestBuildEngineInjection:
    def test_tuned_preset_reaches_speed_cmd(self):
        tuned = pump_tuning_from_settings(SY_SNAP_TUNED, "sy01b")
        engine = build_engine({}, on_pi=lambda: True, pump_model="sy01b", pump_preset=tuned)
        assert isinstance(engine, Sy01bEngineAdapter)
        assert engine.preset.pump_max_top_speed_hz == 4000
        # 서버가 top 6000 을 요구해도 어댑터 상한(튠 V 4000)이 이긴다 — 프레임에 v1000V4000c4000.
        assert engine._speed_cmd(6000, 14) == "v1000V4000c4000L14"
        # 스텝 축은 표 그대로라 축 가드(spec.stroke == preset.stroke)가 그대로 성립.
        assert engine.preset.pump_full_stroke == 12000

    def test_none_preset_is_manufacturer_default(self):
        """튠 정보 없음(스냅샷 부재·캐시 부팅) = 제조사 기본값으로 조립 — 표 상한(v1000V6000c5400)이 아니다."""
        engine = build_engine({}, on_pi=lambda: True, pump_model="sy01b", pump_preset=None)
        assert isinstance(engine, Sy01bEngineAdapter)
        assert engine.preset == apply_pump_tuning(PUMP_PRESETS["sy01b"], None)
        # 서버가 top 6000 을 요구해도 기본값 V4000 이 상한 — 프레임 v900V4000c900.
        assert engine._speed_cmd(6000, 14) == "v900V4000c900L14"

    def test_cross_model_preset_is_ignored(self):
        """호출측 실수로 sy01b 튠을 tecan 조립에 넘겨도 build_engine 이 기종 대조로 버린다."""
        tuned = pump_tuning_from_settings(SY_SNAP_TUNED, "sy01b")
        engine = build_engine({}, on_pi=lambda: True, pump_model="tecan_xcalibur", pump_preset=tuned)
        assert isinstance(engine, TecanXCaliburEngineAdapter)
        assert engine.preset == apply_pump_tuning(PUMP_PRESETS["tecan_xcalibur"], None)
        assert engine._speed_cmd(6000, 14) == "v900V1400c900L7"

    def test_tecan_tuned_within_manual_passes_ctor_guard(self):
        snap = {
            "pumpPreset": {"pumpPresetId": "tecan_xcalibur", "pumpMaxTopSpeedHz": 3000, "pumpMaxCutoffSpeedHz": 2000},
            "hardware": {"pumpModel": "tecan_xcalibur", "valvePortCount": 12},
        }
        tuned = pump_tuning_from_settings(snap, "tecan_xcalibur")
        engine = build_engine({}, on_pi=lambda: True, pump_model="tecan_xcalibur", pump_preset=tuned)
        assert isinstance(engine, TecanXCaliburEngineAdapter)
        assert engine.preset.pump_max_top_speed_hz == 3000
        assert engine.preset.pump_max_cutoff_speed_hz == 2000
        assert engine.preset.pump_full_stroke == 3000


class TestBuildComponentsNoTuning:
    """부팅 스냅샷에 속도 축이 없으면 어댑터 preset == 제조사 기본값 — bootstrap 이 늘 pump_preset 인자를 넘기는 단계의 그물."""

    def test_snapshot_without_tuning_boots_manufacturer_default(self, tmp_path):
        from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
        from senlyt_pi.app.bootstrap import build_components
        from senlyt_pi.config.server_target import SENLYT_ENV_KEY

        store = DeviceIdentityStore(tmp_path / "identity.json")
        store.save(DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999))
        snap = {
            "pumpPreset": {"pumpPresetId": "sy01b", "pumpFullStroke": 12000, "syringeCapacityMl": 0.5},
            "hardware": {"sensoriumVersion": "sensorium-fragrance-1.0.0", "pumpModel": "sy01b", "valvePortCount": 12},
        }
        comp = build_components(
            {SENLYT_ENV_KEY: "v1_2_0"},
            identity_store=store,
            register=False,
            fetch_settings=True,
            settings_fetcher=lambda cfg, token, mode: snap,
            port_lister=lambda: [],
        )
        preset = getattr(comp.engine, "preset", None)
        assert preset == apply_pump_tuning(PUMP_PRESETS["sy01b"], None)
        assert preset.pump_max_top_speed_hz == 4000 and preset.pump_full_stroke == 12000

    def test_snapshot_with_tuning_boots_tuned_preset(self, tmp_path):
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
            settings_fetcher=lambda cfg, token, mode: SY_SNAP_TUNED,
            port_lister=lambda: [],
        )
        preset = getattr(comp.engine, "preset", None)
        assert preset is not None and preset.pump_max_top_speed_hz == 4000
        assert preset.pump_full_stroke == 12000
