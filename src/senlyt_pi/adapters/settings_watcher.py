"""기기 설정 상시 구독 — 바뀌면 **유휴일 때 재시작 없이 적용** (2026-09-29 기기 설정 한 벌 · 2026-09-30 무재시작 단일 규칙).

pi 는 부팅 때 설정 스냅샷(`/api/dispenser/settings` SSE)으로 pump_map·어댑터·알코올 포트 표를 조립한다. 이 감시자는 스트림을
**끊지 않고** 계속 읽는다(끊기면 지수 백오프로 재연결).

단일 규칙(04_erd §9-3): 어떤 기기 설정이 바뀌든(물리 설정·AI 계약·통 배치·펌프 기종·펌프 주소) 서버 `settingsHash` 가 바뀐다 →
서버는 pi 가 새 해시를 하트비트로 보고할 때까지 제조를 보류한다(`device_settings_applying`) → pi 는 유휴일 때 적용한 뒤 새 해시를
보고한다(`SenlytDaemon._apply_pending_settings`). 통 배치·용량(펌프 재초기화)·튠·포트 상한·계약은 **재시작 없이** 적용한다.
펌프 기종·펌프 주소 집합이 바뀌면(선언 비교) 어댑터를 새로 조립해야 해 **우아한 재시작 1회** — 재시작한 부팅이 새 스냅샷으로
조립하고 새 해시를 보고한다. 적용에 실패하면 보고하지 않는다(보류 유지 · 다음 주기에 재시도).

⚠️ pi 는 해시를 **계산하지 않는다** — 서버가 준 값을 비교하고 하트비트로 되돌려 보낼 뿐이다(언어 간 정규화 parity 불필요).
⚠️ 해시가 없는 프레임(구 서버)은 비교하지 않는다.
⚠️ 부팅 때 스냅샷이 없었으면(오프라인 부팅 · 캐시 폴백) 첫 해시 프레임이 곧 "적용 대기"다.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from ..config.server_target import ServerConfig
from .http_client import SseStream, bearer_headers, open_sse

OpenStream = Callable[..., SseStream]

_HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")

# 재연결 백오프(초) — 서버 재배포·네트워크 순단을 흡수. 상한 60s(설정 반영 지연의 최댓값).
_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 60.0
# 읽기 타임아웃 — 서버 SSE heartbeat(15s) 주석이 계속 오므로 넉넉히. 이보다 오래 무소식이면 재연결.
_READ_TIMEOUT_S = 90.0
_CONNECT_TIMEOUT_S = 10.0


def settings_hash_from_settings(settings: Any) -> "str | None":
    """스냅샷 `settingsHash`(서버 계산) → 형식 검증된 값. 부재/불량 = None(비교 안 함)."""
    if not isinstance(settings, Mapping):
        return None
    h = settings.get("settingsHash")
    return h if isinstance(h, str) and _HASH_RE.match(h) else None


def contract_id_from_settings(settings: Any) -> "str | None":
    """스냅샷 `hardware.contractId`(없으면 `sensoriumVersion`) — 하트비트 `appliedContractId` 로 되돌려 보낸다."""
    if not isinstance(settings, Mapping):
        return None
    hw = settings.get("hardware")
    if not isinstance(hw, Mapping):
        return None
    v = hw.get("contractId") or hw.get("sensoriumVersion")
    return str(v) if isinstance(v, str) and 0 < len(v) <= 80 else None


def settings_summary(settings: Any) -> "dict[str, Any]":
    """핵심 값 요약 — 설정 변경 로그의 전→후 비교용(2026-09-30).

    용량(mL) · 유효 튠(v·V·c·L) · 펌프 주소 목록 · AI 계약 · 기종. 서버가 해시에 넣는 축과 같은 값만 본다(향료 이름 없음).
    """
    if not isinstance(settings, Mapping):
        return {}
    pp = settings.get("pumpPreset")
    pp = pp if isinstance(pp, Mapping) else {}
    ports = settings.get("pumpPorts")
    addrs = (
        sorted(int(k) for k in ports.keys() if str(k).isdigit())
        if isinstance(ports, Mapping)
        else []
    )
    tune = None
    if all(isinstance(pp.get(k), (int, float)) for k in ("pumpMaxStartSpeedHz", "pumpMaxTopSpeedHz", "pumpMaxCutoffSpeedHz", "pumpMaxSlope")):
        tune = (
            f"v{pp['pumpMaxStartSpeedHz']}V{pp['pumpMaxTopSpeedHz']}"
            f"c{pp['pumpMaxCutoffSpeedHz']}L{pp['pumpMaxSlope']}"
        )
    cap = pp.get("syringeCapacityMl")
    return {
        "syringeCapacityMl": cap if isinstance(cap, (int, float)) and not isinstance(cap, bool) else None,
        "pumpTuning": tune,
        "pumpAddrs": addrs,
        "contractId": contract_id_from_settings(settings),
        "pumpModel": pp.get("pumpPresetId") if isinstance(pp.get("pumpPresetId"), str) else None,
    }


def summarize_settings_change(before: Any, after: Any) -> "list[dict[str, Any]]":
    """두 스냅샷의 핵심 값 비교 — 바뀐 것만 `[{field, from, to}]`."""
    a = settings_summary(before)
    b = settings_summary(after)
    return [{"field": k, "from": a.get(k), "to": b.get(k)} for k in b if a.get(k) != b.get(k)]


class SettingsWatcher:
    """설정 스트림 상시 구독(백그라운드 스레드) — 부팅 해시와 다른 해시가 오면 사유를 기록한다."""

    def __init__(
        self,
        server_config: ServerConfig,
        bearer_token: str,
        mode: str,
        *,
        boot_settings_hash: "str | None",
        # (2026-09-30) 부팅 스냅샷 원본 — 변경 로그에 "무엇이 전→후로 바뀌었나"(용량·튠·펌프 주소·계약)를 싣는다. 없으면 해시만.
        boot_settings: Any = None,
        logger: Any = None,
        open_stream: OpenStream = open_sse,
        sleep: "Callable[[float], bool] | None" = None,
    ) -> None:
        self._url = f"{server_config.settings_stream_url}?{urlencode({'mode': mode})}"
        self._token = bearer_token
        self._boot_hash = boot_settings_hash
        self._boot_settings = boot_settings
        # 마지막 변경 사유의 전→후 요약(로그 필드 · 테스트 관측).
        self.last_changes: "list[dict[str, Any]]" = []
        self._log = logger
        self._open = open_stream
        self._stop = threading.Event()
        # sleep seam — stop 시 즉시 깨어난다(Event.wait). 테스트는 가짜로 대체.
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._lock = threading.Lock()
        self._latest_hash: "str | None" = None
        self._latest_settings: Any = None
        # (2026-09-30 · 04_erd §9-3 단일 규칙) 적용 대기 중인 최신 프레임 · 적용한 스냅샷(해시·원본).
        self._pending: Any = None
        self._applied_hash: "str | None" = boot_settings_hash
        self._applied_settings: Any = boot_settings
        self._thread: "threading.Thread | None" = None

    # ── 관측 ─────────────────────────────────────────────────────────────────
    @property
    def boot_settings_hash(self) -> "str | None":
        return self._boot_hash

    def observe(self, settings: Any) -> None:
        """프레임 1장 판정 — 스레드 루프와 테스트가 같이 쓴다.

        (2026-09-30 · 04_erd §9-3 단일 규칙) 설정 해시가 **적용한 값**과 다르면 적용 대기(`pending_settings`)로 둔다 — 재시작 없음.
        데몬이 유휴(소비 루프 사이)에 적용하고 `mark_applied` 로 새 해시를 보고할 때까지 서버는 제조를 보류한다. 최신 프레임이 이긴다.
        """
        h = settings_hash_from_settings(settings)
        if h is None:
            return  # 구 서버 프레임 — 비교하지 않는다.
        if not isinstance(settings.get("hardware"), Mapping):
            # 폴백 프레임(하드웨어 병합 없음 — 서버 조회 실패 등) — 기본값이라 적용·캐시하지 않는다(서버도 해시를 빼지만 이중 방어).
            return
        with self._lock:
            self._latest_hash = h
            self._latest_settings = settings
            if h == self._applied_hash:
                self._pending = None
                return
            first = self._pending is None or settings_hash_from_settings(self._pending) != h
            self._pending = settings
            changes = summarize_settings_change(self._applied_settings, settings)
            self.last_changes = changes
            applied = self._applied_hash
        if first and self._log is not None:
            try:
                self._log.info(
                    "기기 설정 변경 수신 — 유휴일 때 재시작 없이 적용합니다(적용 전까지 서버가 제조를 보류)",
                    stage="pi.received",
                    appliedHash=applied,
                    newHash=h,
                    changes=changes,
                )
            except Exception:  # noqa: BLE001 — 로그 실패가 감시를 죽이면 안 된다.
                pass

    def pending_settings(self) -> Any:
        """적용을 기다리는 최신 프레임(없으면 None)."""
        with self._lock:
            return self._pending

    def mark_applied(self, settings: Any) -> None:
        """핫 적용 완료 — 이 프레임의 해시를 '적용한 해시'로 보고한다(서버 보류 해제). 그 사이 더 새 프레임이 왔으면 대기 유지."""
        h = settings_hash_from_settings(settings)
        with self._lock:
            self._applied_hash = h
            self._applied_settings = settings
            if self._pending is settings or settings_hash_from_settings(self._pending) == h:
                self._pending = None
            # 적용하는 동안 다른 해시(예: A→B 적용 중 A 로 되돌림)가 마지막으로 왔으면 그 프레임을 다시 대기로 — 서버는 값이
            #   바뀔 때만 프레임을 보내므로 여기서 놓치면 보류가 풀리지 않는다(A→B→A).
            if (
                self._pending is None
                and self._latest_hash is not None
                and self._latest_hash != h
                and self._latest_settings is not None
            ):
                self._pending = self._latest_settings

    def applied_settings_hash(self) -> "str | None":
        """하트비트 `settingsHash` — 부팅 스냅샷 또는 마지막으로 **핫 적용한** 스냅샷의 해시(서버 계산값 그대로)."""
        with self._lock:
            return self._applied_hash

    def applied_settings(self) -> Any:
        with self._lock:
            return self._applied_settings

    # ── 수명 ─────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="settings-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """연결 1회 — 스트림이 끝나면 반환(True = 정상 프레임을 하나라도 읽음). 예외는 호출자가 받는다."""
        stream = self._open(
            self._url,
            headers=bearer_headers(self._token),
            timeout=_READ_TIMEOUT_S,
            connect_timeout=_CONNECT_TIMEOUT_S,
        )
        got = False
        try:
            for event, data in stream.events():
                if self._stop.is_set():
                    break
                if event != "settings":
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                inner = parsed.get("settings") if isinstance(parsed, Mapping) else None
                if isinstance(inner, Mapping):
                    got = True
                    self.observe(inner)
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        return got

    def _loop(self) -> None:
        delay = _BACKOFF_START_S
        while not self._stop.is_set():
            try:
                if self.run_once():
                    delay = _BACKOFF_START_S  # 한 번이라도 읽었으면 백오프 초기화.
            except Exception:  # noqa: BLE001 — 연결 실패·read 타임아웃 = 재연결.
                pass
            if self._sleep(delay):
                return
            delay = min(delay * 2, _BACKOFF_MAX_S)
