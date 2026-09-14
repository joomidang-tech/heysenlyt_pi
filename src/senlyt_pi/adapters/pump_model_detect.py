"""펌프 기종 자동 인식(2026-09-14) — 어댑터를 조립하기 **전에** 실물에 물어본다.

왜: 종전 pi 는 서버 선언(센소리움 pumpModel)대로 어댑터를 조립했고, 선언이 실물과 어긋나면
  ① sy01b 어댑터가 Tecan 에 `U…R`(NVM 기록)을 내거나 ② 게이트가 조용히 스텝을 버려 "제조 시작됨·
  아무 일도 없음" 이 됐다. 사용자 요구(2026-09-14): "pi 는 Runze 든 Tecan 이든 스스로 인식해 바로 그
  기종으로 동작한다." 그래서 조립의 1순위 근거를 선언에서 **실물 지문**으로 옮긴다.

어떻게: 임시 Sy01b 베이스 인스턴스로 예상 주소에 `?`(응답 확인) → `&`(펌웨어 리포트)만 읽는다.
  둘 다 read-only Report 라 어느 기종에 보내도 모션·NVM 기록이 없다(Runze 2026-09-11 실측 ·
  Tecan 벤치 실측). 초기화 프리앰블(U/N0R)은 `_setup` 경로에만 있어 여기서는 절대 나가지 않는다.
  임시 인스턴스는 `port_resolver` 없이(포트를 옮겨 다니지 않게) 만들고 finally 에서 닫는다 —
  이중 open 방지(`_pyserial_factory` 는 exclusive 가 아니다).

착지 규칙(전부 판정 없음·관측 그대로 보고):
  · 응답 주소 전부 같은 기종 → model=M (조립은 M)
  · 응답은 있는데 지문을 못 읽음/미지 값 → model=None, unreadable=True → **Undeclared(fail-closed)**
    (선언으로 폴백하지 않는다 — 검증 P0-1: 감지와 게이트가 같은 신호라 둘 다 눈이 멀 때 선언 추측은 곧 U-NVM)
  · 기종이 섞임 → model=None, mixed=True → Undeclared
  · 응답 0 → model=None, responding=() → 종전 경로(선언>캐시>Undeclared) — 펌프 전원이 늦게 켜지는
    흔한 경우라 재발견 재기동(`on_pumps_seen_unmapped`)이 다시 여기로 데려온다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..obs.log import STAGE_PI_RECEIVED, StructuredLogger


@dataclass(frozen=True, slots=True)
class DetectResult:
    model: "str | None"  # "sy01b" | "tecan_xcalibur" | None
    responding: tuple[int, ...]  # `?` 에 응답한 주소(오름차순)
    fingerprints: dict[int, str] = field(default_factory=dict)  # addr → `&` 데이터 블록
    mixed: bool = False  # 응답 주소들의 기종이 둘 이상
    unreadable: bool = False  # 응답은 있는데 분류 가능한 지문이 하나도 없음
    # 근거 강도(검증 P0-1) — 응답 주소 전부가 **실측 정확값**(KNOWN_FINGERPRINTS)과 일치하면 True.
    #   False = 형식 규칙만으로 분류(Runze `d.dd` 는 과광의) → bootstrap 이 선언과 대조해 어긋나면 fail-closed.
    strong: bool = False

    @property
    def source(self) -> str:
        if self.model is not None:
            return "detected"
        if self.mixed:
            return "mixed"
        if self.unreadable:
            return "undetected"
        return "no_pumps"


Detector = Callable[[str, Sequence[int]], DetectResult]


def detect_pump_model(
    port: str,
    addresses: Sequence[int],
    *,
    serial_factory=None,
    logger: "StructuredLogger | None" = None,
) -> DetectResult:
    """포트 하나·예상 주소들 → DetectResult. 어떤 예외도 밖으로 내지 않는다(감지 실패 = 응답 0 취급)."""
    from .sy01b_engine_adapter import (
        Sy01bEngineAdapter,
        classify_fingerprint,
        fingerprint_confidence,
    )

    adapter = Sy01bEngineAdapter(port=port, serial_factory=serial_factory, logger=logger)
    responding: list[int] = []
    try:
        for addr in addresses:
            try:
                if adapter.probe(addr):  # probe 가 응답 주소마다 `&` 를 1회 읽어 캐시한다.
                    responding.append(int(addr))
            except Exception:  # noqa: BLE001 — 주소 하나의 실패가 감지 전체를 막지 않는다.
                continue
        fps = adapter.pump_fingerprints()
    except Exception:  # noqa: BLE001
        fps = {}
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass
    responding.sort()
    if not responding:
        return DetectResult(model=None, responding=())
    models = {a: classify_fingerprint(fps.get(a)) for a in responding}
    known = {m for m in models.values() if m is not None}
    result: DetectResult
    if len(known) == 1 and all(m is not None for m in models.values()):
        strong = all(fingerprint_confidence(fps.get(a)) == "exact" for a in responding)
        result = DetectResult(
            model=next(iter(known)), responding=tuple(responding), fingerprints=dict(fps), strong=strong
        )
    elif len(known) >= 2:
        result = DetectResult(model=None, responding=tuple(responding), fingerprints=dict(fps), mixed=True)
    else:
        # 일부/전부 미분류(무응답·미지 값) — 부분 인식으로 조립하지 않는다(그 주소에 남의 프레임이 나간다).
        result = DetectResult(
            model=None, responding=tuple(responding), fingerprints=dict(fps), unreadable=True
        )
    if logger is not None:
        logger.event(
            "펌프 기종 자동 인식 — 부팅 실물 지문",
            stage=STAGE_PI_RECEIVED,
            port=port,
            responding=list(responding),
            fingerprints={str(a): v for a, v in fps.items()},
            model=result.model,
            source=result.source,
            strong=result.strong,
        )
    return result
