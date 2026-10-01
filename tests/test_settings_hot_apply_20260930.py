"""설정 변경 적용(2026-09-30 · 04_erd §9-3 단일 규칙).

모든 설정 변경은 서버 `settingsHash` 를 바꾼다 → 서버는 pi 가 새 해시를 보고할 때까지 제조를 보류한다 → pi 는 소비 루프 사이(유휴)에
적용하고 새 해시를 보고한다. 통 배치·용량(재초기화)·튠·계약 = 재시작 없음. 펌프 기종·주소 변경 = 우아한 재시작 1회(부팅이 조립).
적용 실패 = 옛 설정 유지 · 미보고. 봉투는 조립 시점 해시를 싣고, 지금 적용한 해시와 다르면 모션 0 거부.
"""

from __future__ import annotations

from senlyt_pi.adapters.settings_source import pump_addrs_from_settings, pump_model_from_settings
from senlyt_pi.adapters.settings_watcher import SettingsWatcher, settings_hash_from_settings
from senlyt_pi.app.daemon import DaemonDeps, SenlytDaemon
from senlyt_pi.config.server_target import ServerConfig
from senlyt_pi.core.pump_guard import PUMP_PRESETS, SyringeSpec
from senlyt_pi.core.wire_messages import RecipeStep
from senlyt_pi.persistence.idempotency_ledger import InMemoryIdempotencyLedger
from senlyt_pi.pipeline.recipe_resolver import RecipeResolver, RecipeValidationError


def _port(liquid, enabled=True):
    return {"liquid": liquid, "enabled": enabled, "aspirateSpeedHz": None, "dispenseSpeedHz": None}


def _layout(pumps, alcohol_port=1):
    row = {"2": _port("bergamot"), "11": _port("output"), "12": _port("air")}
    row[str(alcohol_port)] = _port("alcohol")
    if alcohol_port != 1:
        row["1"] = _port("vanilla")
    return {str(p): dict(row) for p in pumps}


def _frame(h, *, model="tecan_xcalibur", cap=1.0, pumps=(1, 2, 3, 4), alcohol_port=1,
           contract="sensorium-icad-0.1.0+tecan", tune=None):
    preset = {"pumpPresetId": model, "syringeCapacityMl": cap, **(tune or {})}
    return {
        "settingsHash": h,
        "pumpPreset": preset,
        "hardware": {"contractId": contract, "pumpModel": model, "valvePortCount": 12, "settingsStatus": "confirmed"},
        "pumpPorts": _layout(pumps, alcohol_port),
    }


class _Eng:
    """가짜 엔진 어댑터 — 프로브·재초기화·닫기 기록."""

    MODEL_ID = "tecan_xcalibur"

    def __init__(self, present=(1, 2, 3, 4), reinit_code=0):
        self.preset = PUMP_PRESETS[self.MODEL_ID]
        self.present = set(present)
        self.reinit_code = reinit_code
        self.reinits: list = []
        self.closed = 0

    def probe(self, addr):
        return addr in self.present

    def reinitialize(self, addr, spec):
        self.reinits.append((addr, spec.syringe_capacity_ml))
        return self.reinit_code

    def close(self):
        self.closed += 1


class _Sy(_Eng):
    MODEL_ID = "sy01b"


class _Env:
    mode = "fragrance"

    def __init__(self):
        self.persisted: list = []

    def target_model(self, s):
        return pump_model_from_settings(s)

    def target_addrs(self, s):
        return pump_addrs_from_settings(s)

    def persist(self, s, model):
        self.persisted.append((settings_hash_from_settings(s), model))


def _daemon(boot, engine, *, busy=False, restarts=None):
    stroke = engine.preset.pump_full_stroke
    cap = boot["pumpPreset"]["syringeCapacityMl"]
    rr = RecipeResolver({a: SyringeSpec(pump_full_stroke=stroke, syringe_capacity_ml=cap)
                         for a in pump_addrs_from_settings(boot)})
    watch = SettingsWatcher(ServerConfig(base_url="https://example.test"), "t", "fragrance",
                            boot_settings_hash=boot["settingsHash"], boot_settings=boot)

    class _Sink:
        last = None

        def send_heartbeat(self, hb):
            _Sink.last = hb

        def report_status(self, r):
            pass

    d = SenlytDaemon(DaemonDeps(
        device_id="dev-A",
        command_source=type("S", (), {"commands": lambda s, i: iter(())})(),
        status_sink=_Sink(),
        engine=engine,  # type: ignore[arg-type]
        ledger=InMemoryIdempotencyLedger(),  # type: ignore[arg-type]
        resolver=rr,
        heartbeat_interval_s=0,
        hw_watch_addrs=tuple(pump_addrs_from_settings(boot)),
        settings_watch=watch,
        settings_hot_apply=_Env(),
        on_settings_changed=(lambda: restarts.append(1)) if restarts is not None else None,
        applied_settings_hash=boot["settingsHash"],
        applied_contract_id=boot["hardware"]["contractId"],
    ))
    if busy:
        d._sequencer._busy = True  # noqa: SLF001
    return d, watch, engine, _Sink


def _alcohol_step(addr, in_port, vol=225.0):
    return RecipeStep.from_json({
        "kind": "batchSyringe", "idx": 0, "stage": 0, "pumpAddr": addr, "outPort": 11,
        "dispenseSpeedHz": 3000, "slope": 7,
        "aspirations": [{"flavor": "alcohol", "inPort": in_port, "volume": vol, "aspirateSpeedHz": 2000}],
    })


def _hb(d, sink):
    d._emit_heartbeat()
    return sink.last.to_json()


BOOT = _frame("aaaaaaaa00000001")


def test_layout_change_applies_without_restart_and_reports_new_hash():
    """통 배치만 변경(알코올 포트 1→5) — 적용 전 옛 해시 보고, 적용 후 새 해시 · 새 배치로 해석."""
    eng = _Eng()
    d, watch, _, sink = _daemon(BOOT, eng)
    new = _frame("aaaaaaaa00000002", alcohol_port=5)
    watch.observe(new)
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"  # 적용 전 = 옛 해시(서버 보류)
    d._apply_pending_settings()
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000002"
    assert d._sequencer.resolver.resolve([_alcohol_step(1, 5)]).steps[0].aspirations[0].steps == 675
    try:
        d._sequencer.resolver.resolve([_alcohol_step(1, 1)])
        raise AssertionError("옛 포트(이제 바닐라)는 거부돼야 한다")
    except RecipeValidationError as e:
        assert e.reason == "alcohol_port_conflict"
    assert eng.reinits == []  # 용량 불변 = 재초기화 없음


def test_heysenlyt_layout_change_hot():
    boot = _frame("bbbbbbbb00000001", model="sy01b", cap=0.5, pumps=(1, 2, 3), contract="sensorium-fragrance-1.0.0")
    d, watch, _, sink = _daemon(boot, _Sy(present=(1, 2, 3)))
    watch.observe(_frame("bbbbbbbb00000002", model="sy01b", cap=0.5, pumps=(1, 2, 3),
                         contract="sensorium-fragrance-1.0.0", alcohol_port=5))
    d._apply_pending_settings()
    assert _hb(d, sink)["settingsHash"] == "bbbbbbbb00000002"
    assert d._sequencer.resolver.resolve([_alcohol_step(2, 5, 100.0)]).steps[0].aspirations[0].steps == 2400


def test_capacity_change_reinitializes_then_reports():
    """용량 1→5mL — 펌프마다 새 스펙으로 재초기화(표 3-6 힘) 후 새 해시 · 스텝 환산 새 용량."""
    eng = _Eng()
    d, watch, _, sink = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000003", cap=5.0))
    d._apply_pending_settings()
    assert eng.reinits == [(1, 5.0), (2, 5.0), (3, 5.0), (4, 5.0)]
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000003"
    assert d._sequencer.resolver.resolve([_alcohol_step(1, 1)]).steps[0].aspirations[0].steps == 135
    assert d._dispatcher._capacity_mismatch(5.0) is None  # noqa: SLF001
    assert d._dispatcher._capacity_mismatch(1.0) is not None  # noqa: SLF001


def test_reinit_failure_keeps_old_settings_and_hash():
    eng = _Eng(reinit_code=9)
    d, watch, _, sink = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000004", cap=5.0))
    d._apply_pending_settings()
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"  # 미보고 = 서버 보류 유지
    assert d._sequencer.resolver.resolve([_alcohol_step(1, 1)]).steps[0].aspirations[0].steps == 675  # 옛 1mL


def test_tuning_change_swaps_preset_next_motion():
    eng = _Eng()
    d, watch, _, sink = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000005", tune={"pumpMaxTopSpeedHz": 900}))
    d._apply_pending_settings()
    assert eng.preset.pump_max_top_speed_hz == 900
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000005"


def test_busy_waits_then_applies():
    """제조 중 수신 — 끝날 때까지 적용하지 않는다(진행 작업은 시작 시점 설정 · 보고도 옛 해시)."""
    d, watch, _, sink = _daemon(BOOT, _Eng(), busy=True)
    watch.observe(_frame("aaaaaaaa00000006", cap=5.0))
    d._apply_pending_settings()
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"
    d._sequencer._busy = False  # noqa: SLF001
    d._apply_pending_settings()
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000006"


def test_model_change_requests_restart_not_swap():
    """기종 sy01b → tecan — 어댑터를 옆에서 바꾸지 않고 우아한 재시작 1회(부팅이 조립) · 해시 미보고(서버 보류)."""
    boot = _frame("cccccccc00000001", model="sy01b", cap=0.5)
    restarts: list = []
    old = _Sy()
    d, watch, eng, sink = _daemon(boot, old, restarts=restarts)
    watch.observe(_frame("cccccccc00000002", model="tecan_xcalibur", cap=1.0))
    d._apply_pending_settings()
    d._apply_pending_settings()  # 1회 래치
    assert restarts == [1] and old.closed == 0 and old.reinits == []
    assert _hb(d, sink)["settingsHash"] == "cccccccc00000001"


def test_address_change_3_to_4_requests_restart():
    boot = _frame("dddddddd00000001", pumps=(1, 2, 3))
    restarts: list = []
    d, watch, _, sink = _daemon(boot, _Eng(), restarts=restarts)
    watch.observe(_frame("dddddddd00000002", pumps=(1, 2, 3, 4)))
    d._apply_pending_settings()
    assert restarts == [1]
    assert _hb(d, sink)["settingsHash"] == "dddddddd00000001"


def test_dead_pump_at_boot_does_not_force_restart_on_unrelated_change():
    """부팅 때 3번 펌프 무응답(pump_map 에 없음)이어도 — 선언 주소가 같으면 배치 변경은 재시작 없이 적용(선언 ↔ 선언 비교)."""
    restarts: list = []
    d, watch, _, sink = _daemon(BOOT, _Eng(present=(1, 2, 4)), restarts=restarts)
    d._sequencer.resolver.pump_map.pop(3)  # noqa: SLF001 — 부팅 인식 누락 모사
    watch.observe(_frame("aaaaaaaa00000010", alcohol_port=5))
    d._apply_pending_settings()
    assert restarts == [] and _hb(d, sink)["settingsHash"] == "aaaaaaaa00000010"


def test_applied_settings_persist_to_cache():
    d, watch, _, _ = _daemon(BOOT, _Eng())
    watch.observe(_frame("aaaaaaaa00000007", cap=5.0))
    d._apply_pending_settings()
    assert d.deps.settings_hot_apply.persisted == [("aaaaaaaa00000007", "tecan_xcalibur")]


def test_watcher_pending_latest_frame_wins_and_same_hash_clears():
    w = SettingsWatcher(ServerConfig(base_url="https://example.test"), "t", "fragrance",
                        boot_settings_hash="aaaaaaaa00000001", boot_settings=BOOT)
    f2, f3 = _frame("aaaaaaaa00000002"), _frame("aaaaaaaa00000003")
    w.observe(f2)
    w.observe(f3)
    assert w.pending_settings() is f3
    w.observe(_frame("aaaaaaaa00000001"))
    assert w.pending_settings() is None
    w.observe({"hardware": {}})  # 해시 없는 프레임(구 서버) = 무시
    assert w.pending_settings() is None
