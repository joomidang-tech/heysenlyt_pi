"""설정 적용 일관성 수정(2026-09-30 · 04_erd §9-3) — 가짜 시리얼로 실제 어댑터를 돌려 잠근다.

- 용량 핫 적용 = 실제 Tecan 어댑터가 펌프마다 재초기화(N0R → Z) 명령을 보낸 뒤 새 해시 보고
- A→B→A: B 적용 중 A 로 되돌아가도 보류가 풀린다(마지막 프레임 재대기)
- 폴백 프레임(하드웨어 병합 없음)은 해시가 있어도 적용·캐시하지 않는다
- 봉투 조립 시점 해시 ≠ 적용 해시 → 모션 0 거부(`settings_changed_since_dispatch`)
- 목록 밖 용량(capacity_block)이면 재초기화(Z)도 보내지 않는다 · 부분 재초기화 실패 = 셋업 캐시 무효화
- 부팅 때 설정이 없던 pi = 하트비트 `settingsApplied:false`(서버 보류) · 첫 설정은 재시작으로 조립
"""

from __future__ import annotations

from test_settings_hot_apply_20260930 import BOOT, _daemon, _Eng, _frame, _hb
from test_tecan_xcalibur_engine_adapter import FakeSerial, adapter_with

from senlyt_pi.adapters.settings_watcher import SettingsWatcher
from senlyt_pi.config.server_target import ServerConfig


def test_capacity_hot_apply_sends_reinit_frames_on_real_tecan_adapter():
    fake = FakeSerial()
    eng = adapter_with(fake)
    d, watch, _, sink = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000020", cap=5.0))
    d._apply_pending_settings()
    for a in (1, 2, 3, 4):
        assert f"/{a}N0R\r" in fake.written and f"/{a}ZR\r" in fake.written  # 1·5mL = Full 힘(표 3-6)
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000020"


def test_revert_during_apply_repends_last_frame():
    w = SettingsWatcher(ServerConfig(base_url="https://example.test"), "t", "fragrance",
                        boot_settings_hash="aaaaaaaa00000001", boot_settings=BOOT)
    b = _frame("aaaaaaaa00000002", cap=5.0)
    w.observe(b)
    assert w.pending_settings() is b
    w.observe(_frame("aaaaaaaa00000001"))  # B 적용 중 A 로 되돌림 — 적용한 값(A)과 같아 대기 해제
    assert w.pending_settings() is None
    w.mark_applied(b)  # B 적용 완료 → 마지막 수신(A)이 다르므로 A 를 다시 대기로
    assert w.pending_settings()["settingsHash"] == "aaaaaaaa00000001"


def test_fallback_frame_without_hardware_is_ignored():
    w = SettingsWatcher(ServerConfig(base_url="https://example.test"), "t", "fragrance",
                        boot_settings_hash="aaaaaaaa00000001", boot_settings=BOOT)
    w.observe({"settingsHash": "aaaaaaaa000000ff", "pumpPreset": {}, "pumpPorts": {}})
    assert w.pending_settings() is None


def test_envelope_hash_mismatch_rejected_before_motion():
    d, watch, _, _ = _daemon(BOOT, _Eng())
    disp = d._dispatcher  # noqa: SLF001
    assert disp._settings_mismatch("aaaaaaaa00000001") is None  # noqa: SLF001 — 같은 해시
    assert disp._settings_mismatch(None) is None  # noqa: SLF001 — 구 서버 봉투(무검사)
    why = disp._settings_mismatch("aaaaaaaa00000099")  # noqa: SLF001
    assert why is not None and why.startswith("[settings_changed_since_dispatch]")
    assert disp._cap_detail["reason"] == "settings_changed_since_dispatch"  # noqa: SLF001


def test_capacity_block_skips_reinit():
    eng = _Eng()
    d, watch, _, _ = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000021", cap=0.5))  # Tecan 목록 밖
    d._apply_pending_settings()
    assert eng.reinits == []


def test_partial_reinit_failure_invalidates_setup_cache():
    class _Partial(_Eng):
        cleared = 0

        def reinitialize(self, addr, spec):
            self.reinits.append((addr, spec.syringe_capacity_ml))
            return 9 if addr == 2 else 0

        def initialize(self):
            _Partial.cleared += 1

    eng = _Partial()
    d, watch, _, sink = _daemon(BOOT, eng)
    watch.observe(_frame("aaaaaaaa00000022", cap=5.0))
    d._apply_pending_settings()
    assert eng.reinits == [(1, 5.0), (2, 5.0)] and _Partial.cleared == 1
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"


def test_no_boot_settings_reports_not_applied_and_restarts_on_first_frame():
    restarts: list = []
    d, watch, _, sink = _daemon(BOOT, _Eng(), restarts=restarts)
    watch._applied_hash = None  # noqa: SLF001 — 오프라인 부팅(스냅샷 없음) 모사
    watch._applied_settings = None  # noqa: SLF001
    hb = _hb(d, sink)
    assert hb.get("settingsApplied") is False and "settingsHash" not in hb
    watch.observe(_frame("aaaaaaaa00000023"))
    d._apply_pending_settings()
    assert restarts == [1]


def test_reinit_failure_retry_is_capped_then_resumes_on_new_hash_or_maintenance_done():
    """같은 해시 재초기화 실패는 HOT_APPLY_MAX_ATTEMPTS 회까지만 — 파괴적 명령(Z) 무한 반복 금지(최종 검증 P2).

    새 해시(설정 재저장)나 운영자 정비 성공이면 상한을 풀어 다시 시도한다. 보류는 유지(해시 미보고).
    """
    from senlyt_pi.core.command_set import MAINTENANCE_COMMAND_SET_PREFIX
    from senlyt_pi.core.order_status import DispensePhase

    class _Fail(_Eng):
        def reinitialize(self, addr, spec):
            self.reinits.append((addr, spec.syringe_capacity_ml))
            return 9

    eng = _Fail()
    d, watch, _, sink = _daemon(BOOT, eng)
    d.HOT_APPLY_RETRY_S = 0.0  # 재시도 간격 없이 상한만 본다
    watch.observe(_frame("aaaaaaaa00000024", cap=5.0))
    for _ in range(6):
        d._apply_pending_settings()
    assert len(eng.reinits) == d.HOT_APPLY_MAX_ATTEMPTS  # 펌프 1 에서 실패 — 시도당 1회
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"  # 보류 유지
    # 운영자 정비 성공 → 상한 해제 → 한 번 더 시도
    d._publish_progress(DispensePhase.COMPLETED, 1, 1, None, f"{MAINTENANCE_COMMAND_SET_PREFIX}x", "t")
    d._apply_pending_settings()
    assert len(eng.reinits) == d.HOT_APPLY_MAX_ATTEMPTS + 1
    # 새 해시(설정 재저장) → 카운트 초기화
    watch.observe(_frame("aaaaaaaa00000025", cap=5.0))
    d._apply_pending_settings()
    assert len(eng.reinits) == d.HOT_APPLY_MAX_ATTEMPTS + 2


def test_capped_reinit_resumes_automatically_when_pump_status_recovers_real_tecan_bus():
    """상한(3회) 뒤 Z 송신 중단 → 비파괴 상태 조회(`?`)는 계속 → 펌프가 무응답 → 정상으로 바뀌면 자동 재초기화 1회 → 해시 보고.

    가짜 시리얼(실제 Tecan 어댑터) 기반. 연결 회복 감지 = 맹목 반복이 아니라 상태 전환이 트리거(최종 검증 P2 보강).
    """
    from test_tecan_xcalibur_engine_adapter import status_frame

    class _Bus(FakeSerial):
        def __init__(self):
            super().__init__()
            self.silent: set[int] = set()
            self.z_fail = True

        def write(self, data: bytes) -> int:
            text = data.decode("ascii")
            self.written.append(text)
            addr = int(text[1]) if len(text) > 1 and text[1].isdigit() else 0
            if addr in self.silent:
                return len(data)  # 무응답
            if self.z_fail and "Z" in text:
                self._buf.extend(status_frame(1, ready=True))  # 초기화 오류(err1)
            else:
                self._buf.extend(self._default)
            return len(data)

    bus = _Bus()
    eng = adapter_with(bus)
    d, watch, _, sink = _daemon(BOOT, eng)
    d.HOT_APPLY_RETRY_S = 0.0
    watch.observe(_frame("aaaaaaaa00000026", cap=5.0))
    for _ in range(5):
        d._apply_pending_settings()
    z_frames = lambda: sum(1 for w in bus.written if w.startswith("/1") and "Z" in w)  # noqa: E731
    assert z_frames() == d.HOT_APPLY_MAX_ATTEMPTS  # 상한 뒤 Z 송신 중단
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000001"
    # 펌프 1 무응답 관측(비파괴 `?`) — 상한과 무관하게 조회는 돈다
    bus.silent = {1}
    d._refresh_hw_health()
    assert d._pump_health.get(1) == "silent"
    d._apply_pending_settings()
    assert z_frames() == d.HOT_APPLY_MAX_ATTEMPTS  # 회복 전엔 여전히 Z 없음
    # 연결 회복 + 펌프 정상화 → 다음 조회에서 ok 전환 → 자동 재초기화 1회 → 성공 → 새 해시 보고
    bus.silent = set()
    bus.z_fail = False
    d._refresh_hw_health()
    d._apply_pending_settings()
    assert z_frames() == d.HOT_APPLY_MAX_ATTEMPTS + 1
    assert _hb(d, sink)["settingsHash"] == "aaaaaaaa00000026"
