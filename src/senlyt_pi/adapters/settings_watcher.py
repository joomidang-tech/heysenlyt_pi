"""기기 설정 상시 구독 — 바뀌면 **유휴일 때 우아한 재시작** (2026-09-29 · 기기 설정 한 벌).

pi 는 설정 스냅샷(`/api/dispenser/settings` SSE)을 **부팅 때 한 번** 소비해 pump_map(시린지 용량·스트로크)·어댑터 튠·
프로브 주소를 조립한다(settings_source.py 헤더 — 실시간 스왑 없음). 종전엔 부팅 뒤 운영자가 관제에서 AI 계약·시린지 용량·
튠·통 배치 펌프 키를 바꿔도 **재시작할 때까지** 옛 값으로 돌았다(향연 전환 뒤 4번 펌프 미프로브 → 주문 전량 거부 ·
용량 변경 뒤 봉투 용량 ≠ 스냅샷 → 제조·세척 거부).

이 감시자는 스트림을 **끊지 않고** 계속 읽는다(끊기면 지수 백오프로 재연결). 서버가 각 프레임에 싣는 `settingsHash`
(재시작 관련 축 — 계약·기종·펌프 주소·용량·유효 튠·포트 상한의 해시 · 서버 `deviceProfile.settingsHashOf`)가 **부팅 때 받은
값과 달라지면** `changed_reason()` 이 사유를 돌려준다. 재시작 **결정**은 데몬(유휴 판정 — 제조·세척 중엔 기다린다)과
senlytd(우아한 종료 → systemd 재기동) 몫이다. 여기는 관측만 한다.

⚠️ pi 는 해시를 **계산하지 않는다** — 서버가 준 값을 비교하고 하트비트로 되돌려 보낼 뿐이다(언어 간 정규화 parity 불필요).
⚠️ 해시가 없는 프레임(구 서버)은 비교하지 않는다 — 구 서버와 새 pi 조합이 재시작 루프를 만들지 않게.
⚠️ 부팅 때 스냅샷이 없었으면(오프라인 부팅 · 캐시 폴백) 첫 해시 프레임이 곧 "바뀜"이다 — 서버 설정으로 재조립하는 게 맞다.
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


class SettingsWatcher:
    """설정 스트림 상시 구독(백그라운드 스레드) — 부팅 해시와 다른 해시가 오면 사유를 기록한다."""

    def __init__(
        self,
        server_config: ServerConfig,
        bearer_token: str,
        mode: str,
        *,
        boot_settings_hash: "str | None",
        logger: Any = None,
        open_stream: OpenStream = open_sse,
        sleep: "Callable[[float], bool] | None" = None,
    ) -> None:
        self._url = f"{server_config.settings_stream_url}?{urlencode({'mode': mode})}"
        self._token = bearer_token
        self._boot_hash = boot_settings_hash
        self._log = logger
        self._open = open_stream
        self._stop = threading.Event()
        # sleep seam — stop 시 즉시 깨어난다(Event.wait). 테스트는 가짜로 대체.
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._lock = threading.Lock()
        self._reason: "str | None" = None
        self._latest_hash: "str | None" = None
        self._thread: "threading.Thread | None" = None

    # ── 관측 ─────────────────────────────────────────────────────────────────
    @property
    def boot_settings_hash(self) -> "str | None":
        return self._boot_hash

    def changed_reason(self) -> "str | None":
        """부팅 스냅샷과 다른 설정이 도착했으면 사유(한 줄) · 아니면 None."""
        with self._lock:
            return self._reason

    def observe(self, settings: Any) -> None:
        """프레임 1장 판정 — 스레드 루프와 테스트가 같이 쓴다."""
        h = settings_hash_from_settings(settings)
        if h is None:
            return  # 구 서버 프레임 — 비교하지 않는다(재시작 루프 방지).
        with self._lock:
            self._latest_hash = h
            if self._reason is not None:
                return
            if self._boot_hash is None:
                self._reason = f"부팅 때 서버 설정이 없었음 → 서버 설정 수신(hash={h})"
            elif h != self._boot_hash:
                cid = contract_id_from_settings(settings)
                self._reason = (
                    f"기기 설정 변경(hash {self._boot_hash} → {h}"
                    + (f" · AI 계약 {cid}" if cid else "")
                    + ")"
                )
            else:
                return
        if self._log is not None:
            try:
                self._log.warn(
                    f"{self._reason} — 유휴가 되면 우아하게 재시작해 반영합니다(제조·세척 중엔 기다림)",
                    stage="pi.received",
                )
            except Exception:  # noqa: BLE001 — 로그 실패가 감시를 죽이면 안 된다.
                pass

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
