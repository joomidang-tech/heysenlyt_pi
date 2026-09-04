"""pi settings read-only 소비 — 서버 MachineSettings → 시린지 용량·프리셋 파생(O-18).

부팅 **1회** 서버 GET-SSE(`/api/dispenser/settings?mode=`)로 내려오는 MachineSettings(이미
`settingsClamp.ts` 로 clamp 완료)를 **읽기 전용**으로 소비해 RecipeResolver 의 pump_map 에
쓸 시린지 용량/스트로크를 뽑는다. pi 는 settings 를 절대 수정·역보고하지 않는다
(정본 = 서버·단방향). 방어적 이중 clamp: 수신 프리셋도 `core.pump_guard.clamp_pump_preset`
로 한 번 더 통과시킨다(서버↔pi 바이트-parity 이므로 정상 입력에선 no-op — §11 O-17 결).

⚠️ **실시간 스왑 아님 — 부팅 스냅샷 1회**(감사 P2 최소 봉합·2026-07-18). 가동 중 운영자의 admin
   설정(syringeCapacityMl 등) 변경은 **재기동 시** pi 에 반영된다. 상시 SSE settings 구독(진행 중
   제조와 무경합 스왑)은 별도 웨이브(밸브 flowRate SoT 승격과 함께). 부팅 fetch 실패는
   **best-effort** — 서버 미제공/네트워크 오류 시 모드 기본 용량(0.5mL)/sy01b 스트로크로 폴백한다.

계약(heysenlyt-web `lib/server/settingsClamp.ts` MachineSettings — 읽기만·나머지 키 무시):
    {
      "pumpPreset": {                        # 단일 프리셋(양 모드 공통·SoT §6-1)
        "pumpPresetId": "sy01b",
        "pumpFullStroke": 12000,
        "syringeCapacityMl": 0.5,            # 9종 allowlist 밖 → 기본 0.5mL 폴백(양 모드 공통)
        ...프리셋 필드
      },
      "pumpPorts": { "1": {...}, "2": {...} } # 있는 펌프의 주소 키("1".."12")
    }
서버 SSE 프레임 data = `{"settings": <MachineSettings>}` — `fetch_settings_once` 가 벗겨 반환.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import urlencode

from ..config.server_target import ServerConfig
from ..core.pump_guard import (
    PUMP_PRESETS,
    PumpPreset,
    SyringeSpec,
    clamp_pump_preset,
    resolve_syringe_capacity_ml,
)
from .http_client import SseStream, bearer_headers, open_sse

if TYPE_CHECKING:
    from ..persistence.hardware_profile_cache import HardwareProfile

# 부팅 1회 settings SSE 타임아웃 — 서버는 subscribe 즉시 `event:settings` 를 push 하므로 짧게.
#   connect 는 느린 링크를 빨리 포기(best-effort), read 는 첫 프레임 도착 여유.
DEFAULT_SETTINGS_CONNECT_TIMEOUT_S = 5.0
DEFAULT_SETTINGS_READ_TIMEOUT_S = 8.0

# open_sse seam — (url, headers, timeout[, connect_timeout]) → SseStream. 테스트가 fake 주입.
OpenStream = Callable[..., SseStream]


def _preset_of(settings: Any) -> PumpPreset | None:
    """MachineSettings.pumpPreset → 방어적 재clamp된 PumpPreset. 부재/불량 → None."""
    if not isinstance(settings, Mapping):
        return None
    raw = settings.get("pumpPreset")
    if not isinstance(raw, Mapping):
        return None
    return clamp_pump_preset(raw)


def syringe_capacity_from_settings(settings: Any) -> float | None:
    """MachineSettings.pumpPreset.syringeCapacityMl → 검증된 용량(9종 allowlist·O-15). 부재/불량 → None.

    None = '서버 미제공' 신호 — 호출측이 모드 기본(0.5mL)으로 폴백한다(**스냅 아님**). 값이
    9종 밖이면 서버 clamp 와 동일하게 0.5mL 로 떨어진다(양 모드 공통·2026-07-17 확정).
    ⚠️ 용량이 실 시린지와 어긋나면 stepsPerMl 오산 → 과다흡입 → Code 11(펌프 파손). 서버가
       SoT 이므로 여기서 서버값을 우선 반영하는 것이 안전 급소다.
    """
    if not isinstance(settings, Mapping):
        return None
    raw = settings.get("pumpPreset")
    if not isinstance(raw, Mapping):
        return None
    cap = raw.get("syringeCapacityMl")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        return None
    # 폴백값은 모드 무관 0.5mL 이므로 is_flavor 인자는 결과에 영향 없다(시그니처 호환 유지).
    return resolve_syringe_capacity_ml(cap, is_flavor=True)


def pump_model_from_settings(settings: Any) -> "str | None":
    """settings.**hardware.pumpModel** → 펌프 모델 **엄격 판독**(2026-09-02 단일 키 설계).

    ⛔ 판독 채널은 `hardware`(기기 토큰 바인딩 SSE 에서만 서버가 주입하는 선언 블록) **단일**이다
    — `pumpPreset.pumpPresetId` 를 읽으면 안 된다(R6 P0-1). 그 필드는 함대 settings 문서의
    산술 축이라 **병합이 스킵돼도 항상 "sy01b" 가 채워져 있어**, "선언 없음"과 "sy01b 선언"을
    구분할 수 없다 → 병합 스킵(레지스트리 순단·레거시 토큰·기기문서 부재) 부팅이 Tecan 실물에
    sy01b 조립(U…R = XCalibur NVM 기록)으로 새고, 캐시까지 sy01b 로 오염된다. hardware 블록은
    선언이 실제로 병합됐을 때만 존재하므로 부재 = 정직한 "미확정"(→ 캐시 폴백 → Undeclared).

    ⛔ **clamp_pump_preset 미사용** — clamp 는 미지값을 sy01b 로 정규화하는데, 엔진 선택에서 그
    관대함은 같은 U-NVM 급소로 샌다. 정확 등가만 인정, 그 외/부재 = None. 신모델의 카탈로그
    등재는 그 모델을 지원하는 pi 릴리스와 동시에만 한다는 릴리스 규칙이 이 엄격성의 짝이다.

    하위호환: 구 서버(hardware 미주입)와의 조합은 None → 캐시 → Undeclared 로 착지한다 —
    롤아웃 규칙(web 선행 발행)이 이 조합을 봉쇄하고, 어겨도 무동작(복구 가능)이지 오조립이 아니다.
    """
    if not isinstance(settings, Mapping):
        return None
    hw = settings.get("hardware")
    if not isinstance(hw, Mapping):
        return None
    model = hw.get("pumpModel")
    if model in ("sy01b", "tecan_xcalibur"):
        return model
    return None


# 형식 sanity 상한 — 제품 후보가 아니라 '깨진 값' 차단선(밸브 헤드가 이 근처일 리 없다). 선언 SoT 는 서버.
_PORT_COUNT_SANITY_MAX = 64


def valve_port_count_from_settings(settings: Any) -> "int | None":
    """settings.hardware.valvePortCount(센소리움 파생·서버 주입 필드) → 선언된 포트 수. 부재/불량 = None(→12).

    ⚠️ 후보를 열거하지 않는다(2026-09-04 — 종전엔 허용값을 열거했다): 포트 수는 센소리움 선언이 정하는
    값이지 pi 가 미리 아는 목록이 아니다. 여기선 형식 sanity(양의 정수·물리적으로 말이 되는 범위)만.

    hardware 블록은 **settings 객체 내부**의 서버 주입 필드다(clamp allowlist 밖 — 기기 토큰
    바인딩 SSE 에서만 병합됨). 구 서버 스냅샷엔 없다 = None = 기존 12 거동(하위호환).
    """
    if not isinstance(settings, Mapping):
        return None
    hw = settings.get("hardware")
    if not isinstance(hw, Mapping):
        return None
    n = hw.get("valvePortCount")
    if isinstance(n, bool) or not isinstance(n, int):
        return None
    return n if 1 <= n <= _PORT_COUNT_SANITY_MAX else None


def full_stroke_from_settings(settings: Any) -> int | None:
    """MachineSettings.pumpPreset → clamp된 pumpFullStroke. 부재 → None(sy01b 12000 폴백 유도)."""
    preset = _preset_of(settings)
    return preset.pump_full_stroke if preset is not None else None


def hardware_profile_from_snapshot(model: str, settings: Any) -> "HardwareProfile":
    """선언 스냅샷 → 캐시용 HardwareProfile — **이 함수가 유일한 조립 지점**이다.

    stroke 폴백은 **선언된 모델의** 프리셋 기본(R6.5 P2) — sy01b 고정이면 "tecan 선언 +
    pumpPreset 부재" 프레임이 {tecan, 12000} 프로파일을 캐시에 남겨, 다음 오프라인 부팅이
    어댑터 3000 vs spec 12000 = 영구 -1001 로 죽는다. 이 규칙을 bootstrap 과 senlytd
    (_undeclared_refetch)가 각자 손으로 유지하다 한쪽만 어긋나는 걸 막기 위해 여기로 모았다.

    `model` 은 pump_model_from_settings 가 좁힌 값("sy01b"|"tecan_xcalibur")만 온다 —
    PUMP_PRESETS 미등재 키가 오면 KeyError 로 시끄럽게 죽는 게 맞다(추측 조립 금지).
    """
    from ..persistence.hardware_profile_cache import HardwareProfile

    hw = settings.get("hardware") if isinstance(settings, Mapping) else None
    sv = hw.get("sensoriumVersion") if isinstance(hw, Mapping) else None
    return HardwareProfile(
        pump_model=model,
        pump_full_stroke=full_stroke_from_settings(settings)
        or PUMP_PRESETS[model].pump_full_stroke,
        valve_port_count=valve_port_count_from_settings(settings) or 12,
        sensorium_version=str(sv) if sv is not None else None,
    )


def pump_addrs_from_settings(settings: Any) -> list[int]:
    """MachineSettings.pumpPorts 키("1".."12") → 펌프 주소(≥1) 오름차순.

    ⚠️ 이 목록은 '**설정상** 있어야 할 펌프'다 — 물리 존재는 확정하지 않는다. 실배선(build_resolver)
       은 주소를 **물리 프로브**(discover_pumps)로 확정하고, settings 는 그 위에 **용량**만 얹는다.
       (부재 주소를 config 만 보고 매핑하면 없는 펌프에 토출 명령이 나가는 over-trust — 프로브가 SoT.)
    """
    if not isinstance(settings, Mapping):
        return []
    ports = settings.get("pumpPorts")
    if not isinstance(ports, Mapping):
        return []
    addrs: list[int] = []
    for k in ports.keys():
        try:
            a = int(k)
        except (TypeError, ValueError):
            continue
        if a >= 1:  # addr 0 = RS485 브로드캐스트 — 실 주소 아님(배제).
            addrs.append(a)
    return sorted(set(addrs))


def pump_map_from_settings(
    settings: Any,
    *,
    addrs: "list[int] | None" = None,
    mode_is_flavor: bool = True,
) -> dict[int, SyringeSpec]:
    """MachineSettings → pumpAddr→SyringeSpec 매핑(직접 소비·테스트용).

    - 주소: `addrs` 인자 우선(실배선은 물리 프로브 결과를 넘긴다), 없으면 pumpPorts 키.
    - 용량: pumpPreset.syringeCapacityMl(없으면 모드 기본 0.5mL).
    - 스트로크: pumpPreset.pumpFullStroke(없으면 sy01b 12000).

    미매핑 addr 는 RecipeResolver 게이트가 drop 한다(silent 매핑 금지) — 여기선 물리 매핑만.
    """
    capacity = syringe_capacity_from_settings(settings)
    if capacity is None:
        capacity = resolve_syringe_capacity_ml(None, is_flavor=mode_is_flavor)
    stroke = full_stroke_from_settings(settings)
    if stroke is None:
        stroke = PUMP_PRESETS["sy01b"].pump_full_stroke
    use_addrs = addrs if addrs is not None else pump_addrs_from_settings(settings)
    spec = SyringeSpec(pump_full_stroke=stroke, syringe_capacity_ml=capacity)
    return {int(a): spec for a in sorted(set(use_addrs)) if int(a) >= 1}


def fetch_settings_once(
    server_config: ServerConfig,
    bearer_token: str,
    mode: str,
    *,
    open_stream: OpenStream = open_sse,
    connect_timeout: float = DEFAULT_SETTINGS_CONNECT_TIMEOUT_S,
    read_timeout: float = DEFAULT_SETTINGS_READ_TIMEOUT_S,
) -> "Mapping[str, Any] | None":
    """부팅 1회 서버 settings SSE 구독 → 첫 `settings` 이벤트의 MachineSettings 반환.

    서버 GET `/api/dispenser/settings?mode=` 는 subscribe 즉시 `event:settings{settings}` 를
    push 한다. **첫 프레임만** 읽고 스트림을 닫는다(실시간 스왑 아님 — 부팅 스냅샷). 실패
    (연결 거부·타임아웃·비-settings·JSON 오류)는 전부 **None**(best-effort — 모드 기본 용량 폴백).
    `open_stream` 은 테스트 주입 seam(네트워크 없이 검증).
    """
    url = f"{server_config.settings_stream_url}?{urlencode({'mode': mode})}"
    try:
        stream = open_stream(
            url,
            headers=bearer_headers(bearer_token),
            timeout=read_timeout,
            connect_timeout=connect_timeout,
        )
    except Exception:  # noqa: BLE001 — 연결/시작 실패는 best-effort None(폴백).
        return None
    try:
        for event, data in stream.events():
            if event != "settings":
                continue  # 서버 heartbeat 코멘트 등은 SseStream 이 이미 스킵.
            try:
                parsed = json.loads(data)
            except ValueError:
                return None
            if not isinstance(parsed, Mapping):
                return None
            inner = parsed.get("settings")
            return inner if isinstance(inner, Mapping) else None
        return None  # settings 이벤트 없이 스트림 종료.
    except Exception:  # noqa: BLE001 — 스트림 read 오류는 폴백.
        return None
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001 — close 실패는 삼킴(best-effort).
            pass
