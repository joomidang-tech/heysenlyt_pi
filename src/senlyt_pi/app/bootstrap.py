"""senlytd 실 어댑터 조립(bootstrap) — 환경변수 → ServerConfig → 실 어댑터 결선.

정본: 02_infra §10 통합 E2E 토폴로지.

책임(스텁 제거·사용자 원칙 2026-07-10):
  - `SENLYT_ENV`/`SENLYT_SERVER_BASE_URL` → `ServerConfig`(base URL 단일 결정·fail-fast).
  - 등록(POST /api/dispensers/register·실 HTTP) → deviceId·dispenserToken 확보(파일 영속).
  - 실 어댑터 조립: SSE command/commandSet source + HTTP status sink(orders/heartbeat/trace/봉투전이).
  - **엔진은 서버 선언(센소리움 pumpModel)대로 실물 어댑터** — 호스트가 Pi 든 맥북이든 같다(2026-09-04).
    FakeEnginePort 는 테스트·도커 E2E 가 `SENLYT_FAKE_ENGINE=1` 로 명시할 때만(실 Pi 에선 거부).

⚠️ 이 모듈은 **결선(wiring)만** 한다 — 실제 펌프 소비 루프(SSE→멱등→Sequencer→역보고 상시 구동)는
   안전상 daemon.boot 유보를 유지한다. bootstrap 은 어댑터를 실체로 만들어 DaemonDeps 로 묶는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 타입 전용.
    from ..adapters.pump_model_detect import Detector

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
from ..adapters.fake_engine_adapter import FakeEnginePort
from ..adapters.valve_adapter import (
    DEFAULT_FLOW_ML_PER_SEC,
    DEFAULT_MAX_OPEN_SEC,
    DEFAULT_VALVE_PINS,
    FakeValveAdapter,
)
from ..adapters.http_status_sink_adapter import HttpStatusSinkAdapter
from ..adapters.registration_client import (
    RegistrationClient,
    ensure_registered,
    make_http_register_transport,
    read_hardware_id,
)
from ..adapters.settings_source import (
    expected_pump_addrs,
    expected_pump_addrs_with_source,
    fetch_settings_once,
    full_stroke_from_settings,
    pump_addrs_from_settings,
    pump_tuning_from_settings,
    syringe_capacity_from_settings,
)
from ..adapters.sse_command_source_adapter import SseCommandSourceAdapter
from ..config.server_target import ServerConfig
from ..core.pump_guard import (
    PUMP_PRESETS,
    PumpPreset,
    SyringeSpec,
    apply_pump_tuning,
    SUPPORTED_SYRINGE_CAPACITIES_ML,
    REQUIRES_EXPLICIT_SYRINGE_CAPACITY,
    is_supported_syringe_capacity,
    resolve_syringe_capacity_ml,
)
from ..obs.log import STAGE_ERROR, STAGE_PI_RECEIVED, StructuredLogger
from ..persistence.file_idempotency_ledger import FileIdempotencyLedger
from ..persistence.hardware_profile_cache import HardwareProfile
from ..persistence.idempotency_ledger import IdempotencyLedger, InMemoryIdempotencyLedger
from ..pipeline.pump_health import auto_pump_map, discover_pumps
from ..pipeline.trace_spill import TraceSpill
from ..pipeline.recipe_resolver import RecipeResolver
from ..ports.engine_port import EnginePort
from ..ports.valve_port import ValvePort

# 엔진 선택 env(override) — 미지정이면 **자동감지**(실 Pi+시리얼 어댑터→sy01b·아니면 fake). 02_infra §10.
#   설치 시 안 넣어도 됨("URL만"). 명시하면 그 값 우선(fake|sy01b|tecan) — E2E/개발 고정용.
#   tecan(=tecan_xcalibur|xcalibur) 은 Cavro XCalibur 실물 전용 — 자동감지로는 절대 선택되지 않는다.
SENLYT_ENGINE_ENV = "SENLYT_ENGINE"
# ⛔ 테스트·도커 E2E 전용 — 물리 엔진을 FakeEnginePort 로 바꾸는 **명시** 스위치(2026-09-04).
#   종전 "GPIO 없는 호스트 = 자동 fake" 규칙을 폐기하면서 도입. 자동 fake 는 맥북 같은 개발기에서
#   서버 선언(Tecan)을 무시하고 가짜 엔진을 올려 "펌프 0개" 로 보였다(실측). 이제 엔진은 호스트와
#   무관하게 서버 선언대로 실물 어댑터를 조립하고, fake 는 이 키를 켠 곳에서만 나온다.
#   실 Pi(GPIO 존재)에서는 켜도 거부(BootstrapError) — 실기기에서 가짜 토출 보고는 최악의 사고.
SENLYT_FAKE_ENGINE_ENV = "SENLYT_FAKE_ENGINE"
# pi 실행 모드(주문 큐 mode·flavor|fragrance) — 어느 컬렉션/큐를 구독·역보고할지.
#   ⚠️ TOFU 후 **서버 배정(identity.mode)이 우선** — 이 env 는 서버 미배정 시 폴백일 뿐(더 이상 필수 아님).
SENLYT_MODE_ENV = "SENLYT_MODE"
# 정체성 파일 경로 override(기본 = LOG_DIR 또는 작업 디렉터리).
SENLYT_IDENTITY_PATH_ENV = "SENLYT_IDENTITY_PATH"
# 매장 표시 이름(선택) — register name.
SENLYT_DEVICE_NAME_ENV = "SENLYT_DEVICE_NAME"
# 멱등 ledger 파일 경로 override(기본 = LOG_DIR 또는 작업 디렉터리).
SENLYT_LEDGER_PATH_ENV = "SENLYT_LEDGER_PATH"
# 관측 로그 디스크 스풀 파일 경로 override(기본 = LOG_DIR 또는 작업 디렉터리).
#   단절 중 전송 실패한 trace 배치를 보존 → 재연결 시 전량 업로드(유실 0 · 2026-07-19).
SENLYT_TRACE_SPILL_PATH_ENV = "SENLYT_TRACE_SPILL_PATH"
# 펌프 addr 배치(모드별) — 예: "aroma:1,2,3;flavor:4" (E2E 02_infra §10 pi 서비스 env).
SENLYT_PUMP_ADDRESSES_ENV = "PUMP_ADDRESSES"
# 기주 밸브 선택 env(override·§9-1 v2) — 미지정이면 **자동감지**(실 Pi→gpio·아니면 fake).
#   설치 시 안 넣어도 됨("URL만"). 명시: fake(시뮬) | gpio(실기기·명시 시 결선실패=fail-fast) | off(미결선 drop).
SENLYT_VALVE_ENV = "SENLYT_VALVE"
# 밸브 핀 매핑(BCM) — 기본 "sour:17,normal:27" (신기주=BCM17/물리핀11·베이스=BCM27/물리핀13·2026-07-17 실배선 정정).
SENLYT_VALVE_PINS_ENV = "SENLYT_VALVE_PINS"
# 밸브 유량(mL/s) — openSec = volumeMl ÷ 이 값. 벤치 캘리브레이션으로 교체(기본 10.0).
SENLYT_VALVE_FLOW_ENV = "SENLYT_VALVE_FLOW_ML_PER_SEC"
# 최대 개방 클램프(s) — 밸브 영구개방 차단(기본 15.0).
SENLYT_VALVE_MAX_OPEN_ENV = "SENLYT_VALVE_MAX_OPEN_SEC"

# 상태(정체성 등) 기본 디렉터리 override — install.sh 가 /var/lib/senlyt 를 각인(로그 dir 과 분리).
SENLYT_STATE_DIR_ENV = "SENLYT_STATE_DIR"

DEFAULT_IDENTITY_FILENAME = "device-identity.json"
# 환경별 정체성 파일 하위 디렉터리 — 서버(환경)마다 자기 신분증을 따로 둔다(2026-07-23).
#   서버를 바꿔 재설치해도 각 서버의 등록·승인이 보존돼, 돌아오면 재승인·왕복 없이 즉시 재사용.
IDENTITIES_SUBDIR = "identities"
DEFAULT_LEDGER_FILENAME = "idempotency-ledger.log"
DEFAULT_TRACE_SPILL_FILENAME = "trace-spill.jsonl"


class BootstrapError(Exception):
    """부팅 조립 실패 — deviceId(수집 시리얼) 부재·서버 타겟 미설정·등록 실패 등(fail-fast)."""


# 부팅 1회 settings fetch seam(주입 가능·테스트가 네트워크 없이 검증) —
#   (server_config, dispenser_token, mode) → MachineSettings|None. 기본 = 실 SSE 1회 읽기.
SettingsFetcher = Callable[[ServerConfig, str, str], "Mapping[str, Any] | None"]


@dataclass(frozen=True, slots=True)
class DaemonComponents:
    """조립된 실 어댑터 묶음 — daemon 이 소비."""

    device_id: str
    server_config: ServerConfig
    identity: DeviceIdentity
    command_source: SseCommandSourceAdapter
    status_sink: HttpStatusSinkAdapter
    engine: EnginePort
    valve: ValvePort | None
    ledger: IdempotencyLedger
    logger: StructuredLogger
    # 서버 배정/env 로 확정된 실행 모드(flavor|fragrance) — 구독·역보고 축 + settings mode 쿼리.
    mode: str
    # 부팅 1회 서버 settings 스냅샷(시린지 용량 SoT) — fetch_settings=True 일 때만 채워짐(없으면 None).
    #   RecipeResolver pump_map 의 용량/스트로크를 서버값으로 얹는다(build_resolver 소비).
    server_settings: "Mapping[str, Any] | None" = None
    # 하드웨어 선언(2026-09-02 단일 SoT) — 스냅샷(엄격 판독) > 로컬 캐시 > None(Undeclared).
    #   None 이면 engine 은 UndeclaredEngineAdapter(모션 거부)로 조립돼 있다.
    hardware_profile: "HardwareProfile | None" = None
    # 하드웨어 출처 관측 — "detected"(부팅 실물 지문·2026-09-14 1순위) | "snapshot" | "cache" |
    #   "undeclared" | "undetected"(응답은 있는데 지문 판독 불가) | "mixed"(기종 혼합). 뒤 셋은 Undeclared 조립.
    hardware_source: str = "undeclared"
    # 부팅 감지에서 `?` 에 응답한 주소(2026-09-14) — build_resolver 가 2차 스캔 없이 그대로 pump_map 으로 쓴다
    #   ("감지 기종과 pump_map 이 같은 관측에서 나온다" + 부재 주소 프로브 상한 낭비 0). None = 감지 안 함.
    detected_pump_addrs: "tuple[int, ...] | None" = None


def _resolve_mode(environ: Mapping[str, str]) -> str:
    mode = environ.get(SENLYT_MODE_ENV, "").strip().lower()
    return "fragrance" if mode == "fragrance" else "flavor"


def _is_truthy(raw: str | None) -> bool:
    """env 불리언("1"/"true"/"yes"/"on" — 대소문자 무시). senlytd 의 동명 헬퍼와 같은 판정."""
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _gpio_available() -> bool:
    """실 라즈베리파이 GPIO 존재 여부 — **Pi4(`/dev/gpiomem`)·Pi5(`/dev/gpiomem0`·RP1) 모두 커버**.

    **기주 밸브(GPIO) 자동감지에만 쓴다**(2026-09-04) — 비-Pi(CI·dev·docker·맥북)는 gpiomem 이 없어
    False → 밸브 없음(off). 펌프 엔진은 이 판정과 무관하게 서버 선언대로 조립한다(시리얼만 있으면 됨).
    이 값이 True 인 호스트에서는 SENLYT_FAKE_ENGINE 도 거부한다(실기기 가짜 엔진 금지).
    ⚠️ Pi5 는 RP1 칩이라 `/dev/gpiomem` 이 아니라 `/dev/gpiomem0`(뱅크별 gpiomem0..4) — glob 로 둘 다 잡는다
    (`/dev/gpiomem` 단일 경로만 보면 Pi5 에서 gpio 자동감지가 fake 로 오판·2026-07-17 실기기 발견).
    """
    from glob import glob

    return bool(glob("/dev/gpiomem*"))


def build_engine(
    environ: Mapping[str, str],
    *,
    engine: EnginePort | None = None,
    on_pi: Callable[[], bool] | None = None,
    port_lister: "Callable[[], list] | None" = None,
    estop_event: "threading.Event | None" = None,
    logger: StructuredLogger | None = None,
    # 서버 선언 펌프 모델(2026-09-02 단일 키) — "sy01b"|"tecan_xcalibur"|None.
    #   None(선언 미확정·실 Pi) = UndeclaredEngineAdapter(모션 거부·fail-closed).
    pump_model: "str | None" = None,
    # Undeclared 착지 사유(2026-09-14) — 선언 부재 외에 "지문 판독 불가"·"기종 혼합" 도 같은 fail-closed.
    undeclared_detail: "str | None" = None,
    # 운영자 속도 튠이 얹힌 어댑터 preset(§6-3a · 2026-09-22) — pump_tuning_from_settings 가 **실물 기종과
    #   같은 기종**으로 만든 값만 온다. None = 그 기종 제조사 기본값으로 조립(둘째 판). 스트로크·U 는 표 값 그대로다.
    pump_preset: "PumpPreset | None" = None,
) -> EnginePort:
    """엔진 조립 — 주입 우선. 호스트와 무관하게 **서버 선언(pump_model)** 대로 실물 어댑터를 조립한다.

    - 선언 sy01b/tecan_xcalibur → 그 어댑터(시리얼 포트는 자동 탐지·미탐지면 어댑터 기본 경로).
    - 선언 없음 → UndeclaredEngineAdapter(모션 거부·fail-closed).
    - `SENLYT_FAKE_ENGINE=1`(테스트·E2E 전용) → FakeEnginePort. 실 Pi(GPIO 존재)에서는 거부.
    2026-09-04 이전의 "비-Pi 면 자동 fake" 는 폐기 — 맥북 같은 개발기도 펌프를 꽂으면 실물로 돌고,
    안 꽂으면 탐색 결과대로 "미연결"이 정직하게 보고된다. GPIO 유무는 기주 밸브(build_valve)만 가른다.
    `on_pi`·`port_lister` 는 테스트 주입 seam(기본 = 실 판정).
    """
    if engine is not None:
        return engine
    from ..adapters.serial_port_discovery import discover_serial_port

    # ── 단일 키 설계(2026-09-02) — 펌프 모델은 **서버 선언(pump_model 인자)** 이 정한다. ──
    #   SENLYT_ENGINE env 는 은퇴(잔재 = 무시 + deprecated WARN 1릴리스 → install.sh 가 strip).
    #   선언은 부팅 스냅샷(엄격 판독) > 로컬 캐시 순으로 build_components 가 해석해 넘긴다.
    raw = environ.get(SENLYT_ENGINE_ENV)
    if raw is not None and raw.strip() != "" and logger is not None:
        logger.warn(
            f"SENLYT_ENGINE={raw.strip()} 은 폐기된 키 — 무시됨(펌프 모델은 admin 센소리움 버전이"
            " 결정·부팅 스냅샷으로 수신). 재설치(install.sh)가 이 키를 제거합니다",
            stage=STAGE_PI_RECEIVED,
        )
    # 명시 fake(테스트·E2E) — 실 Pi 에서는 거부(가짜 엔진이 실기기 위에서 "토출 완료"를 보고하는 사고 차단).
    if _is_truthy(environ.get(SENLYT_FAKE_ENGINE_ENV)):
        is_pi = on_pi() if on_pi is not None else _gpio_available()
        if is_pi:
            raise BootstrapError(
                f"{SENLYT_FAKE_ENGINE_ENV} 는 실 Pi(GPIO 존재)에서 허용되지 않습니다 — "
                "테스트·도커 E2E 전용 스위치입니다. 키를 제거하고 다시 시작하세요."
            )
        return FakeEnginePort(estop_event=estop_event)
    if pump_model in ("sy01b", "tecan_xcalibur"):
        # 실 RS485 어댑터의 probe/dispense 는 hw-dev 워크오더(실 시리얼). 스텁이면 self-test 가 미준비를
        # 표면화(fail-closed) — 부팅·등록 자체는 허용(제조 트래픽만 보류).
        if pump_model == "sy01b":
            from ..adapters.sy01b_engine_adapter import Sy01bEngineAdapter as _RealAdapter
        else:
            from ..adapters.tecan_xcalibur_engine_adapter import (
                TecanXCaliburEngineAdapter as _RealAdapter,
            )

        port = discover_serial_port(environ, port_lister=port_lister)
        # ⚠️ estop_event 주입 = 데몬·시퀀서와 **같은 공유 래치**(§9-4). 이게 있어야 어댑터의 in-flight
        #   모션 폴이 데몬이 세운 래치를 직접 보고 즉시 bail 한다(설계 '단일 공유 _estop').
        #   port=None(미탐지) 이면 어댑터 기본값(/dev/ttyUSB0) 유지 — None 을 넘기지 않는다.
        from ..adapters.serial_port_discovery import list_candidate_ports

        # 핫플러그 자가 회복 seam(2026-07-19) — 장치 소멸 시 어댑터가 후보 포트를 재열거해
        #   재오픈한다(ttyUSB0→ttyUSB1 이동 실측·재시작 불필요). env override 도 그대로 존중.
        def _resolve_ports() -> list[str]:
            return list_candidate_ports(environ, port_lister=port_lister)

        # 튠 preset 은 기종 교차 방지 — 호출측이 실물 기종으로 만들었어도 여기서 한 번 더 대조한다
        #   (다른 기종의 상한을 이 어댑터에 꽂는 조합을 문법적으로 차단 · P1-6 정신).
        #   None(스냅샷 부재·캐시 부팅·기종 불일치) = 그 기종 **제조사 기본값**(apply_pump_tuning(표, None) —
        #   §6-3a 둘째 판 2026-09-22). 표 상한을 그대로 어댑터에 꽂던 첫 판 동작은 폐기.
        preset = (
            pump_preset
            if pump_preset is not None and pump_preset.pump_preset_id == pump_model
            else apply_pump_tuning(PUMP_PRESETS[pump_model], None)
        )
        if port:
            return _RealAdapter(
                port=port,
                estop_event=estop_event,
                logger=logger,
                port_resolver=_resolve_ports,
                preset=preset,
            )
        return _RealAdapter(
            estop_event=estop_event, logger=logger, port_resolver=_resolve_ports, preset=preset
        )
    # 선언 미확정(None/미지값) — Undeclared fail-closed(추측 조립 금지 · 상태모델 D3). 호스트 무관.
    #   ⛔ 폴백 sy01b 금지: tecan 이라 선언됐던 기기가 미확정 부팅에서 sy01b 로 조립되면
    #   초기화 프리앰블 U…R 이 XCalibur NVM 에 기록된다(undeclared_engine_adapter 헤더).
    from ..adapters.undeclared_engine_adapter import UndeclaredEngineAdapter

    if logger is not None:
        logger.warn(
            undeclared_detail
            or "하드웨어 선언 미확정 — 모션 거부 어댑터로 부팅(스냅샷·캐시 모두 무효). "
            "네트워크/admin 센소리움 배정 확인 후 재시작 필요",
            stage=STAGE_PI_RECEIVED,
        )
    return UndeclaredEngineAdapter(detail=undeclared_detail)


def _valve_pins_from_env(raw: str | None) -> dict[str, int]:
    """`SENLYT_VALVE_PINS`("sour:17,normal:27") → base→BCM 핀 매핑. 파싱 불가 항목은 건너뜀."""
    if not raw:
        return dict(DEFAULT_VALVE_PINS)
    pins: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        base, pin = part.split(":", 1)
        base = base.strip().lower()
        pin = pin.strip()
        if base and pin.isdigit():
            pins[base] = int(pin)
    return pins if pins else dict(DEFAULT_VALVE_PINS)


def _float_env(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def build_valve(
    environ: Mapping[str, str],
    *,
    valve: "ValvePort | None" = None,
    on_pi: Callable[[], bool] | None = None,
) -> ValvePort | None:
    """기주 밸브 조립(§9-1 v2) — 주입 우선. **env 미지정이면 자동감지**(GPIO 있음 → gpio·없음 → off).

    설치 시 `SENLYT_VALVE` 를 안 넣어도 된다("URL만" 목표) — GPIO(gpiomem)가 있으면 gpio, 없으면 **없음(None)**.
      - 2026-09-04: 종전 "비-Pi 는 fake" 폐기. GPIO 가 없는 호스트에서 가짜 밸브가 "열렸다"고 답하면
        기주가 안 나왔는데 성공 보고가 된다. 없으면 없다고 두고 밸브 스텝은 Sequencer pre-flight 가
        fail-closed drop(토출 0) — 음료 모드는 "밸브 미결선"이 드러나고 향수 모드는 밸브가 원래 없어 무영향.
      - 자동 gpio 결선 실패(gpiozero 부재 등)도 같은 이유로 **None**(자동 선택이라 부팅 중단은 없음).
      - fake 는 `SENLYT_VALVE=fake` 명시(테스트·E2E)일 때만.
      - 명시 `gpio` 는 결선 실패 시 **fail-fast**(BootstrapError) — 운영자가 콕 집었으니 조용히 넘어가지 않음.
      - `off`: None — valve 스텝 수신 시 Sequencer pre-flight 가 fail-closed drop(토출 0).
    """
    if valve is not None:
        return valve
    flow = _float_env(environ, SENLYT_VALVE_FLOW_ENV, DEFAULT_FLOW_ML_PER_SEC)
    max_open = _float_env(environ, SENLYT_VALVE_MAX_OPEN_ENV, DEFAULT_MAX_OPEN_SEC)

    def _gpio() -> "GpioValveAdapter":
        from ..adapters.valve_adapter import GpioValveAdapter

        return GpioValveAdapter(
            pins=_valve_pins_from_env(environ.get(SENLYT_VALVE_PINS_ENV)),
            flow_ml_per_sec=flow,
            max_open_sec=max_open,
        )

    raw = environ.get(SENLYT_VALVE_ENV)
    if raw is None or raw.strip() == "":
        # 자동감지 — GPIO 있으면 gpio(결선 실패 시 None), 없으면 None(밸브 없음·가짜 금지).
        is_pi = on_pi() if on_pi is not None else _gpio_available()
        if is_pi:
            try:
                return _gpio()
            except Exception:  # noqa: BLE001 — 자동 선택 실패는 "밸브 없음"(부팅 중단 없음·가짜 없음).
                return None
        return None

    choice = raw.strip().lower()
    if choice == "off":
        return None
    if choice == "gpio":
        try:
            return _gpio()
        except Exception as e:  # 명시 선택 — fail-fast(잘못된 핀으로 조용히 뜨는 것 방지).
            raise BootstrapError(f"GPIO 밸브 결선 실패: {e}") from e
    return FakeValveAdapter(flow_ml_per_sec=flow, max_open_sec=max_open)


def _server_slug(server_base_url: str) -> str:
    """서버 base URL → 파일명 안전 slug(호스트 기준). 환경별 정체성 파일 분리 키(2026-07-23).

    예: https://dev-env.senlyt.com → "dev-env.senlyt.com" · https://senlyt.com/ → "senlyt.com".
    영숫자·`.`·`-` 만 남기고 나머지는 `_`(포트 `:`·경로 `/` 등). 빈 값이면 "default".
    """
    from urllib.parse import urlsplit

    s = server_base_url.strip()
    parsed = urlsplit(s if "://" in s else f"//{s}")
    host = (parsed.netloc or parsed.path).strip("/").lower()
    slug = "".join(c if (c.isalnum() or c in ".-") else "_" for c in host)
    return slug or "default"


def _identity_path(environ: Mapping[str, str], server_base_url: str | None = None) -> Path:
    """정체성 파일 경로. 우선순위:
    1) SENLYT_IDENTITY_PATH 명시 → 그대로(단일 파일 override·하위호환).
    2) server_base_url 있으면 → {state}/identities/{slug}.json (**환경별 분리** — 서버마다 신분증 따로).
    3) 서버 미상 → {state}/device-identity.json (구 단일 파일 폴백).
    state = SENLYT_STATE_DIR > LOG_DIR > cwd.
    """
    explicit = environ.get(SENLYT_IDENTITY_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit)
    state_dir = environ.get(SENLYT_STATE_DIR_ENV, "").strip()
    log_dir = environ.get("LOG_DIR", "").strip()
    base = Path(state_dir) if state_dir else (Path(log_dir) if log_dir else Path.cwd())
    if server_base_url:
        return base / IDENTITIES_SUBDIR / f"{_server_slug(server_base_url)}.json"
    return base / DEFAULT_IDENTITY_FILENAME


def _ledger_path(environ: Mapping[str, str]) -> Path:
    explicit = environ.get(SENLYT_LEDGER_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit)
    log_dir = environ.get("LOG_DIR", "").strip()
    base = Path(log_dir) if log_dir else Path.cwd()
    return base / DEFAULT_LEDGER_FILENAME


def _trace_spill_path(environ: Mapping[str, str]) -> Path:
    """관측 로그 스풀 경로 — ledger/identity 와 같은 우선순위(override > LOG_DIR > cwd)."""
    explicit = environ.get(SENLYT_TRACE_SPILL_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit)
    log_dir = environ.get("LOG_DIR", "").strip()
    base = Path(log_dir) if log_dir else Path.cwd()
    return base / DEFAULT_TRACE_SPILL_FILENAME


def build_ledger(environ: Mapping[str, str]) -> FileIdempotencyLedger:
    """crash-safe 파일 멱등 ledger 조립 — 상시 소비 루프의 IL-02/CR-01 물리 보증.

    경로: `SENLYT_LEDGER_PATH` > `LOG_DIR`/idempotency-ledger.log > cwd/idempotency-ledger.log.
    (InMemoryIdempotencyLedger 는 mark_running/recovery 스캔 미지원 — 실 루프엔 파일 ledger.)
    """
    return FileIdempotencyLedger.open(_ledger_path(environ))


def pump_map_from_addresses_env(
    raw: str | None,
    *,
    capacity_override: float | None = None,
    full_stroke_override: int | None = None,
) -> dict[int, SyringeSpec]:
    """`PUMP_ADDRESSES`("aroma:1,2,3;flavor:4") → pumpAddr→SyringeSpec 매핑(RR pump_map).

    명시 env 로 주소를 고정하는 경로 — 용량/스트로크는 **서버 settings 오버라이드 우선**(O-18):
      - `capacity_override`(서버 settings 프리셋 용량)가 있으면 그 값, 없으면 모드 기본 0.5mL
        (양 모드 공통·2026-07-17 확정). ⚠️ 용량 오류 = Code 11 과다흡입이라 서버값이 안전 SoT.
      - `full_stroke_override`(서버 프리셋 스트로크)가 있으면 그 값, 없으면 sy01b(12000).
    누락/비정수 addr 는 건너뛴다(미매핑 addr 는 RR 게이트가 drop — silent 매핑 금지).
    """
    pump_map: dict[int, SyringeSpec] = {}
    if not raw:
        return pump_map
    stroke = (
        full_stroke_override
        if full_stroke_override is not None
        else PUMP_PRESETS["sy01b"].pump_full_stroke
    )
    for group in raw.split(";"):
        group = group.strip()
        if not group or ":" not in group:
            continue
        mode, addrs = group.split(":", 1)
        is_flavor = mode.strip().lower() == "flavor"
        capacity = (
            capacity_override
            if capacity_override is not None
            else resolve_syringe_capacity_ml(None, is_flavor=is_flavor)  # 모드 기본값 폴백.
        )
        spec = SyringeSpec(pump_full_stroke=stroke, syringe_capacity_ml=capacity)
        for a in addrs.split(","):
            a = a.strip()
            # ⛔ addr 0 = RS485 브로드캐스트 — pump_map 에 넣으면 어댑터가 `/0…`(전 펌프 동시
            #   응답·충돌)을 쏜다. 서버 게이트와 대칭으로 0 을 배제(실 주소는 1.. 뿐).
            if a.isdigit() and int(a) >= 1:
                pump_map[int(a)] = spec
    return pump_map


def capacity_block_for(model: "str | None", capacity_ml: "float | None", capacity_source: str) -> "str | None":
    """기종 지원 용량 판정(2026-09-30 · Tecan = 1·5mL) — 목록 밖이면 **모든 모션 거부** 사유, 통과면 None.

    부팅 조립(build_resolver)과 핫 적용(derive_hot_settings)이 같은 판정을 쓴다(한 곳). 모드 기본 추정(default)은 **명시 저장이
    필요한 기종(Tecan)** 에서만 "모름"으로 막는다 — SY-01B 는 추정 0.5 가 지원 목록 안이라 종전대로 돈다.
    """
    unknown = capacity_source == "default" and str(model) in REQUIRES_EXPLICIT_SYRINGE_CAPACITY
    if is_supported_syringe_capacity(model, None if unknown else capacity_ml):
        return None
    supported = SUPPORTED_SYRINGE_CAPACITIES_ML.get(str(model), ())
    return (
        f"시린지 용량 미지원 — 기종 {model} 지원 {list(supported)}mL, "
        f"실제 {'미확인(저장 안 됨)' if unknown else capacity_ml}mL(출처 {capacity_source}). "
        "admin 설정에서 실제 시린지 용량을 저장하면 재시작 없이 풀립니다"
    )


@dataclass(frozen=True)
class HotSettings:
    """재시작 없이 적용할 설정 한 벌(2026-09-30 · 04_erd §9-3) — 스냅샷 1장에서 파생한 값. 데몬이 유휴일 때 원자 교체한다."""

    pump_map: "dict[int, SyringeSpec]"
    valve_port_count: int
    port_liquids: "dict[int, dict[int, str | None]] | None"
    alcohol_strict: bool
    capacity_ml: float
    capacity_block: "str | None"
    engine_preset: "Any"
    contract_id: "str | None"
    capacity_changed: bool


def derive_hot_settings(
    settings: Any,
    *,
    pump_map: "Mapping[int, SyringeSpec]",
    pump_model: "str | None",
    mode: "str | None",
) -> HotSettings:
    """스냅샷 → 재시작 없이 적용할 값(순수). `pump_map` 의 주소·스트로크를 그대로 쓰고 용량만 스냅샷 값으로 바꾼다
    (기종·주소가 바뀐 경우 호출자가 새 어댑터의 주소·스트로크로 만든 `pump_map` 을 넘긴다)."""
    from ..adapters.settings_source import alcohol_carrier_rule_from_settings, valve_port_count_from_settings
    from ..adapters.settings_watcher import contract_id_from_settings
    from ..core.pump_guard import PUMP_PRESETS as _PP

    snap_cap = syringe_capacity_from_settings(settings)
    source = "snapshot" if snap_cap is not None else "default"
    cap = (
        snap_cap
        if snap_cap is not None
        else resolve_syringe_capacity_ml(None, is_flavor=(str(mode or "").lower() == "flavor"))
    )
    new_map = {
        addr: SyringeSpec(pump_full_stroke=spec.pump_full_stroke, syringe_capacity_ml=cap)
        for addr, spec in pump_map.items()
    }
    changed = any(abs(spec.syringe_capacity_ml - cap) > 1e-9 for spec in pump_map.values())
    table, strict = alcohol_carrier_rule_from_settings(settings)
    preset = None
    if pump_model in _PP:
        preset = pump_tuning_from_settings(settings, pump_model) or _PP[pump_model]
    return HotSettings(
        pump_map=new_map,
        valve_port_count=valve_port_count_from_settings(settings) or 12,
        port_liquids=table,
        alcohol_strict=strict,
        capacity_ml=cap,
        capacity_block=capacity_block_for(pump_model, cap, source),
        engine_preset=preset,
        contract_id=contract_id_from_settings(settings),
        capacity_changed=changed,
    )


class SettingsHotApplyEnv:
    """설정 무재시작 적용 재료(2026-09-30 · 04_erd §9-3) — 데몬이 유휴일 때 부른다.

    부팅 조립과 **같은 함수**(선언 기종·주소 파생 · 캐시 규칙)를 쓴다 — 적용 경로가 따로 놀지 않게. 어댑터 재조립은 여기서 하지 않는다
    (펌프 기종·주소 변경 = 재시작한 부팅이 조립).
    """

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        mode: "str | None",
        server_base_url: str,
    ) -> None:
        self.environ = environ
        self.mode = mode
        self.server_base_url = server_base_url
        self.state_dir = environ.get(SENLYT_STATE_DIR_ENV, "").strip() or environ.get("LOG_DIR", "").strip()

    def target_model(self, settings: Any) -> "str | None":
        from ..adapters.settings_source import pump_model_from_settings

        return pump_model_from_settings(settings)

    def target_addrs(self, settings: Any) -> list[int]:
        return pump_addrs_from_settings(settings)

    def persist(self, settings: Any, model: "str | None") -> None:
        """확정된 설정만 오프라인 캐시에 기록(부팅과 같은 규칙)."""
        from ..adapters.settings_source import hardware_profile_from_snapshot, snapshot_settings_confirmed
        from ..persistence.hardware_profile_cache import save_profile

        if not self.state_dir or not model or not snapshot_settings_confirmed(settings):
            return
        save_profile(self.state_dir, hardware_profile_from_snapshot(model, settings), self.server_base_url)


def build_resolver(
    environ: Mapping[str, str],
    *,
    engine: EnginePort | None = None,
    server_settings: "Mapping[str, Any] | None" = None,
    mode: str | None = None,
    # 하드웨어 선언(2026-09-02 단일 SoT) — 캐시 부팅 시 stroke·포트 상한의 공급원(R-P0-4:
    #   스냅샷 부재여도 캐시 stroke 로 pump_map 을 맞춰 영구 -1001 을 막는다). 용량은 비캐시.
    hardware_profile: "HardwareProfile | None" = None,
    # 부팅 감지가 이미 찾은 응답 주소(2026-09-14) — 주어지면 2차 프로브(discover_pumps) 를 생략한다.
    known_pump_addrs: "Sequence[int] | None" = None,
) -> RecipeResolver:
    """RecipeResolver 조립 — pump_map 을 **자동인식**하고, env 가 있으면 그게 이긴다.

    우선순위(주소):
      ① `PUMP_ADDRESSES` env 명시 → 그대로(고정 구성·기존 설치 호환).
      ② 미지정 → **버스 스캔 자동인식** — 단 **모드가 알려주는 예상 주소만** 프로브한다.
      ③ 둘 다 실패 → 빈 매핑(모든 스텝 unmapped drop = 토출 0·안전측).

    ⚠️ **시린지 용량/스트로크 = 서버 settings 우선(O-18·안전 급소)**. `server_settings`(부팅 1회
       GET-SSE 스냅샷)의 pumpPreset.syringeCapacityMl/pumpFullStroke 이 있으면 그 값을 pump_map
       SyringeSpec 에 얹고, 없으면 모드 기본(0.5mL/sy01b 12000)으로 폴백한다. 용량이 실 시린지와
       어긋나면 stepsPerMl 오산 → 과다흡입 → Code 11(펌프 파손)이라, 서버 SoT 값을 우선한다.
       주소 자체는 위 우선순위(명시 env > 물리 프로브)가 정한다 — settings 는 '용량'만 얹는다
       (설정상 있어야 할 펌프를 config 만 보고 매핑하지 않는다·물리 프로브가 존재 SoT).

    ⚠️ ②가 없으면 **`PUMP_ADDRESSES` 없는 기기는 전 스텝이 CMD_VALIDATION_FAILED 로 죽는다**
    — "URL만" 설치 목표(설치 시 env 안 넣어도 됨)와 정면 충돌한다. `pump_health` 는 이 스캔
    로직을 갖고 있었지만 **부팅에 배선돼 있지 않았다**(비-테스트 호출자 0건·2026-07-17 발견).

    ⚠️ **스캔 범위 = 소프트웨어 포트 매핑(모드)이 정한다**(2026-07-18). 무작정 1..10 을 훑으면
    식향(펌프 2대)에서 없는 3..10 각각에 프로브 상한(~6s)만큼 낭비해 부팅이 ~48s 늘어진다.
    모드가 펌프 수를 알려주므로(식향 2 → 주소 1,2 · 향장향 3 → 1,2,3) 그 예상 주소만 프로브한다
    — 부재 주소 낭비 0. (더 넓은 구성이 필요하면 `PUMP_ADDRESSES` 로 명시 = ①이 이긴다.)

    자동인식의 근거는 **실제 펌프 응답**이지 VID/PID 가 아니다 — 엔진 어댑터가 `probe(addr)`
    를 제공하면 그걸 쓰고(sy01b=RS485 상태쿼리), 없으면(Fake 등) 건너뛴다.
    """
    # 서버 settings 프리셋(부팅 스냅샷) → 용량/스트로크 오버라이드(없으면 None → 모드 기본 폴백).
    snapshot_capacity = syringe_capacity_from_settings(server_settings)
    # 오프라인 캐시 부팅(2026-09-29) — 스냅샷이 없으면 마지막으로 받은 이 기기 용량(캐시)을 **추정값**으로 쓴다(모드 기본 0.5
    #   추정보다 낫다 · 기기마다 시린지를 바꿀 수 있다). ⛔ 용량 가드는 라이브 스냅샷일 때만(아래 _mark — R4 P0-1).
    cache_capacity = (
        getattr(hardware_profile, "syringe_capacity_ml", None) if hardware_profile is not None else None
    )
    capacity_override = snapshot_capacity if snapshot_capacity is not None else cache_capacity
    # 용량 출처(2026-09-30 로그·가드) — snapshot(라이브 서버) | cache(마지막으로 받은 이 기기 설정) | default(모드 기본 추정).
    capacity_source = (
        "snapshot" if snapshot_capacity is not None else "cache" if cache_capacity is not None else "default"
    )
    # 이 기기 기종 — 감지(실물) > 선언(스냅샷·캐시). 없으면 판정하지 않는다(Undeclared 엔진이 어차피 모션을 거부한다).
    _model_for_capacity = hardware_profile.pump_model if hardware_profile is not None else None
    stroke_override = full_stroke_from_settings(server_settings)
    # 캐시 폴백(R-P0-4) — 스냅샷이 stroke 를 못 줬을 때 캐시 stroke 로 pump_map 을 맞춘다
    #   (안 맞추면 tecan 캐시 부팅이 어댑터 3000 vs spec 12000 = 영구 -1001). 용량은 비캐시 원칙.
    if stroke_override is None and hardware_profile is not None:
        stroke_override = hardware_profile.pump_full_stroke
    # 실물 감지 조립(2026-09-14)이면 **감지 기종 프리셋 stroke 가 스냅샷보다 우선** — 어댑터와 pump_map 이 같은
    #   관측에서 나와야 `_axis_guard` 가 구조적으로 안 걸린다. 선언(스냅샷 stroke)이 실물과 다른 경우가 바로
    #   이 기능이 존재하는 이유라, 그 값을 spec 에 얹으면 영구 -1001 이 된다.
    if hardware_profile is not None and getattr(hardware_profile, "source", "declared") == "detected":
        stroke_override = hardware_profile.pump_full_stroke
    # 포트 상한 — 스냅샷(hardware.valvePortCount) > 캐시 > 12.
    from ..adapters.settings_source import valve_port_count_from_settings as _vpc

    valve_port_count = _vpc(server_settings) or (
        hardware_profile.valve_port_count if hardware_profile is not None else None
    ) or 12

    def _mark(r: RecipeResolver) -> RecipeResolver:
        # 용량 출처 각인(R4.5 P2-A) — "pump_map 용량이 스냅샷 유래인가"를 여기(용량을 실제로
        #   파생하는 유일한 곳)서 함께 돌려준다. senlytd 가 이 값을 그대로 Dispatcher 가드에
        #   넘기므로 술어를 두 번 계산할 일이 없다 — 두 파일이 손으로 같은 불변식을 유지하다
        #   한쪽만 고쳐져 조용히 어긋나는(=P0-1 부활) 구조를 없앤다.
        # (2026-09-30) 캐시 용량으로 부팅해도 가드를 켠다 — 캐시 = "마지막으로 서버가 말한 이 기기 용량"(추측값 아님)이고,
        #   온라인이 되면 설정 감시자가 서버 해시와 다름을 보고 유휴일 때 재시작 없이 라이브 스냅샷을 적용한다. 가드를 끄면 오프라인 부팅 뒤
        #   재시작 전 창에 들어온 봉투가 틀린 용량으로 조용히 2배/절반 토출된다(검증 P1). 모드 기본(0.5 추정)만 가드 OFF 로 남긴다.
        r.capacity_from_settings = capacity_source in ("snapshot", "cache")
        r.capacity_source = capacity_source
        # 포트 상한 각인(2026-09-02) — RR 2차 게이트가 1..N 으로 판정(§C).
        r.valve_port_count = valve_port_count
        # 알코올 캐리어 포트 규칙(2026-09-30) — 스냅샷 통 배치로 "빈 구멍 알코올 흡입"을 거부한다(스냅샷 없으면 검사 안 함).
        from ..adapters.settings_source import alcohol_carrier_rule_from_settings as _alc

        r.port_liquids, r.alcohol_strict = _alc(server_settings)
        # 기종별 지원 용량(2026-09-30 · Tecan = 1·5mL) — 목록 밖이면 **모든 모션 거부**(fail-closed). 서버도 같은 판정으로
        #   제조를 막지만(syringe_unsupported), 옛 서버·스테일 캐시·모드 기본 추정(Tecan 0.5)으로 부팅한 경우를 pi 가 스스로 막는다.
        _cap_eff = (
            capacity_override
            if capacity_override is not None
            else resolve_syringe_capacity_ml(None, is_flavor=(str(mode or "").lower() == "flavor"))
        )
        r.capacity_ml = _cap_eff
        # 모드 기본 추정(default)은 **명시 저장이 필요한 기종(Tecan)** 에서만 "모름"으로 막는다 — SY-01B 는 추정 0.5 가
        #   지원 목록 안이라 종전대로 돈다(web requiresExplicitSyringeCapacity 와 일치 · 리뷰 P1 2026-09-30).
        r.capacity_block = capacity_block_for(_model_for_capacity, _cap_eff, capacity_source)
        return r

    raw = environ.get(SENLYT_PUMP_ADDRESSES_ENV)
    if raw and raw.strip():
        # env 고정 주소 ↔ AI 계약 펌프 키 불일치 경고(2026-09-29) — env 가 이긴다(고정 구성 호환)지만, 계약이 4펌프(향연)인데
        #   env 가 1,2,3 이면 4번 펌프 향료가 전부 unmapped drop 된다. 조용히 두지 않고 부팅 로그로 알린다.
        try:
            env_addrs = sorted(pump_map_from_addresses_env(raw).keys())
        except Exception:  # noqa: BLE001 — 형식 오류는 아래 조립이 그대로 보고한다.
            env_addrs = []
        snap_addrs = pump_addrs_from_settings(server_settings)
        if snap_addrs and env_addrs and sorted(set(env_addrs)) != sorted(set(snap_addrs)):
            import logging as _logging

            _logging.getLogger("senlyt_pi.bootstrap").warning(
                "PUMP_ADDRESSES env %s ≠ AI 계약 펌프 %s — env 가 이긴다(고정 구성). 계약 펌프 중 env 에 없는 주소의 향료는 "
                "토출되지 않는다 · env 를 지우거나 계약을 확인하세요",
                env_addrs,
                snap_addrs,
            )
        return _mark(
            RecipeResolver(
                pump_map_from_addresses_env(
                    raw,
                    capacity_override=capacity_override,
                    full_stroke_override=stroke_override,
                )
            )
        )

    probe = getattr(engine, "probe", None) if engine is not None else None
    if callable(probe):
        # 모드 = **서버배정 mode 우선**(TOFU 후 identity.mode), env 는 폴백(2026-07-18). 'URL만' 설치는
        #   env 를 안 넣으므로, mode 를 안 받으면 식향 2펌프 기기도 향장향으로 오판해 부재 addr 3 프로브
        #   상한(~6s)을 낭비한다. 기능은 물리 프로브가 SoT 라 어느 경로든 정확하나, 부팅 지연·설계 정합.
        mode_str = (mode or environ.get(SENLYT_MODE_ENV, "") or "").strip().lower()
        is_flavor = mode_str == "flavor"
        # 모드 → 예상 펌프 주소(소프트웨어 매핑). 식향 2대(1,2) / 향장향 3대(1,2,3) + 서버 설정의 펌프 키
        #   (향연 4대 · 2026-09-29 — `expected_pump_addrs`).
        expected = expected_pump_addrs(mode_str, server_settings, hardware_profile)
        found = (
            sorted(set(int(a) for a in known_pump_addrs))
            if known_pump_addrs is not None
            else discover_pumps(probe, expected)
        )
        if found:
            capacity = (
                capacity_override
                if capacity_override is not None
                else resolve_syringe_capacity_ml(None, is_flavor=is_flavor)
            )
            return _mark(
                RecipeResolver(
                    auto_pump_map(found, capacity_ml=capacity, full_stroke=stroke_override)
                )
            )
    return _mark(RecipeResolver({}))


def build_components(
    environ: Mapping[str, str],
    *,
    engine: EnginePort | None = None,
    ledger: IdempotencyLedger | None = None,
    logger: StructuredLogger | None = None,
    identity_store: DeviceIdentityStore | None = None,
    register: bool = True,
    fetch_settings: bool = False,
    settings_fetcher: SettingsFetcher | None = None,
    estop_event: "threading.Event | None" = None,
    # 부팅 실물 감지 seam(2026-09-14) — 테스트는 port_lister=lambda: [] 로 감지를 끄거나 pump_detector 를 주입.
    port_lister: "Callable[[], list] | None" = None,
    pump_detector: "Detector | None" = None,
    # None = fetch_settings 를 따른다(실 부팅만 감지 · 조립 self-test/테스트는 포트를 열지 않는다).
    detect_hardware: "bool | None" = None,
) -> DaemonComponents:
    """환경변수에서 실 어댑터 전체를 조립 — 서버 타겟 결정 + 등록 + 어댑터 결선.

    Args:
      register: True(기본)면 실 HTTP 등록을 수행. 테스트는 register=False + identity_store
                (선주입 정체성)로 네트워크 없이 조립 검증.
      engine:   주입 시 그대로(유일 mock=Fake). 미주입이면 SENLYT_ENGINE 분기.
      fetch_settings: True 면 부팅 1회 서버 settings 스냅샷을 읽어 server_settings 에 싣는다
                (시린지 용량 SoT·O-18). 실 데몬(senlytd._run)만 켠다 — 기본 False(조립 테스트는
                네트워크 없이). best-effort: 실패 시 server_settings=None(모드 기본 용량 폴백).
      settings_fetcher: 부팅 settings fetch seam 주입(테스트). 미주입이면 실 SSE 1회 읽기.

    Raises:
      ServerTargetError: 서버 base URL 미설정/미지원(fail-fast — config.server_target).
      BootstrapError:    deviceId(수집 시리얼) 부재 또는 등록 실패.
    """
    log = logger if logger is not None else StructuredLogger()
    # 1) 서버 타겟(base URL) 결정 — 미설정 시 ServerTargetError(fail-fast·prod 오접속 차단).
    server_config = ServerConfig.from_environ(environ)

    # 2) 정체성 확보 — 저장분 재사용 or 실 HTTP 등록.
    store = identity_store or DeviceIdentityStore(_identity_path(environ, server_config.base_url))
    if register:
        # [D-A] 수집 HW 시리얼 = deviceId(서버 발급 없음). 부재 시 fail-fast(임의값 금지).
        device_id = read_hardware_id(env=environ)
        if not device_id:
            raise BootstrapError(
                "deviceId(수집 시리얼) 확보 불가 — SENLYT_HARDWARE_ID 또는 /proc/cpuinfo Serial 필요"
            )
        # TOFU(2026-07-17): 공유키 없음 — deviceId 만 제시(등록 202 pending → 운영자 승인 후 토큰).
        transport = make_http_register_transport(server_config.register_url)
        client = RegistrationClient(
            transport,
            device_id=device_id,
            name=environ.get(SENLYT_DEVICE_NAME_ENV) or None,
        )
        try:
            # server_base_url 전달 = 서버 바인딩(2026-07-23) — 저장된 정체성이 다른 서버 것이면(=URL 만
            #   바꿔 재설치) 그 서버에 재등록해 admin 후보로 뜨게 한다(옛 서버 정체성 재사용 → 페어링 실패 방지).
            identity = ensure_registered(store, client, server_base_url=server_config.base_url)
        except Exception as e:  # RegistrationError 포함 — fail-fast 표면화.
            log.error(
                "디바이스 등록 실패 — 부팅 중단",
                stage=STAGE_ERROR,
                error=str(e),
            )
            raise BootstrapError(f"등록 실패: {e}") from e
    else:
        loaded = store.load()
        if loaded is None:
            raise BootstrapError(
                "register=False 이지만 저장된 정체성이 없음(테스트는 identity_store 선주입 필요)"
            )
        identity = loaded

    log.bind_device(identity.device_id)

    # 3) 실 어댑터 조립 — 동일 base·동일 dispenserToken·동일 logger.
    # mode 는 서버 배정(identity.mode·TOFU 승인 시 하달)이 **우선** — 없으면 env(SENLYT_MODE)→flavor 폴백.
    #   서버가 SoT(운영자가 /admin 에서 기기 모드 배정) → SENLYT_MODE env 는 더 이상 필수가 아니다.
    mode = identity.mode or _resolve_mode(environ)

    # 부팅 1회 서버 settings 스냅샷(시린지 용량 SoT·O-18) — fetch_settings=True(실 데몬)일 때만.
    #   best-effort: 실패는 삼켜 None(모드 기본 용량 폴백). seam(settings_fetcher) 로 테스트 주입 가능.
    server_settings: "Mapping[str, Any] | None" = None
    if fetch_settings:
        fetcher = settings_fetcher if settings_fetcher is not None else fetch_settings_once
        # 재시도는 **엔진 무관 3회**(R4 P0-1) — 종전 "sy01b 는 폴백 축이 곧 정답이라 1회" 는
        #   스트로크 축(12000)에만 참이었다. 용량 축이 fail-closed 가 된 지금, 스냅샷 부재는
        #   "용량 가드 비활성 + 서버·pi 용량 불일치 가능" 창이라 어느 엔진이든 순단을 흡수한다.
        attempts = 3
        for _attempt in range(attempts):
            try:
                server_settings = fetcher(server_config, identity.dispenser_token, mode)
            except Exception as e:  # noqa: BLE001 — settings fetch 실패는 부팅을 막지 않는다(폴백).
                log.warn(
                    "부팅 settings fetch 실패 — 모드 기본 용량으로 폴백(best-effort)",
                    stage=STAGE_ERROR,
                    error=str(e),
                )
                server_settings = None
            if server_settings is not None or _attempt == attempts - 1:
                break
            log.warn(
                f"settings 스냅샷 부재(시도 {_attempt + 1}/{attempts}) — 재시도 (tecan 축 확정에 필수)",
                stage=STAGE_ERROR,
            )
            time.sleep(2.0)

    command_source = SseCommandSourceAdapter(
        server_config=server_config,
        bearer_token=identity.dispenser_token,
        mode=mode,
        logger=log,
    )
    status_sink = HttpStatusSinkAdapter(
        server_config=server_config,
        bearer_token=identity.dispenser_token,
        mode=mode,
        # 관측 로그 디스크 스풀(단절 유실 0) — 전송 실패 배치를 보존, 재연결/재부팅 후 업로드.
        trace_spill=TraceSpill(_trace_spill_path(environ)),
        logger=log,
    )

    # 4) 하드웨어 선언 해석(2026-09-02 단일 SoT) — 스냅샷 엄격 판독 > 로컬 캐시 > Undeclared.
    #    성공한 스냅샷 선언은 캐시에 기록(오프라인 재부팅 폴백 — hardware_profile_cache 헤더).
    from ..adapters.settings_source import (
        hardware_profile_from_snapshot,
        pump_model_from_settings,
        snapshot_settings_confirmed,
    )
    from ..persistence.hardware_profile_cache import load_profile, save_profile

    # 캐시 디렉터리 = **명시된 상태 경로만**(SENLYT_STATE_DIR > LOG_DIR). 미설정(테스트·개발 cwd)
    #   이면 캐시 비활성 — cwd 에 상태 파일을 흘리면 테스트 간 오염·레포 오염이 된다(실기기는
    #   install.sh 가 SENLYT_STATE_DIR=/var/lib/senlyt 를 각인하므로 항상 활성).
    _hw_state_dir = (
        environ.get(SENLYT_STATE_DIR_ENV, "").strip() or environ.get("LOG_DIR", "").strip()
    )
    hardware_profile: "HardwareProfile | None" = None
    hardware_source = "undeclared"
    _snap_model = pump_model_from_settings(server_settings)
    if _snap_model is not None:
        # 조립 규칙(stroke 폴백 = 선언 모델 프리셋 기본 등)은 헬퍼가 SoT — senlytd
        #   _undeclared_refetch 와 손 동기화하다 한쪽만 어긋나는 것 방지(R6.5 M3).
        hardware_profile = hardware_profile_from_snapshot(_snap_model, server_settings)
        hardware_source = "snapshot"
        # (2026-09-30) 확정된 설정만 캐시한다 — 미확정(신규 기기·계약 구성 변경·조회 실패) 프레임의 용량·배치는 **계약 기본값 초안**
        #   이라 "마지막으로 받은 이 기기 설정"이 아니다. 그걸 캐시하면 다음 오프라인 부팅이 초안 용량으로 가드를 켠다.
        #   settingsStatus 부재(구 서버)는 종전대로 저장한다.
        if _hw_state_dir and snapshot_settings_confirmed(server_settings):
            save_profile(_hw_state_dir, hardware_profile, server_config.base_url)
    else:
        cached = load_profile(_hw_state_dir, server_config.base_url) if _hw_state_dir else None
        if cached is not None:
            hardware_profile = cached
            hardware_source = "cache"
            log.warn(
                f"하드웨어 선언 — 스냅샷 부재, 캐시 폴백(model={cached.pump_model}·"
                f"stroke={cached.pump_full_stroke}·ports={cached.valve_port_count}·"
                f"pumpAddrs={list(cached.pump_addrs) or '모드 기본'}). "
                "용량 축은 미확정(모드 기본 가정·용량 가드 OFF) — 네트워크 복구 후 재시작 권장",
                stage=STAGE_PI_RECEIVED,
            )

    # 4.5) **실물 기종 자동 인식**(2026-09-14 · 사용자 요구 "pi 는 Runze 든 Tecan 이든 스스로 인식") — 어댑터를
    #    조립하기 전에 예상 주소에 `?`→`&` 만 읽어 기종을 정한다. 우선순위: 감지 > 선언(스냅샷>캐시) > Undeclared.
    #    엔진 주입(테스트)·fake 스위치·후보 포트 없음이면 건너뛴다(감지 없음 = 종전 경로 그대로).
    detected_pump_addrs: "tuple[int, ...] | None" = None
    undeclared_detail: "str | None" = None
    _detect = fetch_settings if detect_hardware is None else detect_hardware
    if _detect and engine is None and not _is_truthy(environ.get(SENLYT_FAKE_ENGINE_ENV)):
        from ..adapters.serial_port_discovery import discover_serial_port as _dsp

        _port = _dsp(environ, port_lister=port_lister)
        if _port:
            from ..adapters.pump_model_detect import detect_pump_model as _default_detector

            # 향연 4대(2026-09-29) — 모드 기본 ∪ (스냅샷 펌프 키 > 캐시 pumpAddrs). 오프라인 캐시 부팅도 addr 4 를 프로브한다.
            _expected, _addr_src = expected_pump_addrs_with_source(mode, server_settings, hardware_profile)
            log.info(f"펌프 프로브 대상 {_expected} (출처={_addr_src})", stage=STAGE_PI_RECEIVED)
            _detector = pump_detector if pump_detector is not None else (
                lambda p, addrs: _default_detector(p, addrs, logger=log)
            )
            try:
                _det = _detector(_port, _expected)
            except Exception as e:  # noqa: BLE001 — 감지 실패 = 응답 0 취급(종전 경로).
                log.warn("펌프 기종 자동 인식 실패 — 선언 경로로 조립", stage=STAGE_ERROR, error=str(e))
                _det = None
            if _det is not None:
                # 응답 0 도 "스캔 결과" 다 — 2차 스캔(discover_pumps)을 또 돌지 않는다(검증 P2-2). 늦게 켜진 펌프는
                #   종전대로 재발견 재기동(on_pumps_seen_unmapped)이 다시 부팅 감지로 데려온다.
                detected_pump_addrs = tuple(_det.responding)
            if _det is not None and _det.responding:
                _declared = hardware_profile.pump_model if hardware_profile is not None else None
                # ── 근거 강도 비대칭 규칙(검증 P0-1) — 형식 규칙만으로 분류된 기종이 **선언과 어긋나면** 채택하지
                #   않는다. Runze 정규식(소수 하나)은 미지 XCalibur 로트의 `&`(예 "3.10")도 잡아, 선언 tecan 을 sy01b 로
                #   뒤집으면 U…R 이 NVM 에 나간다. 실측 정확값(strong)·선언과 일치·선언 없음+Tecan(좁은 정규식)만 채택,
                #   나머지는 fail-closed(Undeclared) + WARN — 새 로트는 지문을 KNOWN_FINGERPRINTS(+서버 레지스트리)에 등록.
                #   ⚠️ 비대칭의 이유: Tecan 정규식(`^30\d{6}\s+[A-Z]`)은 좁아 Runze 가 만들 수 없는 꼴이라 형식만으로도
                #   채택한다(sy01b 선언을 뒤집어 Tecan 어댑터로 — 그 반대인 sy01b 조립이 곧 U-NVM 위험). Runze 형식만은 안 된다.
                _weak_conflict = (
                    _det.model == "sy01b" and not _det.strong and _declared != "sy01b"
                )
                if _weak_conflict:
                    log.warn(
                        f"펌프 지문이 형식 규칙으로만 {_det.model} 로 분류됐고 선언({_declared})과 어긋납니다 — 추측 조립 금지, "
                        f"모션 거부(fingerprints={ {str(a): v for a, v in _det.fingerprints.items()} }). 실측 지문을 목록에 등록하거나 선언을 확인하세요",
                        stage=STAGE_PI_RECEIVED,
                    )
                    hardware_profile = None
                    hardware_source = "undetected"
                    undeclared_detail = (
                        "펌프 기종 지문이 형식만 일치하고 선언과 어긋남 — 모션 거부. 실측 지문 등록 또는 선언 확인 후 재연결"
                    )
                elif _det.model is not None:
                    if hardware_profile is not None and hardware_profile.pump_model != _det.model:
                        log.warn(
                            f"센소리움 선언(model={hardware_profile.pump_model})과 실물(model={_det.model})이 다릅니다 — "
                            "실물 기종으로 조립합니다(선언은 표시·AI 축에만 남음 · admin 에서 '선언을 실물에 맞추기')",
                            stage=STAGE_PI_RECEIVED,
                        )
                    hardware_profile = HardwareProfile(
                        pump_model=_det.model,
                        pump_full_stroke=PUMP_PRESETS[_det.model].pump_full_stroke,
                        valve_port_count=(
                            hardware_profile.valve_port_count if hardware_profile is not None else 12
                        ),
                        sensorium_version=(
                            hardware_profile.sensorium_version if hardware_profile is not None else None
                        ),
                        pump_addrs=(hardware_profile.pump_addrs if hardware_profile is not None else ()),
                        source="detected",
                        # (2026-09-30 검증 P1) 선언(스냅샷·캐시)의 보조축을 이어받는다 — 빠뜨리면 오프라인 부팅이 캐시 용량을 버리고
                        #   모드 기본 0.5 로 조립된다(용량 가드도 꺼져 조용히 2배/절반 토출). 튠은 **기종이 같을 때만**(다른 기종 값이
                        #   이 펌프로 새지 않게) · 계약은 그대로. 용량도 **기종이 같을 때만**(리뷰 P2-2 — 기종이 바뀐 건 랙 교체라
                        #   다른 기종의 시린지 용량을 얹으면 오프라인 창에 틀린 양이 나간다 → default 로 두고 가드가 판정).
                        contract_id=(hardware_profile.contract_id if hardware_profile is not None else None),
                        syringe_capacity_ml=(
                            hardware_profile.syringe_capacity_ml
                            if hardware_profile is not None and hardware_profile.pump_model == _det.model
                            else None
                        ),
                        pump_tuning=(
                            hardware_profile.pump_tuning
                            if hardware_profile is not None and hardware_profile.pump_model == _det.model
                            else None
                        ),
                    )
                    hardware_source = "detected"
                else:
                    # 응답은 있는데 안전하게 조립할 기종을 정할 수 없음 — 선언 추측으로 폴백하지 않는다(fail-closed).
                    hardware_profile = None
                    hardware_source = _det.source  # "mixed" | "undetected"
                    undeclared_detail = (
                        "펌프 기종 혼합 감지(한 버스에 Runze 와 Tecan) — 모션 거부. 같은 기종으로 맞춘 뒤 재연결"
                        if _det.mixed
                        else "펌프가 응답하지만 기종 지문(&)을 판독하지 못함 — 모션 거부. 60초 주기로 재감지"
                    )

    # 5) 엔진·밸브 조립 + 부팅 자가진단 로그(눈에 띄게) — 무엇으로 잡았는지 운영자가 로그로
    #    확인한다(silent auto 금지 — auto + visible self-diagnostic).
    # 운영자 속도 튠(§6-3a · 2026-09-22) — 스냅샷 pumpPreset 의 v·V·c·L 을 **실물 기종** 기본값 위에 얹는다.
    #   스냅샷에 hardware 선언이 없거나(병합 스킵·캐시 부팅) 선언 기종 ≠ 조립 기종이면 None → build_engine 이
    #   그 기종 **제조사 기본값**으로 조립한다 — 다른 기종 값이 이 펌프로 새지 않는다. 부팅 1회 스냅샷이라
    #   admin "적용" 뒤 반영 = 재시작(용량 축과 같은 계약). 아래 자가진단 로그의 pumpTuning/pumpSpeedCeil 로 드러난다.
    tuned_preset = (
        pump_tuning_from_settings(server_settings, hardware_profile.pump_model)
        if hardware_profile is not None
        else None
    )
    # 오프라인 캐시 부팅(2026-09-29) — 스냅샷 튠이 없으면 마지막으로 받은 이 기기 튠(캐시)을 쓴다. **캐시 기종 = 조립 기종**일
    #   때만(감지로 기종이 바뀌었으면 다른 기종 값이 새지 않게 제조사 기본값).
    if (
        tuned_preset is None
        and server_settings is None
        and hardware_profile is not None
        and getattr(hardware_profile, "pump_tuning", None)
    ):
        from ..core.pump_guard import PUMP_PRESETS as _PP
        from ..core.pump_guard import apply_pump_tuning as _apt

        if hardware_profile.pump_model in _PP:
            tuned_preset = _apt(_PP[hardware_profile.pump_model], hardware_profile.pump_tuning)
    engine_adapter = build_engine(
        environ,
        engine=engine,
        estop_event=estop_event,
        logger=log,
        pump_model=hardware_profile.pump_model if hardware_profile is not None else None,
        undeclared_detail=undeclared_detail,
        port_lister=port_lister,
        pump_preset=tuned_preset,
    )
    valve_adapter = build_valve(environ)
    # 축(stroke) 자가진단 — 단일 키 설계(2026-09-02)에선 어댑터가 설정에서 조립되므로 "설정 vs
    #   어댑터" 불일치는 동어반복이 됐다. 남는 감시 대상은 **스냅샷 vs 캐시 드리프트**(센소리움을
    #   바꿨는데 오프라인 캐시로 부팅한 창 — 재시작/네트워크 복구 권고)뿐이다.
    settings_stroke = full_stroke_from_settings(server_settings)
    adapter_preset = getattr(engine_adapter, "preset", None)
    adapter_stroke = adapter_preset.pump_full_stroke if adapter_preset is not None else None
    # 유효 설정축 — 스냅샷 > 캐시(캐시 부팅은 pump_map 도 캐시 stroke 라 이게 진짜 유효축) > 기본.
    # 실물 감지 조립(2026-09-14)이면 유효축 = 감지 기종 프리셋(pump_map 도 같은 값) — 스냅샷(선언) stroke 로 보면
    #   A1 정상 경로에서 매 부팅 "축 드리프트" 거짓 WARN 이 찍힌다(검증 P2-1).
    effective_stroke = (
        hardware_profile.pump_full_stroke
        if hardware_source == "detected" and hardware_profile is not None
        else settings_stroke
        if settings_stroke is not None
        else (
            hardware_profile.pump_full_stroke
            if hardware_profile is not None
            else PUMP_PRESETS["sy01b"].pump_full_stroke
        )
    )
    # (2026-09-30 로그 보강) 이 부팅이 실제로 쓰는 용량·출처·스텝 모드·계약·설정 해시 — Cloud Logging 에서 한 줄로 대조한다.
    from ..adapters.settings_watcher import contract_id_from_settings as _cid
    from ..adapters.settings_watcher import settings_hash_from_settings as _shash

    _snap_cap = syringe_capacity_from_settings(server_settings)
    _cache_cap = hardware_profile.syringe_capacity_ml if hardware_profile is not None else None
    _boot_cap_source = "snapshot" if _snap_cap is not None else "cache" if _cache_cap is not None else "default"
    _boot_model = hardware_profile.pump_model if hardware_profile is not None else None
    log.event(
        "하드웨어 자가진단 — 엔진·밸브 자동감지 결과",
        stage=STAGE_PI_RECEIVED,
        gpio_available=_gpio_available(),
        engine=type(engine_adapter).__name__,
        valve=type(valve_adapter).__name__ if valve_adapter is not None else "off",
        mode=mode,
        # 이 부팅의 유효 시린지 용량(mL) — 스냅샷 > 캐시(마지막으로 받은 이 기기 설정) > 부재(None = 모드 기본 추정).
        syringeCapacityMl=_snap_cap if _snap_cap is not None else _cache_cap,
        capacitySource=_boot_cap_source,
        # 풀스트로크 스텝·스텝 모드 — Tecan 은 N0(표준 3000 · 어댑터가 N0R 명시), SY-01B 는 자체 해상도(12000).
        fullStroke=effective_stroke,
        stepMode=(
            "N0" if _boot_model == "tecan_xcalibur" else "sy01b" if _boot_model == "sy01b" else None
        ),
        contractId=(
            _cid(server_settings)
            or (hardware_profile.contract_id if hardware_profile is not None else None)
        ),
        settingsHash=_shash(server_settings),
        settingsSnapshot="present" if server_settings is not None else "absent",
        # ⚠️ 키 구분(R3 P3-4): 여기는 스냅샷 원본 축(부재=None), 아래 WARN 의 settingsStroke 는
        #   폴백 적용 후 유효축 — 같은 키로 두 뜻을 찍으면 로그 대조가 어긋난다.
        settingsStrokeRaw=settings_stroke,
        adapterStroke=adapter_stroke,
        # 하드웨어 선언 각인(2026-09-02) — 무엇으로(model·ports) 어디서(source) 조립했는가.
        hardwareModel=hardware_profile.pump_model if hardware_profile is not None else None,
        hardwarePorts=hardware_profile.valve_port_count if hardware_profile is not None else None,
        hardwareSource=hardware_source,
        # 속도 튠 관측(§6-3a) — "default"=제조사 기본값 그대로(튠 없음/스냅샷 부재/기종 불일치) · "tuned"=운영자 값.
        #   판정은 **어댑터가 실제로 든 preset**(주입을 거부·무시한 경우 포함)으로 — 실제 v·V·c·L 을 함께 찍어
        #   admin 화면 값과 대조할 수 있게 한다.
        pumpTuning=(
            "tuned"
            if adapter_preset is not None
            and adapter_preset.pump_preset_id in PUMP_PRESETS
            and adapter_preset != apply_pump_tuning(PUMP_PRESETS[adapter_preset.pump_preset_id], None)
            else "default"
        ),
        pumpSpeedCeil=(
            f"v{adapter_preset.pump_max_start_speed_hz}V{adapter_preset.pump_max_top_speed_hz}"
            f"c{adapter_preset.pump_max_cutoff_speed_hz}L{adapter_preset.pump_max_slope}"
            if adapter_preset is not None
            else None
        ),
    )
    if syringe_capacity_from_settings(server_settings) is None:
        # R4 P0-1 — 스냅샷이 용량을 안 줬다 = 이 부팅의 용량(모드 기본 0.5)은 **추측값**이다.
        #   용량 축 가드는 비활성(추측으로 서버 선언을 거부하면 벽돌)이고, 서버 설정이 0.5 가
        #   아니면 소량 주문이 무성 과소/과다로 흐를 수 있다 — 재시작(재fetch)이 정석 복구.
        log.warn(
            "settings 스냅샷 부재 — 시린지 용량 미확정(모드 기본 0.5 가정·용량 축 가드 비활성). "
            "서버 설정 용량이 0.5mL 가 아니면 부피가 어긋난다 — 네트워크 확인 후 senlytd 재시작 권장",
            stage=STAGE_PI_RECEIVED,
        )
    if adapter_stroke is not None and adapter_stroke != effective_stroke:
        # ⚠️ 숫자를 message 에 인라인 — 서버 trace allowlist 는 message 만 통과시키고 kwargs
        #   (detail)는 admin 도달 전에 폐기된다. 단일 키 설계에선 이 불일치 = **캐시 부팅인데
        #   서버 선언이 그 사이 바뀐 드리프트**(또는 스텝 조립 축과 어댑터 축이 갈린 스테일 창).
        log.warn(
            f"축 드리프트 — 유효 설정축 {effective_stroke} ≠ 부팅 어댑터축 {adapter_stroke}"
            f"(선언출처={hardware_source}·snapshot={'present' if server_settings is not None else 'absent'}). "
            "모션은 축 가드가 거부한다(-1001). 서버 센소리움 선언 확인 후 senlytd 재시작 필요",
            stage=STAGE_PI_RECEIVED,
            settingsStroke=effective_stroke,
            adapterStroke=adapter_stroke,
        )

    return DaemonComponents(
        device_id=identity.device_id,
        server_config=server_config,
        identity=identity,
        command_source=command_source,
        status_sink=status_sink,
        engine=engine_adapter,
        valve=valve_adapter,
        ledger=ledger if ledger is not None else InMemoryIdempotencyLedger(),
        logger=log,
        mode=mode,
        hardware_profile=hardware_profile,
        hardware_source=hardware_source,
        server_settings=server_settings,
        detected_pump_addrs=detected_pump_addrs,
    )


# ── 공개 env 해석 표면(2026-09-04 헥사고날 감사 P2) — 형제 도구(hwtool 등)가 daemon 과
#   동일한 env 해석을 쓰되 **프라이빗 심볼을 관통하지 않게** 하는 안정 별칭. 시그니처 계약:
#   float_env(environ, key, default) · valve_pins_from_env(raw) — 내부 리팩토링 시 이 별칭은 유지.
float_env = _float_env
valve_pins_from_env = _valve_pins_from_env
