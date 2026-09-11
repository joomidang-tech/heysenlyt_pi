"""용량 축 fail-closed 가드 (2026-09-02) — 봉투/명령의 선언 용량 ↔ 부팅 스냅샷 대조.

배경(E2E 감사 P1): pi 는 설정을 부팅 1회만 읽는다. admin 에서 시린지 용량만 바꾸고 재시작을
안 하면 서버는 새 용량으로 µL 을 조립하고 pi 는 옛 용량으로 스텝 환산 — **에러 0 인 채
10~50% 과소/과다 토출**(stroke 축 -1001 가드의 옆 축이 무방비였다). 서버가 봉투/명령에
조립 전제 용량(syringeCapacityMl)을 동봉하고, dispatcher 가 자기 pump_map 용량과 대조해
다르면 물리 실행 0 으로 CMD_VALIDATION_FAILED 거부한다. 선언 부재(구서버)=무검사(하위호환).
"""

from collections import deque
from pathlib import Path
from typing import Iterator

import pytest

from senlyt_pi.adapters.fake_engine_adapter import FakeEngineOutcome, FakeEnginePort
from senlyt_pi.app.dispatcher import Dispatcher
from senlyt_pi.core.command_set import CommandSet, CommandSetStatus
from senlyt_pi.core.pump_guard import SyringeSpec
from senlyt_pi.core.wire_messages import Command, RecipeStep
from senlyt_pi.persistence.file_idempotency_ledger import FileIdempotencyLedger
from senlyt_pi.pipeline.pump_sequencer import JobOutcome, PumpSequencer
from senlyt_pi.pipeline.recipe_resolver import RecipeResolver

SPEC = SyringeSpec(pump_full_stroke=12000, syringe_capacity_ml=1.25)


class FakeCommandSource:
    """레거시 command 축 fake — push 분을 poll() 이 소비(프로덕션 경로 _on_command 를 태운다)."""

    def __init__(self) -> None:
        self._pending: deque[Command] = deque()

    def push(self, c: Command) -> None:
        self._pending.append(c)

    def commands(self, device_id: str) -> Iterator:
        while self._pending:
            yield self._pending.popleft()


class FakeCommandSetSource:
    def __init__(self) -> None:
        self._pending: deque[CommandSet] = deque()

    def push(self, cs: CommandSet) -> None:
        self._pending.append(cs)

    def command_sets(self, device_id: str) -> Iterator[CommandSet]:
        while self._pending:
            yield self._pending.popleft()


class SinkRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, "str | None"]] = []

    def __call__(self, cs: CommandSet, status: CommandSetStatus, error_code) -> None:
        self.events.append(
            (cs.command_set_id, status.wire, error_code.wire if error_code else None)
        )


@pytest.fixture
def rig(tmp_path: Path):
    """pump_map(cap 1.25) 주입 dispatcher — 용량 가드가 켜진 조립.

    published = order status 역보고 기록(레거시 축 거부의 서버 흔적 — R4 P1-2 그물).
    """
    fake = FakeEnginePort()
    fake.script_all(FakeEngineOutcome.ACK)
    ledger = FileIdempotencyLedger.open(tmp_path / "l.log")
    seq_counter = iter(range(10_000))
    pump_map = {1: SPEC, 2: SPEC}
    published: list[tuple] = []
    sequencer = PumpSequencer(
        ledger=ledger,
        engine=fake,
        resolver=RecipeResolver(pump_map),
        request_id_gen=lambda: f"req-{next(seq_counter)}",
        publisher=lambda *a: published.append(a),
        now_iso=lambda: "2026-09-02T00:00:00.000Z",
    )
    cmd_source = FakeCommandSource()
    source = FakeCommandSetSource()
    sink = SinkRecorder()
    dispatcher = Dispatcher(
        device_id="dev-A",
        command_source=cmd_source,
        sequencer=sequencer,
        interpret=lambda c: [RecipeStep(idx=0, pump_addr=1, flavor="legacy", volume=200)],
        commandset_source=source,
        commandset_sink=sink,
        now_s=lambda: 1788307200.0,  # 2026-09-02T00:00:00Z epoch — 신선도 게이트 통과.
        pump_map=pump_map,
    )
    yield dispatcher, source, sink, fake, cmd_source, published
    ledger.close()


def envelope(cid: str, *, declared: "float | None") -> CommandSet:
    order_id, attempt = cid.rsplit(":", 1)
    return CommandSet(
        command_set_id=cid,
        device_id="dev-A",
        kind="manufacture",
        steps=(RecipeStep(idx=0, pump_addr=1, flavor="f", volume=200),),
        status=CommandSetStatus.QUEUED,
        created_at="2026-09-02T00:00:00.000Z",
        created_by="server",
        source_order_id=order_id,
        attempt=int(attempt),
        trace_id=f"trace-{cid}",
        syringe_capacity_ml=declared,
    )


def command(cid: str, *, declared: "float | None") -> Command:
    order_id, attempt = cid.rsplit(":", 1)
    return Command(
        id=cid,
        order_id=order_id,
        attempt=int(attempt),
        device_id="dev-A",
        recipe=(RecipeStep(idx=0, pump_addr=1, flavor="f", volume=200),),
        trace_id=f"trace-{cid}",
        created_at="2026-09-02T00:00:00.000Z",
        syringe_capacity_ml=declared,
    )


class TestEnvelopeAxis:
    def test_mismatch_rejected_before_any_motion(self, rig):
        d, source, sink, fake, _cmds, published = rig
        source.push(envelope("o1:1", declared=0.5))  # 서버 0.5 ≠ 스냅샷 1.25
        d.poll_commandsets()
        assert d.reports[-1].outcome is JobOutcome.VALIDATION_FAILED
        # 물리 실행 0 — 어떤 모션도 나가지 않았다.
        assert fake.dispense_calls == [] and fake.batch_calls == []
        assert fake.initialize_count == 0
        # 봉투는 FAILED + CMD_VALIDATION_FAILED 로 정직하게 종단(무성 과소토출 아님).
        assert ("o1:1", "failed", "CMD_VALIDATION_FAILED") in sink.events
        # 주문 status 역보고도 남는다(R4 P1-2 — 서버 흔적 있는 거부).
        assert any(a[5] == "trace-o1:1" for a in published), published

    def test_inflight_duplicate_does_not_fail_transition(self, rig):
        # R5 P3-1 그물 — 같은 멱등키가 이미 claim(미settle) 상태에서 봉투가 오면, 거부 종단은
        #   DUPLICATE 로 접히고 봉투를 FAILED 로 **덮지 않는다**(실행 중 잡을 FAILED 로 오보 금지).
        d, source, sink, _fake, _cmds, _pub = rig
        d.sequencer.ledger.check_and_claim("o7:1", "trace-o7:1")  # 레거시 축이 잡은 in-flight 재현.
        source.push(envelope("o7:1", declared=0.5))
        d.poll_commandsets()
        assert d.reports[-1].outcome is JobOutcome.DUPLICATE_DROPPED
        assert ("o7:1", "failed", "CMD_VALIDATION_FAILED") not in sink.events

    def test_match_and_absent_pass(self, rig):
        d, source, sink, _fake, _cmds, _pub = rig
        source.push(envelope("o2:1", declared=1.25))  # 정합
        source.push(envelope("o3:1", declared=None))  # 구서버(선언 없음) 하위호환
        d.poll_commandsets()
        done = [e for e in sink.events if e[1] == "done"]
        assert {c for (c, _s, _e) in done} == {"o2:1", "o3:1"}


class TestLegacyCommandAxis:
    def test_mismatch_rejected_via_poll(self, rig):
        # 프로덕션 진입점(poll→_on_command) 경유(R4 P1-1 — dispatch_once 만 덮던 그물 봉합).
        d, _source, _sink, fake, cmds, published = rig
        cmds.push(command("o4:1", declared=0.5))
        d.poll()
        assert d.reports[-1].outcome is JobOutcome.VALIDATION_FAILED
        assert fake.dispense_calls == [] and fake.batch_calls == []
        # 레거시 축의 유일한 서버 흔적 = order status FAILED 역보고(R4 P1-2).
        failed = [a for a in published if a[4] == "o4:1"]
        assert failed and failed[0][0].name == "FAILED", published
        # snapshot 재파생 재거부는 ledger DUPLICATE 로 조용히 접힌다(재보고 0 — 소음 방지).
        n_before = len(published)
        cmds.push(command("o4:1", declared=0.5))
        d.poll()
        assert len(published) == n_before
        assert d.reports[-1].outcome is JobOutcome.DUPLICATE_DROPPED

    def test_mismatch_rejected(self, rig):
        d, _source, _sink, fake, _cmds, _pub = rig
        report = d.dispatch_once(command("o4:1", declared=0.5))
        assert report.outcome is JobOutcome.VALIDATION_FAILED
        assert fake.dispense_calls == [] and fake.batch_calls == []

    def test_match_passes(self, rig):
        d, _source, _sink, _fake, _cmds, _pub = rig
        report = d.dispatch_once(command("o5:1", declared=1.25))
        assert report.outcome is JobOutcome.COMPLETED


class _NullSink:
    def report_status(self, report) -> None:
        pass

    def send_heartbeat(self, hb) -> None:
        pass

    def ship_trace(self, spans) -> None:
        pass


class TestDaemonWiring:
    """R4.5 P2-A 그물 — 데몬이 '용량 출처' 계약을 실제로 지키는지(dispatcher 수준 계약과 별개)."""

    @staticmethod
    def _daemon(tmp_path: Path, resolver: RecipeResolver):
        from senlyt_pi.app.daemon import DaemonDeps, SenlytDaemon

        fake = FakeEnginePort()
        fake.script_all(FakeEngineOutcome.ACK)
        ledger = FileIdempotencyLedger.open(tmp_path / "dw.log")
        daemon = SenlytDaemon(
            DaemonDeps(
                device_id="dev-A",
                command_source=FakeCommandSource(),
                status_sink=_NullSink(),
                engine=fake,
                ledger=ledger,
                resolver=resolver,
                capacity_from_settings=resolver.capacity_from_settings,
                heartbeat_interval_s=0.0,
            )
        )
        return daemon, ledger

    def test_snapshot_origin_arms_guard_fallback_disarms(self, tmp_path):
        armed = RecipeResolver({1: SPEC})
        armed.capacity_from_settings = True  # build_resolver 가 스냅샷 유래일 때 각인하는 값.
        d1, l1 = self._daemon(tmp_path, armed)
        assert d1._dispatcher._pump_map == {1: SPEC}
        l1.close()

        fallback = RecipeResolver({1: SPEC})  # 기본 False = 폴백(추측값) — 가드 꺼져야 한다.
        d2, l2 = self._daemon(tmp_path, fallback)
        assert d2._dispatcher._pump_map == {}
        l2.close()

    def test_build_resolver_marks_origin(self):
        # 출처 각인의 단일 지점(build_resolver) — 스냅샷 용량 유무가 그대로 플래그가 된다.
        from senlyt_pi.app.bootstrap import build_resolver

        env = {"PUMP_ADDRESSES": "flavor:1,2"}
        with_snapshot = build_resolver(env, server_settings={"pumpPreset": {"syringeCapacityMl": 1.25}})
        assert with_snapshot.capacity_from_settings is True
        assert with_snapshot.pump_map[1].syringe_capacity_ml == 1.25
        without = build_resolver(env, server_settings=None)
        assert without.capacity_from_settings is False


class TestGuardScope:
    def test_no_pump_map_means_no_check(self, tmp_path):
        # 신서버 + 구 daemon 조립(pump_map 미주입) 또는 스냅샷 부재(폴백 용량) — 가드 무검사.
        #   폴백 0.5 는 추측값이라 이를 근거로 서버 선언을 거부하면 부팅 순단 1회가 제조·세척
        #   전량 거부(벽돌)로 번진다(R4 P0-1). 무주입=무검사가 계약.
        fake = FakeEnginePort()
        fake.script_all(FakeEngineOutcome.ACK)
        ledger = FileIdempotencyLedger.open(tmp_path / "l2.log")
        seq_counter = iter(range(100))
        sequencer = PumpSequencer(
            ledger=ledger,
            engine=fake,
            resolver=RecipeResolver({1: SPEC, 2: SPEC}),
            request_id_gen=lambda: f"req-{next(seq_counter)}",
            now_iso=lambda: "2026-09-02T00:00:00.000Z",
        )
        d = Dispatcher(
            device_id="dev-A",
            command_source=FakeCommandSource(),
            sequencer=sequencer,
            interpret=lambda c: [],
        )
        report = d.dispatch_once(command("o6:1", declared=0.5))  # 선언 0.5 ≠ spec 1.25 여도
        assert report.outcome is JobOutcome.COMPLETED  # 무검사 — 거부 없음.
        ledger.close()


class TestWireParsing:
    def test_command_set_from_json_reads_capacity(self):
        cs = CommandSet.from_json(
            {
                "commandSetId": "o9:1",
                "deviceId": "dev-A",
                "kind": "manufacture",
                "steps": None,
                "status": "queued",
                "createdAt": "2026-09-02T00:00:00.000Z",
                "createdBy": "server",
                "sourceOrderId": "o9",
                "attempt": 1,
                "syringeCapacityMl": 0.25,
            }
        )
        assert cs.syringe_capacity_ml == 0.25
        # 부재 = None(하위호환).
        cs2 = CommandSet.from_json(
            {
                "commandSetId": "o9:2",
                "deviceId": "dev-A",
                "kind": "manufacture",
                "steps": None,
                "status": "queued",
                "createdAt": "2026-09-02T00:00:00.000Z",
                "createdBy": "server",
                "sourceOrderId": "o9",
                "attempt": 2,
            }
        )
        assert cs2.syringe_capacity_ml is None

    def test_broken_capacity_value_is_tolerated_not_fatal(self):
        # 선택 필드는 tolerant reader(R4 P3) — 값이 깨져도 봉투를 죽이지(파싱 skip→큐 교착)
        #   않고 None(무검사) 으로 강등한다.
        cs = CommandSet.from_json(
            {
                "commandSetId": "o9:3",
                "deviceId": "dev-A",
                "kind": "manufacture",
                "steps": None,
                "status": "queued",
                "createdAt": "2026-09-02T00:00:00.000Z",
                "createdBy": "server",
                "sourceOrderId": "o9",
                "attempt": 3,
                "syringeCapacityMl": "not-a-number",
            }
        )
        assert cs.syringe_capacity_ml is None
        # 왕복 대칭 — to_json 이 필드를 유실하지 않는다(값이 있을 때).
        cs2 = CommandSet.from_json({**cs.to_json(), "syringeCapacityMl": 0.5})
        assert cs2.to_json()["syringeCapacityMl"] == 0.5

    def test_command_from_json_reads_capacity(self):
        c = Command.from_json(
            {
                "id": "o9:1",
                "orderId": "o9",
                "attempt": 1,
                "deviceId": "dev-A",
                "recipe": None,
                "traceId": "t",
                "createdAt": "2026-09-02T00:00:00.000Z",
                "syringeCapacityMl": 1.25,
            }
        )
        assert c.syringe_capacity_ml == 1.25
