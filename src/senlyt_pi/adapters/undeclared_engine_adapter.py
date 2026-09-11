"""미확정 하드웨어 거부 어댑터 (2026-09-02 — 센소리움 단일 SoT · Undeclared fail-closed).

부팅 스냅샷·로컬 캐시 어디에도 유효한 펌프 모델 선언이 없을 때 조립된다 — "정보 부재의
방어"(사용자 확정). 어떤 물리 모션도 시작하지 않고 모든 명령을 UNDECLARED_HW_RAW_CODE
(-1002·permanent)로 정직하게 거부한다. 도착 봉투는 FAILED 로 종단(멱등 원장 정상 기록)되고,
pumpHealth 는 빨강으로 표면화된다. 이탈 경로는 senlytd 의 주기 재fetch 성공 → 정상 종료 →
systemd 재기동(상태모델 D3) 하나다.

⛔ "폴백=sy01b 무조건"을 쓰지 않는 이유: sy01b 초기화 프리앰블 `U{code},{stall}R` 은 XCalibur
에서 NVM 설치 기록이라, tecan 이라 선언됐던 기기가 미확정 부팅에서 sy01b 로 조립되면 실물
NVM 이 오염된다. 미확정 = 무동작이 유일하게 안전하다.
"""

from __future__ import annotations

from typing import Iterable

from ..core.pump_guard import UNDECLARED_HW_RAW_CODE
from ..ports.engine_port import (
    EngineBatchCommand,
    EngineDispenseCommand,
    EngineOpCommand,
    EngineResult,
)

_DETAIL = (
    "하드웨어 선언 미확정 — 서버 스냅샷·로컬 캐시 어디에도 펌프 모델이 없어 모션을 거부합니다. "
    "네트워크 확인 후 senlytd 재시작(또는 admin 센소리움 배정) 필요"
)


class UndeclaredEngineAdapter:
    """모든 모션 거부 EnginePort — 물리 시리얼을 열지 않는다(포트 오픈 0)."""

    def aspirate(self, cmd: EngineDispenseCommand) -> EngineResult:  # noqa: ARG002
        return EngineResult(raw_error_code=UNDECLARED_HW_RAW_CODE, detail=_DETAIL)

    def dispense(self, cmd: EngineDispenseCommand) -> EngineResult:  # noqa: ARG002
        return EngineResult(raw_error_code=UNDECLARED_HW_RAW_CODE, detail=_DETAIL)

    def dispense_batch(self, cmd: EngineBatchCommand) -> EngineResult:  # noqa: ARG002
        return EngineResult(raw_error_code=UNDECLARED_HW_RAW_CODE, detail=_DETAIL)

    def initialize(self) -> EngineResult:
        return EngineResult(raw_error_code=UNDECLARED_HW_RAW_CODE, detail=_DETAIL)

    def run_op(self, cmd: EngineOpCommand) -> EngineResult:  # noqa: ARG002
        return EngineResult(raw_error_code=UNDECLARED_HW_RAW_CODE, detail=_DETAIL)

    def emergency_stop_all(self, addrs: "Iterable[int]") -> None:  # noqa: ARG002
        # 물리 연결이 없으므로 no-op — estop 래치 자체는 데몬 공유 이벤트가 담당.
        return None

    def clear_estop(self) -> None:
        return None

    def probe(self, addr: int) -> bool:  # noqa: ARG002
        # 선언이 없으면 버스 방언(2400/9600·?/Q)도 모른다 — 미실측 프레임을 쏘지 않는다(무프로브).
        return False

    def close(self) -> None:  # EnginePort 정식 계약(2026-09-03 승격) — 자원 없음 no-op.
        return None

    def signal_stop(self) -> None:  # 동상 — 폴링 자체가 없다.
        return None
