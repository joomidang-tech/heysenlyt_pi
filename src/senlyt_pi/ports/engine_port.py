"""EnginePort — 시린지 펌프 구동 포트(인터페이스만) — SoT §6-7.

Dart `lib/ports/engine_port.dart` 포팅.

⛔ 안전상 유보(이번 웨이브 범위 밖): 펌프 구동 실로직(실토출)·Sequencer 는 구현하지 않는다.
   실어댑터(sy01b 시리얼 RR·pyserial)는 `adapters/` 에 TODO 스텁으로만 둔다.

에러코드 분류·재시도 정책은 core `pump_guard.classify_engine_error_code`(§6-7).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ..core.pump_guard import SyringeSpec


@dataclass(frozen=True, slots=True)
class EngineDispenseCommand:
    """단일 펌프 토출 명령(해석된 스텝) — 서버 recipe step → SyringeSpec 파생 후."""

    pump_addr: int
    volume_ul: float
    # SyringeSpec.steps_for_volume_ul 로 파생된 스텝수(하드코딩 금지·§6-4).
    steps: int
    spec: SyringeSpec
    # ── 회전 밸브 구멍 — 한 번의 토출 = `I{in_port}` → `P{steps}` → `O{out_port}` → `D{steps}`. ──
    #   **서버가 기기설정에서 해석해 준 값**이다(pi 는 배치를 모른다). pump_addr 만으로는 한 펌프
    #   다포트 헤드의 여러 액체를 구분할 수 없어, 이게 없으면 어느 통에서 빨지를 정할 수 없다.
    #
    #   ⚠️ **v1.2.0 실 토출 경로는 flavor·fragrance 모두 두 포트를 항상 싣는다**(서버 조립 게이트가
    #   inPort/outPort 를 강제). None 은 **구계약 스텝**(포트 개념 이전)에만 남는 값이다 — 그 경우
    #   어댑터는 있는 포트만 회전하고 없는 회전은 건너뛴 채 P/D 를 수행한다(밸브가 이전 위치에 머묾).
    #   즉 None 스텝의 흡입/배출 대상은 **직전 밸브 위치에 의존**하므로 실 배치 경로에선 만들지 말 것
    #   (생성형 폴백 flavor_recipe_to_steps 는 빈 스텝→drop 으로 이미 봉인·recipe_resolver 참조).
    in_port: int | None = None
    out_port: int | None = None
    # 속도(Hz)·경사 — 서버가 전역설정 × 포트 오버라이드를 해석해 확정(더 느린 쪽). None = 어댑터 기본.
    aspirate_speed_hz: int | None = None
    dispense_speed_hz: int | None = None
    slope: int | None = None


@dataclass(frozen=True, slots=True)
class EngineBatchCommand:
    """배치 흡입 명령(§9-1 v3 · 2026-07-21 "1향료 1펌핑") — 여러 흡입 → 한 번 배출.

    한 주사기에 `aspirations` 순서대로 **누적 절대 흡입**(`I{in}` → `A{누적steps}`) 후 `out_port`
    로 한 번 배출(`A0`)한다. 각 aspiration 은 `(in_port, steps, volume_ul, aspirate_speed_hz)`
    — steps 는 SyringeSpec 파생값(하드코딩 금지)이고, 어댑터가 순서대로 **누적 합산**해 절대
    이동한다. 누적합 ≤ 시린지 용량은 resolver 가 이미 검증(과충전 방지)했다.
    """

    pump_addr: int
    out_port: int
    # 배출 속도(Hz)·경사 — 서버가 배치 흡입 포트들의 오버라이드 중 min 으로 확정. None = 어댑터 기본.
    dispense_speed_hz: int | None
    slope: int | None
    spec: SyringeSpec
    # (in_port, steps, volume_ul, aspirate_speed_hz) — 흡입 순서 = 실행 순서(누적 절대이동).
    aspirations: tuple[tuple[int, int, float, "int | None"], ...]


@dataclass(frozen=True, slots=True)
class EngineResult:
    """엔진 실행 결과."""

    # 엔진 raw errorCode(정수) — classify_engine_error_code 입력(§6-7). 0=정상.
    raw_error_code: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class EngineOpCommand:
    """엔진 조작 명령 — 토출이 아닌 정비 동작(관제 정비 버튼).

    **의도만 온다** — `A12000` 같은 펌프 문법은 서버가 모르고, 번역은 어댑터가 한다.
    `spec` 은 조작이 용량 파생을 타기 때문에 필요하다(예: plungerFull = 그 시린지의 풀스트로크).
    """

    pump_addr: int
    # "estop" | "initialize" | "plunger_full" | "plunger_home" — 서버 wire `op` 의 pi 표기.
    op: str
    spec: SyringeSpec
    # 플런저 이동 **전** 회전할 밸브 포트(v1.1.0 시퀀스: 흡입=air/배출=output — 서버 해석값).
    #   None = 회전 생략(구 서버 하위호환·estop/initialize).
    valve_port: int | None = None
    # (op=initialize 전용·2026-07-21 QA) 홈 스트로크 흡입=air / 배출·주차=output — 서버 해석값.
    #   None = 펌웨어 기본값 + SAFE_PORT 주차(구 서버 하위호환).
    init_in_port: int | None = None
    init_out_port: int | None = None


# 엔진 조작 op — 서버 wire `EngineOp` 와 1:1(camelCase → snake_case).
OP_ESTOP = "estop"
OP_INITIALIZE = "initialize"
OP_PLUNGER_FULL = "plunger_full"
OP_PLUNGER_HOME = "plunger_home"
ENGINE_OPS = (OP_ESTOP, OP_INITIALIZE, OP_PLUNGER_FULL, OP_PLUNGER_HOME)


class EnginePort(Protocol):
    """시린지 펌프 엔진 포트."""

    def aspirate(self, cmd: EngineDispenseCommand) -> EngineResult:
        """단일 스텝 흡입(aspirate). ⛔ 실토출 로직 = 이후 웨이브."""
        ...

    def dispense(self, cmd: EngineDispenseCommand) -> EngineResult:
        """단일 스텝 배출(dispense). ⛔ 실토출 로직 = 이후 웨이브."""
        ...

    def dispense_batch(self, cmd: EngineBatchCommand) -> EngineResult:
        """배치 흡입(§9-1 v3) — 여러 액체를 순서대로 누적 흡입 후 한 번 배출.

        `I{in}` → `A{누적steps}`(절대·누적) 반복 → `O{out}` → `A0`(한 번 배출). 어느 단계든
        에러면 즉시 반환하고, 분류·재시도는 상위 `EngineExecutor` 가 한다.
        """
        ...

    def initialize(self) -> EngineResult:
        """셋업 캐시 무효화 — 다음 토출 때 그 펌프에 TR+U200+Z 를 다시 건다."""
        ...

    def run_op(self, cmd: EngineOpCommand) -> EngineResult:
        """엔진 조작(정비) 실행 — 의도(op)를 펌프 문법으로 번역해 수행한다."""
        ...

    def emergency_stop_all(self, addrs: "Iterable[int]") -> None:
        """긴급정지(§9-4) — 전 펌프에 즉시 정지(TR)를 걸고 in-flight 모션 폴을 협조적으로 중단한다.

        제조 중에도 감시 스레드에서 안전하게 호출된다(어댑터가 버스 락으로 직렬화). 실토출 어댑터만
        의미가 있고, 테스트 더블/미구현 엔진은 no-op 이어도 무방하다(계약상 존재만 강제).
        """
        ...

    def clear_estop(self) -> None:
        """긴급정지 래치 해제 — 복구(초기화) 경로가 부른다."""
        ...

    # ── 2026-09-03 정식 승격(검증 5팀 P1-3) — close/signal_stop 은 정비 툴(hwtool)과
    #   테스트가 이미 소비하던 "사실상 계약"이었다(Protocol 밖 직호출). ⚠️ 데몬 자체는 아직
    #   둘 다 직접 부르지 않는다(종료 시 fd 는 프로세스 종료로 회수) — "데몬도 쓰니 안전"으로
    #   오독하지 말 것(2026-09-03 리뷰 정정). Protocol 은 구조적 타이핑이라 승격은
    #   런타임 no-op — 대신 형상(핀 구세대·불완전 더블)이 타입/계약 테스트에서 드러난다.
    #   ⚠️ probe·health_probe·initialize_polled 는 여기 넣지 **않는다** — 소비자(daemon·
    #   senlytd·pump_sequencer)가 getattr 로 "능력 감지"하고, **부재 자체가 의미를 갖는
    #   설계된 seam** 이다(fake = probe 부재 → 재발견 미주입 / Undeclared = health_probe
    #   부재 → 데몬 발화 안 함 / polled 부재 → per-pump 폴백). 아래 capability Protocol 로
    #   분리해 "지원 어댑터의 시그니처"만 타입으로 고정한다.

    def close(self) -> None:
        """시리얼/자원 정리(멱등) — 종료·재연결 경로. 더블은 no-op 허용."""
        ...

    def signal_stop(self) -> None:
        """진행 중 폴링 협조 중단(취소·SIGTERM) — 더블은 no-op 허용."""
        ...


# ── 능력(capability) 확장 Protocol — "있으면 이 시그니처, 없으면 그 자체가 신호" ──────────


class ProbeCapableEngine(Protocol):
    """부팅 자동인식 능력 — 부재 = 관측 불가 더블(재발견 정책 미주입 신호·senlytd)."""

    def probe(self, addr: int) -> bool:
        """그 주소에 펌프가 응답하는가(모션 무발생·read-only)."""
        ...


class HealthCapableEngine(Protocol):
    """하트비트 건강 판정 능력 — 부재 = 데몬이 그 어댑터로는 발화하지 않는다는 신호."""

    def health_probe(self, addr: int) -> str:
        """"ok" | "garbled" | "silent" (read-only·래치 비소진)."""
        ...


class PolledInitEngine(Protocol):
    """폴 조기완료 초기화 능력 — 부재 = per-pump `run_op(initialize)` 폴백(의도된 거동)."""

    def initialize_polled(
        self,
        addrs: "Iterable[int]",
        spec,
        init_in_port: "int | None" = None,
        init_out_port: "int | None" = None,
        ports_by_addr=None,
    ) -> "dict[int, int]":
        """전 펌프 동시 초기화(주소지정 발사 + Bit5 폴 조기완료) — addr→error_code."""
        ...
