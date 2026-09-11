"""senlytd — hey_senlyt v1.2.0 라즈베리파이 headless 디스펜서 데몬 진입점.

실행: `senlytd` (pyproject [project.scripts]) 또는 `python -m senlyt_pi.app.senlytd`.

동작(스텁 제거·사용자 원칙 2026-07-10):
  - `SENLYT_RUN=1`(02_infra §10 E2E 컨테이너 스위치): 실 어댑터 조립(등록·SSE·역보고 실 HTTP) +
    crash-safe 파일 멱등 ledger + RR pump_map 을 결선해 **SenlytDaemon.boot() 상시 소비 루프**를
    실행한다(엔진=Fake 라 안전). SIGTERM/SIGINT → 우아한 종료(shutdown). 루프는 stop 까지 블록.
  - `SENLYT_SELFTEST=1`: 실 어댑터를 조립만 하고 결선 성공을 로그로 알린 뒤 종료(0). 펌프 미구동.
  - 기본(무설정): 상시 소비 루프는 `SENLYT_RUN=1` 에서 실행됨을 한글 로그로 알리고 안전 종료(0).

⛔ 유일 mock = 물리 엔진(FakeEngineAdapter). 소비 루프 자체는 실구현(SENLYT_RUN 이 켜는 스위치).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Mapping

from ..obs.log import STAGE_ERROR, STAGE_PI_RECEIVED, StructuredLogger
from .bootstrap import (
    BootstrapError,
    build_components,
    build_ledger,
    build_resolver,
)
from .daemon import DaemonDeps, SenlytDaemon

# self-test(실 어댑터 조립·실 HTTP 등록) 트리거 env — 조립만·펌프 미구동.
SENLYT_SELFTEST_ENV = "SENLYT_SELFTEST"
# 상시 소비 루프 실행 트리거 env — E2E 컨테이너가 설정(§10). boot() 상시 루프 기동.
SENLYT_RUN_ENV = "SENLYT_RUN"
# 폴링 간격(ms) — 기본 1000ms(1s). SenlytDaemon.poll_interval_s 로 환산.
SENLYT_POLL_INTERVAL_MS_ENV = "SENLYT_POLL_INTERVAL_MS"
# heartbeat 주기(ms) — 기본 10000ms(10s). 서버 online 표시 창(30s=3주기)과 정합 — 값을 바꾸면
#   서버 표시 창도 함께 조정할 것.
SENLYT_HEARTBEAT_INTERVAL_MS_ENV = "SENLYT_HEARTBEAT_INTERVAL_MS"

_TRUTHY = ("1", "true", "TRUE")


def _is_truthy(v: str | None) -> bool:
    return (v or "").strip() in _TRUTHY


def _resolve_poll_interval_s(environ: Mapping[str, str]) -> float:
    """SENLYT_POLL_INTERVAL_MS(기본 1000) → 초. 파싱 실패·비양수는 1.0s 로 안전 폴백."""
    raw = environ.get(SENLYT_POLL_INTERVAL_MS_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        ms = float(raw)
    except ValueError:
        return 1.0
    return ms / 1000.0 if ms > 0 else 1.0


def _resolve_heartbeat_interval_s(environ: Mapping[str, str]) -> float:
    """SENLYT_HEARTBEAT_INTERVAL_MS(기본 10000) → 초. 파싱 실패·비양수는 10.0s 로 안전 폴백.

    파싱 성공한 양수 값은 [1.0s, 30.0s] 클램프 —
      상한 30s = 서버 online 표시 창(30s=3주기) 붕괴 방지(주기 > 창이면 항상 offline 표시).
      하한 1s  = 서버/Firestore 쓰기 폭주 방지.
    """
    raw = environ.get(SENLYT_HEARTBEAT_INTERVAL_MS_ENV, "").strip()
    if not raw:
        return 10.0
    try:
        ms = float(raw)
    except ValueError:
        return 10.0
    if ms <= 0:
        return 10.0
    return min(max(ms / 1000.0, 1.0), 30.0)


def _resolve_trace_flush_s(environ: Mapping[str, str]) -> float:
    """SENLYT_TRACE_FLUSH_INTERVAL_MS(기본 10000) → 초. DEBUG/INFO 배치 주기(RC8 노브)."""
    raw = environ.get("SENLYT_TRACE_FLUSH_INTERVAL_MS", "").strip()
    if not raw:
        return 10.0
    try:
        ms = float(raw)
    except ValueError:
        return 10.0
    return ms / 1000.0 if ms > 0 else 10.0


def _resolve_ship_log_min_severity(environ: Mapping[str, str]) -> str:
    """SENLYT_SHIP_LOG_MIN_SEVERITY(기본 DEBUG=전 레벨 전송) — 이 값 이상 severity 만 서버 합류(RC8 노브).

    DEBUG 폭주를 운영 중 INFO/WARN 으로 **재배포 없이** 낮추는 안전밸브. 유효값 아니면 DEBUG 폴백.
    """
    raw = environ.get("SENLYT_SHIP_LOG_MIN_SEVERITY", "").strip().upper()
    return raw if raw in ("DEBUG", "INFO", "WARN", "ERROR") else "DEBUG"


def _selftest(environ: Mapping[str, str], logger: StructuredLogger) -> int:
    """실 어댑터 조립 self-test — ServerConfig 결정 + 실 등록 + 어댑터 결선(펌프 미구동)."""
    from ..config.server_target import ServerTargetError

    try:
        components = build_components(environ, logger=logger)
    except ServerTargetError as e:
        logger.error(
            "서버 타겟 미설정/미지원 — 부팅 중단(fail-fast)",
            stage=STAGE_ERROR,
            error=str(e),
        )
        return 1
    except BootstrapError as e:
        logger.error("실 어댑터 조립 실패 — 부팅 중단", stage=STAGE_ERROR, error=str(e))
        return 1

    logger.info(
        "실 어댑터 결선 성공(등록·SSE 구독·역보고 실 HTTP) — 소비 루프는 SENLYT_RUN=1 에서 실행",
        stage=STAGE_PI_RECEIVED,
        device_id=components.device_id,
        baseUrl=components.server_config.base_url,
        engine=type(components.engine).__name__,
    )
    return 0


def _should_arm_pump_rediscovery(pump_map: Mapping[int, object], engine: object, watch_addrs: "tuple[int, ...]") -> bool:
    """재발견 정책 주입 여부(R8.5 P2) — 실 probe 어댑터 + 기대 주소를 **전부** 매핑하지 못했을 때만.

    fake(probe 부재)=미주입 / env 명시·스캔 완전 성공=미주입 / 공집합·부분 인식=주입.
    Undeclared 는 probe 가 있어 주입되지만 health_probe 부재로 데몬이 발화하지 못한다
    (그쪽 복구는 자기 재fetch 경로 소관 — 이중 SIGTERM 없음)."""
    if not callable(getattr(engine, "probe", None)):
        return False
    return not set(watch_addrs) <= set(pump_map)


def _make_pump_rediscovery_restart(logger: StructuredLogger, device_id: str) -> "Callable[[], None]":
    """펌프 재발견 정책(R8 P1-1) — 펌프가 살아 응답하는데 부팅 스캔이 놓친 상태를 재기동으로 복구.

    재기동이 boot 스캔을 다시 돌려 resolver 를 재조립한다(핫스왑 재구성 백로그 불요).
    pump_map 이 빈 상태에서만 불리므로(제조 원천 불가) 진행 중 작업 경합이 없다."""

    def _restart() -> None:
        logger.warn(
            "펌프 응답 감지 + 부팅 인식 부재 — 정상 종료 후 재기동으로 버스를 재스캔합니다",
            stage=STAGE_PI_RECEIVED,
            device_id=device_id,
        )
        os.kill(os.getpid(), signal.SIGTERM)  # 우아한 종료 → systemd Restart=always 재기동.

    return _restart


def _install_signal_handlers(daemon: SenlytDaemon, logger: StructuredLogger) -> None:
    """SIGTERM/SIGINT → 우아한 종료 요청(stop 플래그). 비메인스레드/미지원 플랫폼은 무시."""
    import signal

    def _handler(signum: int, _frame: object) -> None:
        logger.info(
            "종료 시그널 수신 — 우아한 종료 요청",
            stage=STAGE_ERROR,
            device_id=daemon.deps.device_id,
            signal=signum,
        )
        daemon.request_stop()

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # 비메인스레드/미지원 — 무시(테스트/컨테이너 재시작 정책이 보완).


def _run(environ: Mapping[str, str], logger: StructuredLogger) -> int:
    """상시 소비 루프 실행 — 실 어댑터 + 파일 ledger + RR 결선 → SenlytDaemon.boot()."""
    from ..config.server_target import ServerTargetError

    try:
        ledger = build_ledger(environ)
        # 긴급정지 공유 래치(§9-4) — **어댑터·시퀀서·데몬이 같은 Event** 를 보게 여기서 한 번 만들어
        #   build_components(→build_engine→어댑터)·DaemonDeps 양쪽에 주입한다(설계 '단일 공유 _estop').
        estop_event = threading.Event()
        # fetch_settings=True — 부팅 1회 서버 settings 스냅샷으로 시린지 용량 SoT 반영(O-18·안전 급소).
        #   best-effort(실패=모드 기본 폴백). 조립 self-test(_selftest)는 fetch 안 함(펌프 미구동).
        components = build_components(
            environ, ledger=ledger, logger=logger, fetch_settings=True,
            estop_event=estop_event,
        )
    except ServerTargetError as e:
        logger.error(
            "서버 타겟 미설정/미지원 — 부팅 중단(fail-fast)",
            stage=STAGE_ERROR,
            error=str(e),
        )
        return 1
    except BootstrapError as e:
        logger.error("실 어댑터 조립 실패 — 부팅 중단", stage=STAGE_ERROR, error=str(e))
        return 1

    # 엔진을 넘겨 pump_map **자동인식**을 가능하게 한다(PUMP_ADDRESSES 미설정 = "URL만" 설치).
    #   env 가 있으면 그게 이기고, 없으면 어댑터의 probe 로 버스를 스캔한다.
    #   server_settings(부팅 스냅샷)로 시린지 용량/스트로크를 서버 SoT 값으로 얹는다(O-18).
    # 주기 HW 감시·재발견 정책의 기대 주소(모드 파생 — flavor=2펌프[1,2]·그 외=3펌프[1,2,3]).
    watch_addrs: "tuple[int, ...]" = (
        (1, 2) if getattr(components, "mode", None) == "flavor" else (1, 2, 3)
    )
    resolver = build_resolver(
        environ,
        engine=components.engine,
        server_settings=getattr(components, "server_settings", None),
        # 서버배정 mode 우선(env 폴백) — 'URL만' 설치 식향 기기가 예상주소[1,2]만 프로브(부팅지연 0).
        mode=getattr(components, "mode", None),
        # 하드웨어 선언(스냅샷>캐시) — 캐시 부팅의 stroke·포트 상한 공급원(2026-09-02 단일 SoT).
        hardware_profile=getattr(components, "hardware_profile", None),
    )
    deps = DaemonDeps(
        device_id=components.device_id,
        command_source=components.command_source,
        status_sink=components.status_sink,
        engine=components.engine,
        valve=components.valve,
        ledger=ledger,
        resolver=resolver,
        # 용량 축 가드 활성(R4 P0-1·R4.5 P2-A) — 출처 판정은 용량을 파생한 build_resolver 가
        #   각인한 값을 그대로 쓴다(재계산 금지 — 두 곳 계산이 어긋나면 P0-1 이 부활한다).
        capacity_from_settings=resolver.capacity_from_settings,
        commandset_source=components.command_source,  # 동일 SSE 어댑터가 두 축 제공.
        # 주기 HW 감시 기대 주소(실시간 판단·2026-07-19) — 부팅 인식이 비어도 이 주소들을 계속
        #   프로브해 pumpHealth 로 보고(어댑터 미장착 = silent 빨강, USB 꽂히면 ok 초록 자동 전환).
        hw_watch_addrs=watch_addrs,
        logger=components.logger,
        poll_interval_s=_resolve_poll_interval_s(environ),
        heartbeat_interval_s=_resolve_heartbeat_interval_s(environ),
        # 관측 로그 볼륨 노브(RC8) — 재배포 없이 env 로 조절. 기본 = DEBUG 전량·10s 배치.
        ship_log_min_severity=_resolve_ship_log_min_severity(environ),
        trace_flush_interval_s=_resolve_trace_flush_s(environ),
        # 긴급정지 fast-poll 소스(§9-4) — HTTP status_sink 의 estop GET 을 device_id 로 바인딩한다.
        #   제조 중에도 즉시 선점하려면 명령 폴과 무관한 이 별도 축이 필요하다.
        estop_source=lambda: components.status_sink.poll_estop(components.device_id),
        # 어댑터에 주입한 것과 **같은 공유 래치** — 데몬·시퀀서·어댑터가 하나의 estop 이벤트를 본다.
        estop_event=estop_event,
        # 펌프 재발견 정책(R8 P1-1) — PUMP_ADDRESSES 각인 제거로 부팅 1회 스캔이 유일해진 뒤,
        #   펌프 전원이 데몬보다 늦게 켜지면 pump_map 이 빈 채 영구 무토출이 되던 회복 불가를
        #   닫는다. 데몬의 주기 HW 감시가 "펌프 응답 + pump_map 부재"를 확인하면 이 콜백으로
        #   우아한 재기동 → systemd 가 재기동 → boot 스캔이 이번엔 펌프를 찾는다(Undeclared
        #   재fetch 와 동일 계약 — 핫스왑 대신 재기동으로 경계를 명확히). pump_map 이 있으면
        #   (env 명시·스캔 성공) 데몬이 이 콜백을 부르지 않는다.
        on_pumps_seen_unmapped=(
            _make_pump_rediscovery_restart(logger, components.device_id)
            if _should_arm_pump_rediscovery(resolver.pump_map, components.engine, watch_addrs)
            else None
        ),
    )
    # ── Undeclared 자가복구(2026-09-02 상태모델 D3) — 선언 미확정 부팅이면 주기 재fetch 스레드. ──
    #   성공(유효 모델 수신) 시 캐시가 기록되고 데몬을 정상 종료시킨다 → systemd Restart=always 가
    #   재기동해 새 선언으로 조립(핫스왑 없이 경계가 명확). 성공했는데 여전히 미지값이면 백오프
    #   지속 + WARN(무한 타이트루프 금지). 도착 봉투는 UndeclaredEngineAdapter 가 정직 실패 처리.
    if getattr(components, "hardware_source", "") == "undeclared":
        from ..adapters.settings_source import (
            fetch_settings_once,
            hardware_profile_from_snapshot,
            pump_model_from_settings,
        )
        from ..persistence.hardware_profile_cache import save_profile
        from .bootstrap import SENLYT_STATE_DIR_ENV

        def _undeclared_refetch() -> None:
            delay = 60.0
            state_dir = environ.get(SENLYT_STATE_DIR_ENV, "").strip() or environ.get(
                "LOG_DIR", ""
            ).strip()
            while True:
                time.sleep(delay)
                try:
                    snap = fetch_settings_once(
                        components.server_config, components.identity.dispenser_token, components.mode
                    )
                except Exception:  # noqa: BLE001 — 재fetch 실패 = 백오프 지속.
                    snap = None
                model = pump_model_from_settings(snap)
                if model is not None:
                    if state_dir:  # 명시 상태 경로에서만 캐시(무설정 = cwd 오염 방지·bootstrap 동일).
                        # 조립 규칙은 헬퍼가 SoT(R6.5 M3) — bootstrap 스냅샷 경로와 바이트 동일 프로파일.
                        save_profile(
                            state_dir,
                            hardware_profile_from_snapshot(model, snap),
                            components.server_config.base_url,
                        )
                    logger.warn(
                        f"하드웨어 선언 수신(model={model}) — 정상 종료 후 재기동으로 재조립합니다",
                        stage=STAGE_PI_RECEIVED,
                    )
                    os.kill(os.getpid(), signal.SIGTERM)  # 우아한 종료 → systemd 재기동.
                    return
                if snap is not None:
                    logger.warn(
                        "재fetch 성공했으나 펌프 모델이 여전히 미확정 — 백오프 지속(admin 센소리움 확인)",
                        stage=STAGE_PI_RECEIVED,
                    )
                delay = min(delay * 2, 300.0)

        threading.Thread(target=_undeclared_refetch, name="hw-refetch", daemon=True).start()

    daemon = SenlytDaemon(deps)
    _install_signal_handlers(daemon, logger)

    logger.info(
        "senlytd 상시 소비 루프 실행 시작(SENLYT_RUN)",
        stage=STAGE_PI_RECEIVED,
        device_id=components.device_id,
        baseUrl=components.server_config.base_url,
        engine=type(components.engine).__name__,
    )
    daemon.boot()  # stop 까지 블록 — 종료 시 shutdown(우아한 종료) 수행.
    return 0


# ── 호스트 열/전압/스로틀 프로브(2026-07-25) — 실기기 과열→통신끊김 진단의 관측 씨앗 ───────────
#   전부 **read-only OS 조회**(하드웨어 명령 0·펌프 미접촉 → 발열·거동에 영향 없음). vcgencmd 미가용
#   (비-Pi 개발기)이면 sysfs 폴백, 그것도 없으면 skip — 관측이 부팅을 막지 않는다(예외 전부 흡수).
_THROTTLE_BITS: dict[int, str] = {
    0: "undervolt_now",
    1: "arm_capped_now",
    2: "throttled_now",
    3: "soft_temp_now",
    16: "undervolt_since_boot",
    17: "arm_capped_since_boot",
    18: "throttled_since_boot",
    19: "soft_temp_since_boot",
}


def _vcgencmd(arg: str) -> str | None:
    """vcgencmd 서브명령 1회(read-only). 미설치·비-Pi·실패·타임아웃 → None."""
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        res = subprocess.run(  # noqa: S603 — 고정 실행파일·리터럴 인자(사용자 입력 아님).
            [exe, *arg.split()],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except Exception:  # noqa: BLE001 — 관측이 부팅을 막지 않는다.
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _soc_temp_c() -> float | None:
    """SoC 온도(°C) — 리눅스 표준 sysfs 폴백(vcgencmd 없어도 됨)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _probe_host_thermal(logger: StructuredLogger) -> None:
    """부팅 시 호스트 열/전압/스로틀 1회 실측 로그(read-only·무해).

    라즈베리파이 과열→통신끊김 진단(2026-07-25). get_throttled 상위비트(16~19)는 **sticky**(부팅 후
    발생을 기억) — `undervolt_since_boot`=전력강하(H2) vs `throttled/soft_temp_since_boot`=열(H1) 을
    가른다. 재부팅 안 했으면 박람회 당시 발생 여부가 그대로 남아, 텔레메트리 구축 전에도 H1/H2 판별 가능.
    """
    temp = _vcgencmd("measure_temp")
    if temp is None:
        soc = _soc_temp_c()
        temp = f"{soc:.1f}'C(sysfs)" if soc is not None else None

    throttled_raw = _vcgencmd("get_throttled")
    throttled_hex: str | None = None
    flags: list[str] = []
    if throttled_raw and "=" in throttled_raw:
        throttled_hex = throttled_raw.split("=", 1)[1].strip()
        try:
            v = int(throttled_hex, 0)
            flags = [name for bit, name in _THROTTLE_BITS.items() if (v >> bit) & 1]
        except ValueError:
            pass

    # vcgencmd·sysfs 모두 없음(비-Pi 개발기) → 조용히 skip 로그 1줄.
    if temp is None and throttled_raw is None:
        logger.info(
            "호스트 열 프로브 — vcgencmd/thermal 미가용(비-Pi 개발기 추정) · skip",
            stage=STAGE_PI_RECEIVED,
        )
        return

    logger.info(
        "호스트 열 프로브(부팅 실측)",
        stage=STAGE_PI_RECEIVED,
        temp=temp,
        armClock=_vcgencmd("measure_clock arm"),
        coreVolts=_vcgencmd("measure_volts core"),
        throttled=throttled_hex,
        throttleFlags=flags or ["none"],
    )


def main(argv: list[str] | None = None) -> int:
    environ = os.environ
    logger = StructuredLogger()

    # 부팅 시 호스트 열/전압/스로틀 1회 실측(read-only) — 실기기 과열→통신끊김 진단 관측 씨앗(2026-07-25).
    #   RUN/SELFTEST/기본 어느 경로든 찍혀, `senlytd` 한 번만 돌려도 Pi 실측이 로그로 나온다.
    _probe_host_thermal(logger)

    if _is_truthy(environ.get(SENLYT_RUN_ENV)):
        return _run(environ, logger)

    if _is_truthy(environ.get(SENLYT_SELFTEST_ENV)):
        return _selftest(environ, logger)

    # 기본 경로 — 네트워크 없이 결선 준비 상태만 알린다(상시 루프는 SENLYT_RUN 스위치).
    logger.info(
        "senlytd v1.2.0 시작 — 실 어댑터(등록·SSE·역보고) 결선 준비 완료. "
        "상시 소비 루프는 SENLYT_RUN=1 에서 실행(무설정은 안전 종료). "
        "SENLYT_SELFTEST=1 로 조립 self-test 가능.",
        stage=STAGE_PI_RECEIVED,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
