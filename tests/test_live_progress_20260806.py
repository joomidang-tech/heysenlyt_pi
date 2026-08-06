"""라이브 진행 스냅샷(live_progress) + heartbeat jobProgress 동봉 (2026-08-06).

QA "[admin] 튜브필링 UI" 원안(현재 포트 표시) — 통신 추가 없이 기존 10s 하트비트에
실행 중 잡의 (commandSetId, stepsDone, stepN)을 편승시킨다.
  - Sequencer: 잡 시작 시 (id, 0, N) → stage 경계마다 갱신 → 종결 시 None(잔상 금지).
  - Heartbeat.to_json: job_progress 있으면 jobProgress 키 방출, 유휴면 키 미방출(P-4).
"""

from pathlib import Path

import pytest

from senlyt_pi.adapters.fake_engine_adapter import FakeEngineOutcome, FakeEnginePort
from senlyt_pi.core.order_status import DispensePhase
from senlyt_pi.core.pump_guard import SyringeSpec
from senlyt_pi.core.wire_messages import Heartbeat, RecipeStep
from senlyt_pi.persistence.file_idempotency_ledger import FileIdempotencyLedger
from senlyt_pi.pipeline.pump_sequencer import JobOutcome, PumpSequencer
from senlyt_pi.pipeline.recipe_resolver import RecipeResolver

SPEC = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=1.25)


@pytest.fixture
def ledger(tmp_path: Path):
    ledger = FileIdempotencyLedger.open(tmp_path / "l.log")
    yield ledger
    ledger.close()


@pytest.fixture
def fake() -> FakeEnginePort:
    fake = FakeEnginePort()
    fake.script_all(FakeEngineOutcome.ACK)
    return fake


def step(idx: int, addr: int, vol: float) -> RecipeStep:
    return RecipeStep(idx=idx, pump_addr=addr, flavor="f", volume=vol)


def test_live_progress_tracks_steps_and_clears_after_job(ledger, fake):
    """실행 중엔 (id, k, N)이 발행 시점의 진행과 일치, 종결 후엔 None(하트비트 잔상 금지)."""
    snapshots: list = []

    def publisher(phase, step_k, step_n, error_code, command_id, trace_id):
        # 발행 시점의 live_progress 를 캡처 — 하트비트 스레드가 읽는 값과 동일 소스.
        snapshots.append((phase, step_k, seq.live_progress))

    seq = PumpSequencer(
        ledger=ledger,
        engine=fake,
        resolver=RecipeResolver({1: SPEC, 2: SPEC}),
        request_id_gen=lambda: "req-0",
        publisher=publisher,
        now_iso=lambda: "2026-08-06T00:00:00.000Z",
    )
    report = seq.submit(
        command_id="mnt-1",
        trace_id="t-1",
        steps=[step(0, 1, 100.0), step(1, 1, 100.0), step(2, 2, 100.0)],
    )
    assert report.outcome is JobOutcome.COMPLETED

    # PROGRESS 발행 시점마다 live_progress 가 그 진행도와 일치했는지.
    progress_snaps = [s for s in snapshots if s[0] is DispensePhase.PROGRESS]
    assert progress_snaps, "PROGRESS 발행이 없다 — 직렬 3스텝이면 중간 진행보고가 있어야 한다"
    for _, step_k, live in progress_snaps:
        assert live == ("mnt-1", step_k, 3)

    # 잡 종결 후 스냅샷 해제 — 유휴 하트비트에 jobProgress 잔상이 남지 않는다.
    assert seq.live_progress is None


def test_heartbeat_to_json_job_progress(ledger, fake):
    """job_progress 있으면 jobProgress 방출, 없으면(유휴) 키 자체 미방출(P-4)."""
    with_progress = Heartbeat(
        device_id="d", queue_depth=1, job_progress=("mnt-1", 3, 19)
    ).to_json()
    assert with_progress["jobProgress"] == {
        "commandSetId": "mnt-1",
        "stepsDone": 3,
        "stepN": 19,
    }

    idle = Heartbeat(device_id="d", queue_depth=0).to_json()
    assert "jobProgress" not in idle
