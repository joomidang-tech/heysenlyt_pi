"""하드웨어 프로파일 로컬 캐시 (2026-09-02 — 센소리움 단일 SoT · 오프라인 부팅 폴백).

서버 스냅샷이 준 하드웨어 선언을 상태 디렉터리에 남겨, 다음 부팅에서 fetch 가 실패해도
같은 구성으로 조립한다(없으면 Undeclared fail-closed — bootstrap 상태모델 D3).

캐시가 SoT 로 삼는 축은 **{pumpModel, pumpFullStroke, valvePortCount} 셋뿐**이다(R-P0-4).
⛔ syringeCapacityMl 은 캐시하지 않는다 — 캐시 부팅에서 용량을 먹이면
`capacity_from_settings=True` 가 되어 스테일 캐시 vs 봉투 선언 불일치로 전건 오거부가
된다. 캐시 부팅 = 용량 미확정(모드 기본 0.5 가정·용량 가드 자동 OFF) = 기존 오프라인
거동과 동일 + WARN.

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
    if isinstance(ports, bool) or not isinstance(ports, int) or ports not in (12, 15):
        return None
    sv = raw.get("sensoriumVersion")
    return HardwareProfile(
        pump_model=model,
        pump_full_stroke=stroke,
        valve_port_count=ports,
        sensorium_version=sv if isinstance(sv, str) else None,
    )
