"""senlytd 실 어댑터 조립(bootstrap) — 환경변수 → ServerConfig → 실 어댑터 결선.

정본: 02_infra §10 통합 E2E 토폴로지.

책임(스텁 제거·사용자 원칙 2026-07-10):
  - `SENLYT_ENV`/`SENLYT_SERVER_BASE_URL` → `ServerConfig`(base URL 단일 결정·fail-fast).
  - 등록(POST /api/dispensers/register·실 HTTP) → deviceId·dispenserToken 확보(파일 영속).
  - 실 어댑터 조립: SSE command/commandSet source + HTTP status sink(orders/heartbeat/trace/봉투전이).
  - **엔진만 FakeEngineAdapter**(유일 mock·v1.1.0 HW 검증). `SENLYT_ENGINE=fake|sy01b` 로 분기,
    기본(E2E)=fake. sy01b(실 RS485)는 아직 TODO 스텁이라 명시적으로 선택할 때만 조립.

⚠️ 이 모듈은 **결선(wiring)만** 한다 — 실제 펌프 소비 루프(SSE→멱등→Sequencer→역보고 상시 구동)는
   안전상 daemon.boot 유보를 유지한다. bootstrap 은 어댑터를 실체로 만들어 DaemonDeps 로 묶는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 순환 없음 — 타입 전용(런타임 import 는 build_components 내부 지역).
    from ..persistence.hardware_profile_cache import HardwareProfile

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

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
    fetch_settings_once,
    full_stroke_from_settings,
    syringe_capacity_from_settings,
)
from ..adapters.sse_command_source_adapter import SseCommandSourceAdapter
from ..config.server_target import ServerConfig
from ..core.pump_guard import PUMP_PRESETS, SyringeSpec, resolve_syringe_capacity_ml
from ..obs.log import STAGE_ERROR, STAGE_PI_RECEIVED, StructuredLogger
from ..persistence.file_idempotency_ledger import FileIdempotencyLedger
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
    # 선언 출처 관측 — "snapshot" | "cache" | "undeclared"(부팅 자가진단·재fetch 판단용).
    hardware_source: str = "undeclared"


def _resolve_mode(environ: Mapping[str, str]) -> str:
    mode = environ.get(SENLYT_MODE_ENV, "").strip().lower()
    return "fragrance" if mode == "fragrance" else "flavor"


def _gpio_available() -> bool:
    """실 라즈베리파이 GPIO 존재 여부 — **Pi4(`/dev/gpiomem`)·Pi5(`/dev/gpiomem0`·RP1) 모두 커버**.

    **자동감지 게이트** — 비-Pi(CI·dev·docker 컨테이너)는 gpiomem 계열이 없어 False → engine/valve 가
    항상 fake 로 떨어진다(결정적). 실 Pi 에서만 실 하드웨어 자동 선택이 활성화된다.
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
) -> EnginePort:
    """엔진 조립 — 주입 우선. 실 Pi 는 **서버 선언(pump_model)** 로, 비-Pi 는 fake 로 조립.

    설치 시 `SENLYT_ENGINE` 을 안 넣어도 된다("URL만" 목표) — 실 Pi 에 USB-RS485 펌프 어댑터가
    붙어 있으면 sy01b, 그 외(비-Pi·어댑터 미장착)는 fake 로 자동 결정한다. 명시하면 그 값이 우선.
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
    # 비-Pi(개발환경) 자동 fake 는 유지 — 실 Pi 에서 fake 후퇴 금지 원칙(2026-07-19)도 그대로.
    is_pi = on_pi() if on_pi is not None else _gpio_available()
    if not is_pi:
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

        if port:
            return _RealAdapter(
                port=port, estop_event=estop_event, logger=logger, port_resolver=_resolve_ports
            )
        return _RealAdapter(
            estop_event=estop_event, logger=logger, port_resolver=_resolve_ports
        )
    # 실 Pi + 선언 미확정(None/미지값) — Undeclared fail-closed(추측 조립 금지 · 상태모델 D3).
    #   ⛔ 폴백 sy01b 금지: tecan 이라 선언됐던 기기가 미확정 부팅에서 sy01b 로 조립되면
    #   초기화 프리앰블 U…R 이 XCalibur NVM 에 기록된다(undeclared_engine_adapter 헤더).
    from ..adapters.undeclared_engine_adapter import UndeclaredEngineAdapter

    if logger is not None:
        logger.warn(
            "하드웨어 선언 미확정 — 모션 거부 어댑터로 부팅(스냅샷·캐시 모두 무효). "
            "네트워크/admin 센소리움 배정 확인 후 재시작 필요",
            stage=STAGE_PI_RECEIVED,
        )
    return UndeclaredEngineAdapter()


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
    """기주 밸브 조립(§9-1 v2) — 주입 우선. **env 미지정이면 자동감지**(실 Pi → gpio·아니면 fake).

    설치 시 `SENLYT_VALVE` 를 안 넣어도 된다("URL만" 목표) — 실 Pi(GPIO 존재)면 gpio, 비-Pi 는 fake.
      - 자동 gpio 결선 실패(gpiozero 부재 등)는 **graceful fallback → fake**(자동 선택이라 부팅 중단 X).
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
        # 자동감지 — 실 Pi 면 gpio(결선 실패 시 graceful fake), 아니면 fake.
        is_pi = on_pi() if on_pi is not None else _gpio_available()
        if is_pi:
            try:
                return _gpio()
            except Exception:  # noqa: BLE001 — 자동 선택 실패는 안전 폴백(fake), 부팅 중단 없음.
                return FakeValveAdapter(flow_ml_per_sec=flow, max_open_sec=max_open)
        return FakeValveAdapter(flow_ml_per_sec=flow, max_open_sec=max_open)

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


def build_resolver(
    environ: Mapping[str, str],
    *,
    engine: EnginePort | None = None,
    server_settings: "Mapping[str, Any] | None" = None,
    mode: str | None = None,
    # 하드웨어 선언(2026-09-02 단일 SoT) — 캐시 부팅 시 stroke·포트 상한의 공급원(R-P0-4:
    #   스냅샷 부재여도 캐시 stroke 로 pump_map 을 맞춰 영구 -1001 을 막는다). 용량은 비캐시.
    hardware_profile: "HardwareProfile | None" = None,
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
    capacity_override = syringe_capacity_from_settings(server_settings)
    stroke_override = full_stroke_from_settings(server_settings)
    # 캐시 폴백(R-P0-4) — 스냅샷이 stroke 를 못 줬을 때 캐시 stroke 로 pump_map 을 맞춘다
    #   (안 맞추면 tecan 캐시 부팅이 어댑터 3000 vs spec 12000 = 영구 -1001). 용량은 비캐시 원칙.
    if stroke_override is None and hardware_profile is not None:
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
        r.capacity_from_settings = capacity_override is not None
        # 포트 상한 각인(2026-09-02) — RR 2차 게이트가 1..N 으로 판정(§C).
        r.valve_port_count = valve_port_count
        return r

    raw = environ.get(SENLYT_PUMP_ADDRESSES_ENV)
    if raw and raw.strip():
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
        # 모드 → 예상 펌프 주소(소프트웨어 매핑). 식향 2대(1,2) / 향장향 3대(1,2,3). 2026-07-17 확정.
        expected = [1, 2] if is_flavor else [1, 2, 3]
        found = discover_pumps(probe, expected)
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
        if _hw_state_dir:
            save_profile(_hw_state_dir, hardware_profile, server_config.base_url)
    else:
        cached = load_profile(_hw_state_dir, server_config.base_url) if _hw_state_dir else None
        if cached is not None:
            hardware_profile = cached
            hardware_source = "cache"
            log.warn(
                f"하드웨어 선언 — 스냅샷 부재, 캐시 폴백(model={cached.pump_model}·"
                f"stroke={cached.pump_full_stroke}·ports={cached.valve_port_count}). "
                "용량 축은 미확정(모드 기본 가정·용량 가드 OFF) — 네트워크 복구 후 재시작 권장",
                stage=STAGE_PI_RECEIVED,
            )

    # 5) 엔진·밸브 조립 + 부팅 자가진단 로그(눈에 띄게) — 무엇으로 잡았는지 운영자가 로그로
    #    확인한다(silent auto 금지 — auto + visible self-diagnostic).
    engine_adapter = build_engine(
        environ,
        engine=engine,
        estop_event=estop_event,
        logger=log,
        pump_model=hardware_profile.pump_model if hardware_profile is not None else None,
    )
    valve_adapter = build_valve(environ)
    # 축(stroke) 자가진단 — 단일 키 설계(2026-09-02)에선 어댑터가 설정에서 조립되므로 "설정 vs
    #   어댑터" 불일치는 동어반복이 됐다. 남는 감시 대상은 **스냅샷 vs 캐시 드리프트**(센소리움을
    #   바꿨는데 오프라인 캐시로 부팅한 창 — 재시작/네트워크 복구 권고)뿐이다.
    settings_stroke = full_stroke_from_settings(server_settings)
    adapter_preset = getattr(engine_adapter, "preset", None)
    adapter_stroke = adapter_preset.pump_full_stroke if adapter_preset is not None else None
    # 유효 설정축 — 스냅샷 > 캐시(캐시 부팅은 pump_map 도 캐시 stroke 라 이게 진짜 유효축) > 기본.
    effective_stroke = (
        settings_stroke
        if settings_stroke is not None
        else (
            hardware_profile.pump_full_stroke
            if hardware_profile is not None
            else PUMP_PRESETS["sy01b"].pump_full_stroke
        )
    )
    log.event(
        "하드웨어 자가진단 — 엔진·밸브 자동감지 결과",
        stage=STAGE_PI_RECEIVED,
        gpio_available=_gpio_available(),
        engine=type(engine_adapter).__name__,
        valve=type(valve_adapter).__name__ if valve_adapter is not None else "off",
        mode=mode,
        # 서버 settings 시린지 용량 반영 여부(None=서버 미제공→모드 기본 0.5mL 폴백·안전 급소 관측).
        syringeCapacityMl=syringe_capacity_from_settings(server_settings),
        settingsSnapshot="present" if server_settings is not None else "absent",
        # ⚠️ 키 구분(R3 P3-4): 여기는 스냅샷 원본 축(부재=None), 아래 WARN 의 settingsStroke 는
        #   폴백 적용 후 유효축 — 같은 키로 두 뜻을 찍으면 로그 대조가 어긋난다.
        settingsStrokeRaw=settings_stroke,
        adapterStroke=adapter_stroke,
        # 하드웨어 선언 각인(2026-09-02) — 무엇으로(model·ports) 어디서(source) 조립했는가.
        hardwareModel=hardware_profile.pump_model if hardware_profile is not None else None,
        hardwarePorts=hardware_profile.valve_port_count if hardware_profile is not None else None,
        hardwareSource=hardware_source,
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
    )
