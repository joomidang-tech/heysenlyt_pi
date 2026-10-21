"""주문 status PATCH 에 실패 사유 코드를 싣는다(2026-10-02 prod 실측).

서버 위생 하드락은 "토출 0" 코드(CMD_VALIDATION_FAILED 등)면 잠그지 않는다. 종전엔 주문 status PATCH 본문에
errorCode 가 빠져 서버가 "코드 없음 = 나갔을 수 있다"로 읽고 잠갔다 — 조립 거부(sensorium_mismatch) 주문이
토출 0 으로 실패했는데도 세척을 강제했다(prod 주문 jMfZ26DIC0y1mPViBaIT · 2026-10-02 09:00).
"""

from __future__ import annotations

from typing import Any

from senlyt_pi.adapters.http_status_sink_adapter import HttpStatusSinkAdapter
from senlyt_pi.core.pump_guard import StatusErrorCode
from senlyt_pi.core.wire_messages import StatusReport


def _sink(calls: list[dict[str, Any]]) -> HttpStatusSinkAdapter:
    def fake(method: str, url: str, *, body: Any = None, headers: Any = None, timeout: Any = None):
        calls.append({"method": method, "url": url, "body": body})
        return 200, {"applied": True}

    return HttpStatusSinkAdapter(base_url="http://srv", bearer_token="t", mode="fragrance", request=fake)


def _report(phase: str, code: StatusErrorCode | None) -> StatusReport:
    return StatusReport(
        id="ord1:1",
        phase=phase,
        step_k=0,
        step_n=0,
        error_code=code,
        request_id="r1",
        trace_id="t1",
        updated_at="2026-10-02T09:00:03Z",
    )


def test_failed_status_patch_carries_error_code() -> None:
    calls: list[dict[str, Any]] = []
    _sink(calls).report_status(_report("FAILED", StatusErrorCode.CMD_VALIDATION_FAILED))
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert patches, calls
    assert patches[0]["body"]["status"] == "FAILED"
    assert patches[0]["body"]["errorCode"] == "CMD_VALIDATION_FAILED"


def test_status_patch_without_error_code_has_no_key() -> None:
    calls: list[dict[str, Any]] = []
    _sink(calls).report_status(_report("COMPLETED", None))
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert patches and "errorCode" not in patches[0]["body"]
