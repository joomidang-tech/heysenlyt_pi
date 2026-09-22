"""펌프 프리셋 clamp + 부피→스텝 파생 — SoT §6 (byte-parity 안전 급소).

**바이트 동일 축 = 서버 `pumpGuard.ts`/`settingsClamp.ts`(TS) ↔ 이 파일(Python).**
P0 HW 안전 — 식향 Code 11(플런저 오버로드·과다흡입) 재발 방지. 근본원인은
"하드코딩 24000 vs 파생 9600"의 2.5배 불일치였다(SoT 서두).

Dart `lib/core/pump_guard.dart` 포팅이되, **수치 정본 = 서버 TS**:
  - Dart 이관본의 validSyringeCapacitiesMl 4종 [1.25,0.5,2.5,5] → **9종**
    [0.025,0.05,0.1,0.25,0.5,1.0,1.25,2.5,5.0] (pumpGuard.ts VALID_SYRINGE_ML)으로 정정.
  - Tecan(Cavro XLP6000·XCalibur) 프리셋은 제거됨(2026-07-18 · 미도입·기기 미입고 — 서버 TS와 동일).
    현재 빌트인 = SY-01B 1종(+ custom). Dart 파일엔 남아 있으나 그건 동결된 포팅 오라클.
  - 그 외(프리셋 수치·custom 절대상한·기본값·단조성 2줄 순서)는 서버 TS와 대조 완료(동일).

라운딩(부록A P-8): round = half-up(양수 도메인 JS `Math.round` = `floor(x+0.5)`).
Python 내장 round() 는 banker's rounding 이라 **사용 금지** — `_round_half_up` 고정.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PumpPreset:
    """PumpPreset 7필드 — SoT §6-1 (고정 필드명·타입·순서)."""

    pump_preset_id: str  # 항상 "sy01b" (사용자 지정·Tecan/Cavro 제거·2026-07-18)
    pump_full_stroke: int  # 풀스트로크
    pump_max_start_speed_hz: int  # v 상한(start speed)
    pump_max_top_speed_hz: int  # V 상한(top speed)
    pump_max_cutoff_speed_hz: int  # c 상한(cutoff speed)
    pump_max_slope: int  # L 상한(slope)
    pump_syringe_type_code: int  # 스톨 서브코드 U<code>


# 빌트인 프리셋 정식 수치표 — SoT §6-2 (입력 무시·강제 · 바이트 동일 SoT = pumpGuard.ts).
#
# 현재 빌트인 = SY-01B(기본) + Tecan XCalibur(2026-09-01 실물 도입 대비 재도입). 사용자 지정
# (custom)은 제거 유지(2026-07-18). `clamp_pump_preset` 은 **명시 id "tecan_xcalibur" 만 존중**,
# 그 외 전부(오타·미지·custom 포함) SY-01B 폴백이다 — 서버 pumpGuard.ts `clampPumpPreset` 과
# byte-parity(2026-09-01 축 계약: 서버가 tecan 을 배웠다. ⛔ "무조건 sy01b" 로 원복 금지 —
# 원복하면 설정축이 죽어 tecan 기기가 4배 토출한다). 어댑터 조립(`SENLYT_ENGINE=tecan`)은
# 이와 별개로 자기 프리셋을 명시적으로 집어 쓴다(이중 키의 env 쪽).
PUMP_PRESETS: dict[str, PumpPreset] = {
    "sy01b": PumpPreset(
        pump_preset_id="sy01b",
        pump_full_stroke=12000,
        pump_max_start_speed_hz=1000,
        pump_max_top_speed_hz=6000,
        pump_max_cutoff_speed_hz=5400,
        pump_max_slope=20,
        pump_syringe_type_code=200,
    ),
    # Tecan Cavro XCalibur — 수치 정본: 00_research "Manual Operating Cavro XCalibur
    # 20733085-C.txt" (§3.3.2 N0 표준 3000 증분/풀스트로크 · §3.5.3 v 50..1000/V 5..6000/
    # c 50..2700/L 1..20). 표준 모드(N0) 고정 — 미세모드(N1·24000)는 속도 단위가 바뀌므로
    # (increments/sec) 실기기 프로브로 확정 전 도입하지 않는다.
    # pump_syringe_type_code=0: XCalibur 에는 스톨전류 명령(U<code>,<n>)이 없다 — U 는
    # NVM 설정 기록(§3.3.2 Table 3-5)이라 절대 오용 금지. 어댑터가 이 필드를 쓰지 않는다.
    "tecan_xcalibur": PumpPreset(
        pump_preset_id="tecan_xcalibur",
        pump_full_stroke=3000,
        pump_max_start_speed_hz=1000,
        pump_max_top_speed_hz=6000,
        pump_max_cutoff_speed_hz=2700,
        pump_max_slope=20,
        pump_syringe_type_code=0,
    ),
}

# 유효 syringe 용량 이산값(mL) — v1.1.0 allowlist **9종**(서버 pumpGuard.ts VALID_SYRINGE_ML 정본).
VALID_SYRINGE_CAPACITIES_ML: frozenset[float] = frozenset(
    {0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.25, 2.5, 5.0}
)


def _round_half_up(x: float) -> int:
    """round = half-up(.5 올림 · 양수 도메인 JS Math.round 등가) — 부록A P-8. 내장 round() 금지."""
    return math.floor(x + 0.5)


def clamp_pump_preset(cfg: Mapping[str, Any] | None) -> PumpPreset:
    """clampPumpPreset(cfg) — SoT §6-3 (서버 pumpGuard.ts 와 byte-parity·동일 알고리즘).

    판정식(2026-09-01 테칸 재도입 — 서버 TS 와 동일하게 개정):
      - `pumpPresetId == "tecan_xcalibur"` **명시 입력일 때만** 그 빌트인을 존중한다.
      - 그 외 전부(부재·sy01b·custom·unknown·레거시 cavro_*) = SY-01B — 기존과 동일 바이트.
      - 수치는 어느 쪽이든 **표 강제**(입력 수치 무시) — 손 튜닝값이 물리로 가는 경로 차단
        (과다흡입 안전 불변식 유지). syringeCapacityMl 은 호출 측이 별도 주입한다.

    ⚠️ 어댑터 생성자에 이 반환값을 주입하지 말 것 — 어댑터 preset 은 어댑터 클래스가
    소유한다(검증 P1-6: settings 프리셋을 Sy01b 어댑터에 꽂으면 `U0,…`=XCalibur NVM
    "밸브 없음" 기록 같은 조합이 문법적으로 가능해진다). 이 함수의 소비처는 스텝 축
    (SyringeSpec.pump_full_stroke) 파생뿐이다.
    """
    raw_id = cfg.get("pumpPresetId") if isinstance(cfg, Mapping) else None
    honored = "tecan_xcalibur" if raw_id == "tecan_xcalibur" else "sy01b"
    return PUMP_PRESETS[honored]


# ── §6-3a PumpTuning — 제조사 기본값 = 초깃값 · 표 = 상한, 운영자 "적용" 속도 축(v·V·c·L)만 매뉴얼 범위 안에서 인정 (2026-09-22 둘째 판) ──
#
# 서버 `pumpGuard.ts` §6-3a 와 **바이트 동일**(PUMP_TUNING_BOUNDS·clamp_pump_tuning·apply_pump_tuning).
#   열리는 축 = v·V·c·L 4개(하드웨어 테스트 툴 PumpGuard 가 편집을 허용하던 축).
#   잠긴 축  = pump_full_stroke(스텝 환산 — Code 11 원인 축)·pump_syringe_type_code(스톨 U — XCalibur 엔
#             NVM 기록이라 오용 금지). 어떤 입력이 와도 표 값이다.
#   튠이 없으면 apply_pump_tuning 은 그 기종 **제조사 기본값**(PUMP_TUNING_DEFAULTS)을 얹는다 — 표 상한이 아니다.
# ⚠️ clamp_pump_preset 의 "어댑터 생성자에 주입 금지(P1-6)" 경고는 **기종 교차** 위험(sy01b 표를 Tecan 에)
#   이었다. 튠 경로는 `pump_tuning_from_settings`(settings_source) 가 **스냅샷 pumpPresetId == 실물 기종**
#   일 때만 만들고, 그 기종의 표 위에 그 기종 범위로 잘린 값만 얹으므로 교차가 성립하지 않는다 — 그래서
#   이 반환값은 어댑터 `preset=` 에 주입해도 된다(Tecan 어댑터 생성자 가드도 같은 범위라 통과).

TUNING_KEYS: tuple[str, ...] = (
    "pumpMaxStartSpeedHz",
    "pumpMaxTopSpeedHz",
    "pumpMaxCutoffSpeedHz",
    "pumpMaxSlope",
)

# 기종별 **매뉴얼 허용 범위** {(min, max)} — 서버 PUMP_MANUAL_RANGES 와 바이트 동일(§6-3a 둘째 판 2026-09-22).
#   출처: SY-01B ASCII 매뉴얼 V1.2 §4.5.3(v 1..1000 · V 1..6000 · c 1..5400 · L 1..20)
#        XCalibur 매뉴얼 20733085-C §3.5.3·G.5(v 50..1000 · V 5..6000 · c 50..2700 · L 1..20).
#   V 의 실제 하한은 max(V.min, v.min, c.min) 로 올린다(_bounds_of) — 단조성 보정이 v·c 를 V 까지 끌어내리므로
#   V 가 v·c 하한 아래면 v·c 가 XCalibur err3 영역(50 미만)으로 떨어진 프레임이 나간다(검증 P0-1).
PUMP_MANUAL_RANGES: dict[str, dict[str, tuple[int, int]]] = {
    "sy01b": {
        "pumpMaxStartSpeedHz": (1, 1000),
        "pumpMaxTopSpeedHz": (1, 6000),
        "pumpMaxCutoffSpeedHz": (1, 5400),
        "pumpMaxSlope": (1, 20),
    },
    "tecan_xcalibur": {
        "pumpMaxStartSpeedHz": (50, 1000),
        "pumpMaxTopSpeedHz": (5, 6000),
        "pumpMaxCutoffSpeedHz": (50, 2700),
        "pumpMaxSlope": (1, 20),
    },
}

# 기종별 **제조사 기본값** — 운영자가 한 번도 적용하지 않은 기종의 실물 초기값(2026-09-22 사용자 확정:
#   "설정한 적 없으면 기본값, 있으면 이전값"). 서버 PUMP_TUNING_DEFAULTS 와 바이트 동일.
#   종전(첫 판)엔 표 상한(v1000·V6000·c5400/2700·L20)이 초기값이었고 v1.2.0 이후 그 값으로 필드 검증돼 있었다 —
#   이 릴리스부터 튠 없는 기기는 느려진다(토출 시간 재확인 대상 · sy01b_engine_adapter._speed_cmd 주석 참조).
#   출처: SY-01B §4.5.3(v900 · V4000 · c900 · L14) · XCalibur G.5(v900 · V1400 · c900 · L7).
PUMP_TUNING_DEFAULTS: dict[str, dict[str, int]] = {
    "sy01b": {
        "pumpMaxStartSpeedHz": 900,
        "pumpMaxTopSpeedHz": 4000,
        "pumpMaxCutoffSpeedHz": 900,
        "pumpMaxSlope": 14,
    },
    "tecan_xcalibur": {
        "pumpMaxStartSpeedHz": 900,
        "pumpMaxTopSpeedHz": 1400,
        "pumpMaxCutoffSpeedHz": 900,
        "pumpMaxSlope": 7,
    },
}


def _bounds_of(model: str) -> dict[str, tuple[int, int]]:
    """매뉴얼 범위 ∩ 기종 표 — **max 는 표 값을 넘지 못한다**(서버 boundsOf 와 동일 · 튠은 표에서 내리기만) · V.min 은 v·c 하한 이상."""
    t = PUMP_PRESETS[model]
    m = PUMP_MANUAL_RANGES[model]
    speed_floor = max(m["pumpMaxTopSpeedHz"][0], m["pumpMaxStartSpeedHz"][0], m["pumpMaxCutoffSpeedHz"][0])
    return {
        "pumpMaxStartSpeedHz": (m["pumpMaxStartSpeedHz"][0], min(m["pumpMaxStartSpeedHz"][1], t.pump_max_start_speed_hz)),
        "pumpMaxTopSpeedHz": (speed_floor, min(m["pumpMaxTopSpeedHz"][1], t.pump_max_top_speed_hz)),
        "pumpMaxCutoffSpeedHz": (m["pumpMaxCutoffSpeedHz"][0], min(m["pumpMaxCutoffSpeedHz"][1], t.pump_max_cutoff_speed_hz)),
        "pumpMaxSlope": (m["pumpMaxSlope"][0], min(m["pumpMaxSlope"][1], t.pump_max_slope)),
    }


# 기종별 절대 범위 {(min, max)} — 서버 PUMP_TUNING_BOUNDS 와 동일(max = min(매뉴얼, 표) · min = 매뉴얼 · V.min 올림).
#   sy01b: v 1~1000 / V 1~6000 / c 1~5400 / L 1~20 · tecan_xcalibur: v 50~1000 / V 50~6000 / c 50~2700 / L 1~20.
PUMP_TUNING_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "sy01b": _bounds_of("sy01b"),
    "tecan_xcalibur": _bounds_of("tecan_xcalibur"),
}


def _clamp_tune_int(v: Any, bound: tuple[int, int]) -> int | None:
    """정수 clamp(half-up round 후 [min,max]). 숫자 아님/bool/NaN → None(= 그 키는 튠 없음). TS clampTuneInt 등가."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    lo, hi = bound
    return min(hi, max(lo, _round_half_up(float(v))))


def clamp_pump_tuning(model: str, raw: Any) -> dict[str, int] | None:
    """기종 하나의 튠 입력 정규화 — 허용 4키만·기종 범위 clamp·비수치 키 버림. 비면 None. TS clampPumpTuning 등가.

    단조성은 여기서 강제하지 않는다(부분 튠은 표 값과 합쳐진 뒤에야 순서를 판정) — apply_pump_tuning 담당.
    """
    if model not in PUMP_TUNING_BOUNDS or not isinstance(raw, Mapping):
        return None
    bounds = PUMP_TUNING_BOUNDS[model]
    out: dict[str, int] = {}
    for k in TUNING_KEYS:
        n = _clamp_tune_int(raw.get(k), bounds[k])
        if n is not None:
            out[k] = n
    return out or None


def apply_pump_tuning(preset: PumpPreset, tuning: Mapping[str, Any] | None) -> PumpPreset:
    """기종 preset 위에 **제조사 기본값 → 그 기종 튠** 순으로 속도 축을 얹는다. TS applyPumpTuning 등가(§6-3a 둘째 판).

    - 빌트인 기종: 튠 없음 = v·V·c·L 이 PUMP_TUNING_DEFAULTS[기종](제조사 기본값). 튠이 있으면 그 키만 덮는다.
      ⇒ 표(PUMP_PRESETS)의 속도 축은 **상한**으로만 쓰이고 실물 초기값이 아니다.
    - 미지 기종(custom·레거시): 입력 그대로(같은 객체).
    단조성 2줄 순서 고정(서버·테스트 툴 `c = clamp(clamp(c), v, V)` 규약):
      1) v = min(v, V)          2) c = max(min(c, V), v)
    스트로크·U 는 건드리지 않는다.
    """
    base = PUMP_TUNING_DEFAULTS.get(preset.pump_preset_id)
    if base is None:
        return preset
    safe = clamp_pump_tuning(preset.pump_preset_id, tuning) or {}
    top = safe.get("pumpMaxTopSpeedHz", base["pumpMaxTopSpeedHz"])
    start = min(safe.get("pumpMaxStartSpeedHz", base["pumpMaxStartSpeedHz"]), top)
    cutoff = max(min(safe.get("pumpMaxCutoffSpeedHz", base["pumpMaxCutoffSpeedHz"]), top), start)
    slope = safe.get("pumpMaxSlope", base["pumpMaxSlope"])
    return PumpPreset(
        pump_preset_id=preset.pump_preset_id,
        pump_full_stroke=preset.pump_full_stroke,
        pump_max_start_speed_hz=start,
        pump_max_top_speed_hz=top,
        pump_max_cutoff_speed_hz=cutoff,
        pump_max_slope=slope,
        pump_syringe_type_code=preset.pump_syringe_type_code,
    )


def resolve_syringe_capacity_ml(raw: Any, *, is_flavor: bool) -> float:
    """syringeCapacityMl 이산값 검증 — SoT §6-1 / O-15 (TS coerceSyringeCapacityMl 등가).

    유효집합(9종) 밖이면 **모드 기본값 폴백**(스냅 아님). 기본 용량 = 양 모드 공통 0.5mL
    (2026-07-17 확정: flavor 2펌프·fragrance 3펌프 모두 시린지 0.5mL). is_flavor 는
    시그니처 호환을 위해 유지하되 현재 폴백값은 동일하다.
    """
    fallback = 0.5
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    d = float(raw)
    return d if d in VALID_SYRINGE_CAPACITIES_ML else fallback


@dataclass(frozen=True, slots=True)
class SyringeSpec:
    """부피→스텝 파생(SyringeSpec) — SoT §6-4 (하드코딩 금지·파생이 SoT).

      steps         = round( pumpFullStroke × volumeUl ÷ (syringeCapacityMl × 1000) )
      stepsPerMl    = pumpFullStroke ÷ syringeCapacityMl
      maxVolumeUl   = syringeCapacityMl × 1000   (per-pump 안전 게이트 상한)

    검산(§6-4): 12000 × 100 ÷ 500 = 2400 steps / 시린지 0.5mL stepsPerMl=24000, maxVol 500µL
               (양 모드 공통 0.5mL — 2026-07-17 확정: 식향 2펌프·향장향 3펌프). 참고: 1.25mL
               선택 시 12000 × 100 ÷ 1250 = 960 steps / stepsPerMl=9600.
    """

    pump_full_stroke: int
    syringe_capacity_ml: float

    @property
    def max_volume_ul(self) -> float:
        """per-pump 안전 게이트 상한(µL)."""
        return self.syringe_capacity_ml * 1000

    @property
    def steps_per_ml(self) -> float:
        """mL 당 스텝수."""
        return self.pump_full_stroke / self.syringe_capacity_ml

    def steps_for_volume_ul(self, volume_ul: float) -> int:
        """부피(µL) → 스텝수. round = half-up(양수 도메인·부록A P-8)."""
        return _round_half_up(
            self.pump_full_stroke * volume_ul / (self.syringe_capacity_ml * 1000)
        )

    # ── 초기화 파라미터 (용량 파생) — v1.1.0 `syringe_spec.dart` 포팅 ──────────────
    #
    # ⚠️ **모드가 아니라 용량이 결정한다.** v1.1.0 실기기 리포트에서 드러난 사고 경로가
    #    "설정 용량을 무시하고 모드 기본으로 초기화힘을 유도 → Z1R(Half)이어야 할 500µL
    #    시린지에 ZR(Full)이 나가는 매뉴얼 위반"이었다. 그래서 파생을 이 한 곳에 응집한다.
    #    v1.2.0 은 양 모드 공통 0.5mL 이므로 **둘 다 Z1R**(구 식향 1.25mL = ZR 이었다).

    @property
    def stall_current(self) -> int:
        """스톨 전류 단계 n (`U<code>,<n>R`) — 용량 파생 (Manual V1.2 §1.2 Table).

        ≤25µL → 4 · 50µL~1.25mL → 5 · 2.5~5mL → 6.
        """
        ul = self.max_volume_ul
        if ul <= 25:
            return 4
        if ul <= 1250:
            return 5
        return 6

    @property
    def _init_force(self) -> int:
        """초기화 힘 코드 — 용량 파생 (Manual V1.2 §4.4.1). 0=Full·1=Half·2=Third.

        시린지 씰 보호가 목적이라 **작은 시린지에 Full 을 걸면 안 된다**
        (≥1.0mL → Full · 250·500µL → Half · 50·100µL → Third).
        """
        if self.syringe_capacity_ml >= 1.0:
            return 0
        if self.max_volume_ul >= 250:
            return 1
        return 2

    @property
    def init_command(self) -> str:
        """초기화 실행 명령(포트 미지정) — `Z<n1>R`. 힘은 용량 파생(`_init_force`).

        ⚠️ 포트 파라미터가 없으면 펌웨어 기본값(흡입=포트1·배출=마지막 포트)으로 돈다 —
        포트1 액체가 매 초기화마다 빨렸다 버려진다(2026-07-21 실기기 확정). 서버가 포트를
        실어주는 경로는 `init_command_with` 를 쓴다. 이 기본형은 하위호환(구 서버) 전용.
        """
        force = self._init_force
        return "ZR" if force == 0 else f"Z{force}R"

    def init_command_with(self, in_port: "int | None", out_port: "int | None") -> str:
        """포트 지정 초기화 명령 — `Z<힘>,<흡입포트>,<배출포트>R` (Manual §4.4.2 n1,n2,n3).

        흡입=air 포트(액체 소모 0·공기만) / 배출=output 포트(잔여물이 정상 출구로) —
        2026-07-21 QA "초기화시 흡입/배출 포트 변경". 힘은 용량 파생 그대로(n1 명시 필수 —
        포트를 주면서 힘을 생략할 수 없다). 어느 한쪽이라도 없으면 기본형으로 폴백.
        """
        if in_port is None or out_port is None:
            return self.init_command
        return f"Z{self._init_force},{in_port},{out_port}R"


def fragrance_ml_to_ul(amount_ml: float) -> float:
    """fragrance 단위 정규화 — SoT §6-6. amountMl → volumeUl(µL). 미스매치 = Code 11.

    (flavor volume 은 이미 µL 이므로 정규화 불필요.)
    """
    return amount_ml * 1000


def is_volume_within_gate(volume_ul: float, spec: SyringeSpec) -> bool:
    """recipe 스텝 검증 게이트 — SoT §6-4 / §9-1.
    0 < volumeUl ≤ maxVolumeUl. 위반 → CMD_VALIDATION_FAILED(drop).
    """
    return 0 < volume_ul <= spec.max_volume_ul


# 축(stroke) 불일치 fail-closed 코드(2026-09-01 검증 C) — 어댑터 축 가드가 시리얼 송신 없이
#   반환한다. 하드웨어 에러코드(0~15)·통신 sentinel(-1000·-2000)과 겹치지 않는 전용 음수.
#   의미: "명령 spec 의 풀스트로크 ≠ 어댑터 프리셋 풀스트로크" — admin 설정(pumpPresetId)과
#   기기 SENLYT_ENGINE 이 어긋난 상태. 무성 1/4·4배 토출을 막기 위해 모션 자체를 거부한다.
# 축 불일치(2026-09-02 재해석) — 단일 키 설계에선 "스텝 조립 전제 축 vs 부팅 시 조립된 어댑터
#   축"의 드리프트(캐시 부팅 중 서버 선언 변경·스테일 봉투 창)를 뜻한다. 구 의미("설정 vs
#   SENLYT_ENGINE env 불일치")의 env 키는 은퇴됐다 — sy01b_engine_adapter._axis_guard 헤더 참조.
AXIS_MISMATCH_RAW_CODE = -1001
# 하드웨어 선언 미확정(2026-09-02 단일 키) — 스냅샷·캐시 모두 없거나 pumpModel 미지값. 어떤
#   모션도 조립 불가(추측 금지) → permanent(미분류 코드의 보수 분기와 같은 등급). 복구 = 서버
#   재접속(재fetch 성공 시 재기동) 또는 senlytd 재시작.
UNDECLARED_HW_RAW_CODE = -1002
# 모델 지문 불일치(2026-09-03) — sy01b 어댑터가 실물에서 **Tecan 지문**(& 파트넘버)을 읽음.
#   기종 전용 프레임(U=XCalibur NVM 기록)을 쏘기 전에 정직하게 거부한다. 복구 = 올바른
#   센소리움(+tecan 변형) 선택 후 재연결.
MODEL_MISMATCH_RAW_CODE = -1003


class EngineErrorClass(enum.Enum):
    """EnginePort 에러코드 분류 — SoT §6-7."""

    NORMAL = "normal"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify_engine_error_code(code: int) -> EngineErrorClass:
    """엔진 raw errorCode(정수) → 분류 — SoT §6-7.
    0 = 정상 / 1·7·11·15·timeout = transient(R=3 재시도) / 2·3·9·10 = permanent(즉시중단 FAILED).
    축 불일치(-1001)는 설정을 고치기 전엔 재시도가 무의미 — permanent 명시 분기.
    """
    if code == 0:
        return EngineErrorClass.NORMAL
    if code == AXIS_MISMATCH_RAW_CODE:
        return EngineErrorClass.PERMANENT
    if code in (UNDECLARED_HW_RAW_CODE, MODEL_MISMATCH_RAW_CODE):
        # 선언/모델을 고치기 전엔 재시도 무의미 — permanent 명시 분기(R9 P3: 기본 폴백에
        #   얹지 않고 명시해 "복구 = 센소리움 정정 + 재연결" 지시가 라벨로 전달되게 한다).
        return EngineErrorClass.PERMANENT
    if code in (1, 7, 11, 15):
        return EngineErrorClass.TRANSIENT
    if code in (2, 3, 9, 10):
        return EngineErrorClass.PERMANENT
    # 미분류 코드는 보수적으로 permanent(안전측·즉시중단).
    return EngineErrorClass.PERMANENT


class StatusErrorCode(enum.Enum):
    """status.errorCode — SoT §6-7 / §9-2 7종 + CMD_STALE(2026-07-19 신설).

    CMD_STALE = 정비 신선도 초과("지금 아니면 무효" 게이트가 실행 없이 종단). 종전엔
    CMD_VALIDATION_FAILED 를 재사용해 admin 이 "명령 형식 오류"로 표시했다 — 운영자가
    "다시 누르면 된다"를 알 수 없던 오해 소지(15:35·16:03 실기기 2회). 라벨 분리용.
    """

    CMD_VALIDATION_FAILED = "CMD_VALIDATION_FAILED"
    CMD_STALE = "CMD_STALE"
    DUPLICATE_DROPPED = "DUPLICATE_DROPPED"
    ENGINE_TIMEOUT = "ENGINE_TIMEOUT"
    ENGINE_ERROR_TRANSIENT = "ENGINE_ERROR_TRANSIENT"
    ENGINE_ERROR_PERMANENT = "ENGINE_ERROR_PERMANENT"
    PARTIAL_DISPENSE = "PARTIAL_DISPENSE"
    INTERRUPTED = "INTERRUPTED"

    @property
    def wire(self) -> str:
        return self.value

    @staticmethod
    def from_wire(v: Any) -> "StatusErrorCode | None":
        if not isinstance(v, str):
            return None
        for e in StatusErrorCode:
            if e.wire == v:
                return e
        return None
