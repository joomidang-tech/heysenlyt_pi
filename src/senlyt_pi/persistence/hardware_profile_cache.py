"""하드웨어 프로파일 로컬 캐시 (2026-09-02 — 센소리움 단일 SoT · 오프라인 부팅 폴백).

서버 스냅샷이 준 하드웨어 선언을 상태 디렉터리에 남겨, 다음 부팅에서 fetch 가 실패해도
같은 구성으로 조립한다(없으면 Undeclared fail-closed — bootstrap 상태모델 D3).

캐시가 SoT 로 삼는 축은 **{pumpModel, pumpFullStroke, valvePortCount} 셋뿐**이다(R-P0-4).
(2026-09-29) `pumpAddrs` — 스냅샷 `pumpPorts` 키(설정상 펌프 주소 집합)를 **프로브 대상 보조축**으로 함께 남긴다.
  향연(ICAD)은 Tecan 4대라, 오프라인 부팅이 모드 기본 [1,2,3]만 프로브하면 addr 4 가 빠진다. 이 값은 판정축이
  아니다 — 없거나 손상이면 **빈 튜플**(= 모드 기본으로 폴백)이고, 프로파일 자체를 무효로 만들지 않는다(옛 캐시 호환).
(2026-09-29 · 기기 설정 한 벌) `contractId`·`syringeCapacityMl`·`pumpTuning`(유효 v·V·c·L) — 오프라인 부팅의 **추정값**을
  "모드 기본 0.5·제조사 기본 튠"에서 "마지막으로 받은 이 기기 설정"으로 바꾼다(기기마다 시린지를 바꿀 수 있게 된 뒤 0.5 추정은
  틀릴 확률이 커졌다). (2026-09-30) 캐시 용량으로 부팅해도 **용량 가드를 켠다** — 봉투 선언 용량이 캐시와 다르면 모션 0 거부
  (조용한 2배·절반 토출 방지). 이어받기는 **감지 기종이 캐시 기종과 같을 때만**이고, **확정 설정만** 캐시에 기록한다.
  셋 다 부재·손상이면 None(종전 폴백 — 모드 기본 추정값이면 가드 OFF · Tecan 은 명시 저장 없으면 차단)이고 프로파일 자체를
  무효로 만들지 않는다(옛 캐시 호환). 온라인이 되면 설정 감시자가 서버 해시와 달라진 것을 보고 적용한다(04_erd §9-3 ·
  기종·주소 변경만 유휴 재시작, 나머지는 재시작 없이).

- serverBaseUrl 불일치 캐시는 무효 — URL 교체 재설치 시 옛 서버 선언 오용 방지(정체성
  저장과 같은 규약).
- 기록은 임시파일 + os.replace(원자적) — SD 전원단절에 부분 파일이 남지 않게.
- 손상/미지값 = 무효(None) — 추측 조립 금지.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_CACHE_FILENAME = "hardware-profile.json"
_VALID_MODELS = ("sy01b", "tecan_xcalibur")


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    pump_model: str  # "sy01b" | "tecan_xcalibur"
    pump_full_stroke: int
    valve_port_count: int
    sensorium_version: str | None = None  # 관측용 메타(판정에 안 씀)
    # 출처(2026-09-14) — "declared"(스냅샷/캐시 선언) | "detected"(부팅 실물 지문). 캐시엔 declared 만 저장한다
    #   (감지 결과를 캐시하면 펌프 전원이 늦게 켜진 오프라인 재부팅이 직전 랙의 기종으로 조립된다).
    source: str = "declared"
    # (2026-09-29) 설정상 펌프 주소(스냅샷 `pumpPorts` 키) — 오프라인 부팅의 프로브 대상 보조축. 빈 튜플 = 모름(모드 기본).
    pump_addrs: tuple[int, ...] = ()
    # (2026-09-29) 마지막 스냅샷의 AI 계약 · 시린지 용량 · 유효 튠(v·V·c·L) — 오프라인 추정 보조축(위 헤더).
    contract_id: str | None = None
    syringe_capacity_ml: float | None = None
    pump_tuning: "dict[str, int] | None" = None


def cache_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / _CACHE_FILENAME


def save_profile(state_dir: str | Path, profile: HardwareProfile, server_base_url: str) -> None:
    """원자적 기록(best-effort) — 실패는 삼킨다(캐시는 가용성 보조축, 부팅을 막지 않는다)."""
    try:
        path = cache_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pumpModel": profile.pump_model,
                    "pumpFullStroke": profile.pump_full_stroke,
                    "valvePortCount": profile.valve_port_count,
                    "sensoriumVersion": profile.sensorium_version,
                    "pumpAddrs": list(profile.pump_addrs),
                    "contractId": profile.contract_id,
                    "syringeCapacityMl": profile.syringe_capacity_ml,
                    "pumpTuning": profile.pump_tuning,
                    "serverBaseUrl": server_base_url,
                    "savedAt": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — 캐시 실패가 부팅·제조를 막으면 안 된다.
        pass


def load_profile(state_dir: str | Path, server_base_url: str) -> "HardwareProfile | None":
    """유효 캐시 로드 — 손상·미지 모델·타 서버 URL = None(무효). 판정은 엄격(추측 조립 금지)."""
    try:
        raw = json.loads(cache_path(state_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 부재/손상 = 무효.
        return None
    if not isinstance(raw, dict):
        return None
    # 정체성 저장(registration_client._same_server)과 같은 정규화 — 끝 슬래시 차이로 유효 캐시가
    #   무효 판정(→Undeclared 전 모션 거부)되면 안 된다(R6 P2-2).
    saved_url = raw.get("serverBaseUrl")
    if not isinstance(saved_url, str) or saved_url.rstrip("/") != server_base_url.rstrip("/"):
        return None
    model = raw.get("pumpModel")
    stroke = raw.get("pumpFullStroke")
    ports = raw.get("valvePortCount")
    if model not in _VALID_MODELS:
        return None
    if isinstance(stroke, bool) or not isinstance(stroke, int) or stroke <= 0:
        return None
    # 후보 열거 금지(2026-09-04) — 포트 수는 센소리움 선언값. 형식 sanity(1..64)만.
    if isinstance(ports, bool) or not isinstance(ports, int) or not 1 <= ports <= 64:
        return None
    sv = raw.get("sensoriumVersion")
    return HardwareProfile(
        pump_model=model,
        pump_full_stroke=stroke,
        valve_port_count=ports,
        sensorium_version=sv if isinstance(sv, str) else None,
        pump_addrs=_parse_pump_addrs(raw.get("pumpAddrs")),
        contract_id=_parse_contract_id(raw.get("contractId")),
        syringe_capacity_ml=_parse_capacity(raw.get("syringeCapacityMl")),
        pump_tuning=_parse_tuning(raw.get("pumpTuning")),
    )


_TUNING_KEYS = ("pumpMaxStartSpeedHz", "pumpMaxTopSpeedHz", "pumpMaxCutoffSpeedHz", "pumpMaxSlope")


def _parse_contract_id(raw: object) -> "str | None":
    return raw if isinstance(raw, str) and 0 < len(raw) <= 80 else None


def _parse_capacity(raw: object) -> "float | None":
    """9종 allowlist 밖·손상 = None(모드 기본 폴백). 판정은 pump_guard 정본."""
    from ..core.pump_guard import resolve_syringe_capacity_ml

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    v = resolve_syringe_capacity_ml(raw, is_flavor=True)
    return v if v == raw else None


def _parse_tuning(raw: object) -> "dict[str, int] | None":
    """네 축이 전부 양의 정수일 때만(반쪽 튠 금지). 범위 clamp 는 어댑터 조립이 한 번 더 한다."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for k in _TUNING_KEYS:
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            return None
        out[k] = v
    return out


_MAX_PUMP_ADDR = 15  # RS485 주소 1..15(0 = 브로드캐스트). 형식 sanity 만.


def _parse_pump_addrs(raw: object) -> tuple[int, ...]:
    """캐시 `pumpAddrs` → 주소 튜플. 부재(옛 캐시)·손상·범위 밖 = **빈 튜플**(모드 기본 폴백) — 부팅을 막지 않는다.

    하나라도 이상하면 통째로 버린다 — 일부만 살리면 "4대인데 3대로" 같은 반쪽 목록이 확정값처럼 쓰인다.
    """
    if not isinstance(raw, list):
        return ()
    out: set[int] = set()
    for a in raw:
        if isinstance(a, bool) or not isinstance(a, int) or not 1 <= a <= _MAX_PUMP_ADDR:
            return ()
        out.add(a)
    return tuple(sorted(out))
