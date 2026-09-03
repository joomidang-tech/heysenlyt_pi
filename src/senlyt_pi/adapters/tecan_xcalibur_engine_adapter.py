"""Tecan Cavro XCalibur 시린지 펌프 RS485 실어댑터 — EnginePort 실구현 (제조사 차이 재정의판).

왜 별도 구현체인가
------------------
SY-01B 는 Cavro DT 프로토콜의 클론이라 프레임 문법(`/{addr}{cmds}R\\r` → `/0{status}…ETX`)·
밸브 회전(I/O)·플런저 절대이동(A)·에러코드 체계(0/1/2/3/7/9/10/11/15)·브로드캐스트(`_`=5Fh)가
전부 호환된다. 그래서 **버스락·폴링·ack-tolerant·핫플러그 자가회복·estop 래치 등 검증된 기계는
`Sy01bEngineAdapter` 를 그대로 상속**하고, 제조사가 실제로 다른 "기종별 차이" 지점만 재정의한다.
계약(EnginePort)은 동일 — 상위(EngineExecutor·pump_sequencer·정비 툴)는 아무것도 모른다.

기종별 차이 (SoT: `developer/hey_senlyt/v1.3.0/00_research/
"Manual Operating Cavro XCalibur 20733085-C.txt"` — 이하 "매뉴얼")
------------------------------------------------------------------
1. **`U` 절대 금지** — sy01b 의 `U{code},{stall}R` 은 스톨전류 설정이지만, XCalibur 의 `U<n>` 은
   **NVM(EEPROM) 설정 기록**이다(매뉴얼 §3.3.2 Table 3-5: 밸브 타입·보드레이트·프로토콜·
   safe-init — 다음 전원인가 시 적용). 스톨전류 개념 자체가 없다 — 과부하 감지(err 9/10)는
   펌웨어 내장. → `_pre_init_commands` 가 U 대신 `N0R` 을 보낸다.
2. **`N0` 표준 모드 고정** — 풀스트로크 3000 증분(N0) / 24000(N1 미세모드) (§3.3.2).
   프리셋(pump_full_stroke=3000)이 N0 을 전제하므로, 펌웨어 기본값에 의존하지 않고
   (CLAUDE.md 제1원칙 7: 물리 결과를 바꾸는 파라미터는 항상 명시) 매 셋업마다 N0 을 못박는다.
   N1(미세모드)은 속도 단위가 increments/sec 로 바뀌므로 실기기 프로브로 확정 전 도입 금지.
3. **상태 폴 = `Q`** — 매뉴얼 §3.6: *"[Q] is the only valid method for obtaining pump status
   in serial mode"* — busy 비트(Bit5)는 Q 응답에서만 신뢰 가능하다(다른 명령의 answer block
   상태비트는 busy 판정에 쓰지 말 것·§3.6.1 Note). `?` 는 양 기종 다 위치 리포트지만, XCalibur
   는 위 §3.6.1 명시 + Q 래치 소진 실측 때문에 판정/관찰 명령을 갈랐다.
   → `_status_cmd = "Q"` (에러 nibble 해석은 동일 — `parse_status` 그대로).
3b. **정지 = `T` 단독** — §3.5.5: *"[R] … resumption of a halted or terminated command
   string"* — `TR` 한 프레임은 T 가 종료한 그 문자열을 뒤따르는 R 이 **재개**할 수 있다
   (에러로 멈춘 펌프는 버퍼가 비어 무해하나, 건강한 in-flight 모션의 긴급정지가 위험).
   T 는 R 불요 Control 명령(§3.3.1·부록 G.9). → `TERMINATE_CMD = "T"`.
   ⚠️ 모션 중 TR-vs-T 실기기 실측은 백로그(매뉴얼-안전형 선택).
4. **속도 하한** — v 50..1000 · V 5..6000 · c 50..2700 (§3.5.3). sy01b `_speed_cmd` 는 하한
   1 까지 떨어질 수 있어 XCalibur 에선 err 3(invalid operand)이 된다 → 하한 클램프 추가.
   상한은 프리셋(`tecan_xcalibur`)이 이미 반영(c ≤ 2700 등).
5. **초기화 문법 동일** — `Z{힘},{흡입포트},{배출포트}R` (§3.4.3: n1=힘 0/1/2·n2=init input·
   n3=init output). 힘 권장표(§3.4.2 Table 3-6: ≥1.0mL=Full·250/500µL=Half·50/100µL=Third)도
   `SyringeSpec._init_force` 파생과 일치 — 재정의 불필요. 기본 초기화 속도 500Hz(§3.4.3)도 동일.
6. **에러 복구 의미** — err 9/10(오버로드)은 T 로 안 지워지고 **재초기화(Z)로만** 해제(§3.6.3).
   부모의 복구 경로가 정확히 그 순서다: TR(정지·무해) → 셋업 캐시 무효화 → 다음 `_ensure_ready`
   가 Z 재초기화. err 15(Command Overflow)는 "이동 중 새 명령 무시(NAK)" — sy01b 와 동일
   의미론이라 `_settle` 의 busy-NAK 재전송 로직이 그대로 유효하다(§3.6.3: 재초기화 불필요).

✅ 실기기 실측 확정 (2026-09-03 벤치 브링업 — 정본: heysenlyt-hardware-test-tool/
   tecan_bringup_full.ipynb, 84왕복 전 프레임 기록. 펌웨어 30064809 C · ?76=
   "9600|100K|484|3-way|AUTO" · 리니어 엔코더(?4) 장착 개체)
--------------------------------------------------------------------------
  - Q 폴은 Z 모션 중에도 clean (0.5s 폴 전량 정상 프레임 — sy01b 2026-07-22 와 동일 결론).
    busy 중 `?` 리포트도 수락(§3.6.1 실측 — _health_cmd="?" 선택 유효).
  - 브로드캐스트(`_`) 직후 버스 오염 **없음** — sy01b 고유 현상으로 확정. 플러시 로직은
    무해하므로 유지.
  - 초기화 실측 7.3s(건식·0.25mL·Third 힘) — HOME_SETTLE_S(30s)는 과대하나 안전측이라
    유지(하향하려면 실 시린지·액체 조건 재실측 필요).
  - "[Q] clears the error" **실측 확정**: 에러 후 Q 1발 = 에러 보고(0x63/0x67), 2발 = 0x60
    — Q 가 래치를 소진한다. `_health_cmd` 를 Q 로 두면 안 되는 이유가 실물로 증명됨.
    (오버로드 err9/10 래치의 Q 소거는 의도 유발 불가라 미실측·보류 — 방어는 동일.)
  - 축 밖 A4000: 초기화 전 err7 / 후 err3, **양쪽 다 무모션 즉답 거부** + 위치 불변.
  - 속도 readback(?1/?2/?3) 라운딩 오차 α=0 (50/200/50 정확 반환) — readback 판정을
    정확 일치로 걸어도 된다.
  - 응답 프레임 후행에 CR(0x0D)이 붙는 케이스 관측 — ETX 이후 바이트 관용 필수.
  - **프로덕션 경로 종단 실측**: 이 어댑터 그대로(serial_factory 주입) probe→health→
    initialize_polled(4.1s)→aspirate/dispense 10µL(=120 steps·0.25mL/3000축 파생 정확)
    전부 code 0 — 부피축 계약이 실물에서 성립.

⛔ 여전히 미확정 (실측 불가/보류)
  - N0 전원 사이클 휘발 여부(Tier D 미실행) — 매 셋업 재전송 방어 유지로 무해.
  - N0 직접 readback 불가 확정(?76 텍스트에 N 모드 미표시) — 간접판별(?12=12·?24=50)뿐.
  - 오버로드(err9/10) 래치 거동 — 위 참조.
"""

from __future__ import annotations

import threading

from ..core.pump_guard import PUMP_PRESETS, PumpPreset, SyringeSpec
from ..obs.log import StructuredLogger
from .sy01b_engine_adapter import (
    _RECONNECT_RESEND_SAFE,
    DEFAULT_BAUDRATE,
    DEFAULT_INIT_TIMEOUT_S,
    DEFAULT_MOTION_TIMEOUT_S,
    SERIAL_READ_TIMEOUT_S,
    SerialFactory,
    Sy01bEngineAdapter,
)

# XCalibur 상태조회 — 매뉴얼 §3.6 "[Q] is the only valid method for obtaining pump status".
#   응답은 sy01b `?` 와 동일 구조(`/0{status}` · 데이터 블록 길이 0)라 parse_status 그대로.
TECAN_STATUS_QUERY = "Q"

# 속도 하한 — 매뉴얼 §3.5.3 (v 50..1000 · V 5..6000 · c 50..2700). 하한 위반 = err 3.
#   V 의 하드웨어 하한은 5 지만, v ≤ c ≤ V 단조성(§3.5.4)과 세 범위의 교집합을 지키기 위해
#   최상속도(top)도 50 을 바닥으로 클램프한다(실사용 속도는 수백 Hz 이상이라 실질 영향 0).
TECAN_MIN_SPEED_HZ = 50


class TecanXCaliburEngineAdapter(Sy01bEngineAdapter):
    """Tecan Cavro XCalibur 어댑터 — Cavro 계열의 기종별 차이 재정의판(기계는 Sy01b 상속).

    **한 어댑터 = 한 버스**(부모와 동일). 프리셋 미지정 시 `tecan_xcalibur`
    (풀스트로크 3000·N0 표준 모드·XCalibur 속도 상한)를 기본으로 쓴다.
    """

    # sy01b 지문 게이트는 자기 기종이므로 비활성(R9 P2-2 — 명시 선언).
    FOREIGN_FP_GUARD = False
    MODEL_ID = "tecan_xcalibur"
    # 정지 = `T` 단독 — `TR` 은 R 이 종료된 명령 문자열을 재개할 수 있다(§3.5.5 · 헤더 3b).
    TERMINATE_CMD = "T"
    # 속도 하한 50 — 부모 `_speed_cmd` 공식이 이 값으로 그대로 돈다(본문 재정의 없음 · 검증 P2).
    MIN_SPEED_HZ = TECAN_MIN_SPEED_HZ
    # err7 = 무모션 즉답 거부(벤치 실측 — "홈 재탐색 중"이 아니다) → 폴에서 즉시 실패.
    ERR7_REHOMES = False

    def __init__(
        self,
        *,
        port: str = "/dev/ttyUSB0",
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout_s: float = SERIAL_READ_TIMEOUT_S,
        motion_timeout_s: float = DEFAULT_MOTION_TIMEOUT_S,
        init_timeout_s: float = DEFAULT_INIT_TIMEOUT_S,
        preset: PumpPreset | None = None,
        serial_factory: SerialFactory | None = None,
        stop_event: threading.Event | None = None,
        estop_event: threading.Event | None = None,
        logger: StructuredLogger | None = None,
        port_resolver=None,
    ) -> None:
        super().__init__(
            port=port,
            baudrate=baudrate,
            read_timeout_s=read_timeout_s,
            motion_timeout_s=motion_timeout_s,
            init_timeout_s=init_timeout_s,
            # ⚠️ 부모 기본(clamp_pump_preset → sy01b·12000)을 쓰면 스텝 파생이 4배 과다해져
            #   플런저가 물리 한계(3000)를 넘는다(err 3) — Tecan 프리셋을 명시 기본으로.
            preset=preset if preset is not None else PUMP_PRESETS["tecan_xcalibur"],
            serial_factory=serial_factory,
            stop_event=stop_event,
            estop_event=estop_event,
            logger=logger,
            port_resolver=port_resolver,
        )
        # 주입 preset 가드(2026-09-03 검증 P2) — sy01b preset(c 상한 5400 등)을 실수로 꽂으면
        #   _speed_cmd 가 매뉴얼 범위 밖(c>2700 = err3·명령 폐기) 프레임을 만든다. fail-closed.
        pz = self.preset
        if (pz.pump_max_cutoff_speed_hz > 2700 or pz.pump_max_top_speed_hz > 6000
                or pz.pump_max_start_speed_hz > 1000):
            raise ValueError(
                f"XCalibur 프리셋 범위 밖(v≤1000·V≤6000·c≤2700 — §3.5.3): {pz!r}"
            )
        # 기종별 차이 지점(seam) 재정의(부모 __init__ 가 sy01b 기본값을 세팅한 뒤 덮는다).
        self._status_cmd = TECAN_STATUS_QUERY
        # 부팅 발견 probe 는 **?**(위치 리포트) — Q 는 래치를 소진한다(벤치 실측: Q 1발=에러 보고
        #   →2발=0x60). 발견 판정은 "응답 여부"뿐이라 ? 로 충분하고, health(?)·probe(?)가 같은
        #   명령이 되어 재발견(R8) 술어 동일성도 오히려 강해진다(R9 P2-6).
        self._probe_cmd = "?"
        # 관찰 전용(하트비트 health) = `?`(위치 리포트) — Q 와 분리(2026-09-01 검증 P1-a).
        #   §3.6.3 "[Q] clears the error": 주기 관찰이 Q 면 오버로드(err9/10) 래치를 지워
        #   증거가 사라지고, 토출 폴이 에러를 못 보는 거짓 성공 창이 생긴다. `?` 는 에러
        #   nibble 이 항상 유효(§3.6.1)하고 Report 라 busy 중에도 수락 — 관찰엔 충분·무부작용.
        #   판정 폴(_status_cmd=Q)·부팅 발견(probe=Q)은 유지 — 읽고 행동하는 단일 소비자.
        self._health_cmd = "?"
        # 멱등(모션 무발생) 재전송 허용 집합 — 이 기종의 정지 프레임은 `T`(TR 아님)라
        #   부모 집합을 그대로 합치지 않고 재선언한다. Q 도 Report 라 멱등.
        self._resend_safe = frozenset({"?", self.TERMINATE_CMD, TECAN_STATUS_QUERY})
        # tecan 명시 기기만 probe 에서 `&` 펌웨어 관측 채집(R3 P2-4 — sy01b 기본 경로 불변).
        self._fw_probe_capture = True
        # `?76`/`&` readback 관측은 주소당 1회(R3 P2-3) — 관측 목적(포맷 채집)은 1회면 충분하고,
        #   무응답 링크에선 read_timeout×2 가 재셋업(오버로드 복구 직후 포함)마다 붙는다.
        self._readback_observed: set[int] = set()

    # ── 기종별 차이 1·2: U(스톨전류) 금지 → N0(표준 모드) 고정 ────────────────────────
    def _pre_init_commands(self, spec: SyringeSpec) -> "tuple[str, ...]":
        """홈(Z) 앞 셋업 — XCalibur 는 `N0R`(미세모드 OFF·풀스트로크 3000 고정) 1발.

        ⛔ sy01b 의 `U{code},{stall}R` 을 보내지 않는다 — XCalibur `U<n>` 은 NVM 설정 기록
        (매뉴얼 Table 3-5)이라 오용 시 밸브 타입·보드레이트가 바뀔 수 있다. 스톨전류 설정은
        존재하지 않는다(과부하 감지 err 9/10 = 펌웨어 내장·§3.6.3).

        `N0R` 인 이유: 프리셋 풀스트로크(3000)가 N0 표준 모드 전제다. 펌웨어가 N1 로 남아
        있으면 같은 A{steps} 가 1/8 부피로 토출된다(24000 증분 축) — 기본값 의존 금지 원칙
        (CLAUDE.md 제1원칙 7)대로 매 셋업마다 명시한다. 모션 없는 멱등 명령이라 브로드캐스트/
        폴 초기화 경로에서도 안전하다(부모 계약 그대로).

        ⚠️ R9 P2-1 정정 — 종전 주석은 _axis_sealed 가 "설정 tecan + 실물 SY-01B" 를 봉인한다고
        주장했지만, 단일 키 설계에선 spec·preset 이 같은 선언에서 나와 그 봉인이 정상 부팅에서
        도달 불가다. **역방향(N0R→SY-01B 실물)은 현재 미방어·미실측** — SY-01B 지문(&) 실측 후
        게이트를 대칭으로 세울 것(백로그). 그때까지 tecan 설정을 실물 SY-01B 에 쓰지 말 것.
        """
        if self._axis_sealed(spec):
            return ()
        return ("N0R",)

    # ── 셋업 마감 — N0(3000축) **검증 송신** + 설정·펌웨어 readback **관측** (P0-2·P1-1) ──
    def _finalize_setup(self, addr: int, spec: SyringeSpec) -> int:
        """캐시 등록 직전 마감 — ① `N0R` 을 **응답 확인하며** 1발 더 보낸다 ② readback 관측 로그.

        왜 ①: 초기화 발사 경로(`initialize_polled` phase 1·broadcast)는 즉답을 의도적으로 안
        본다(fire-and-forget — sy01b 실측 근거). 그 관대함이 XCalibur 에선 위험 등급이 다르다 —
        `N0R` 이 NAK/깨짐으로 유실된 채 펌웨어가 N1(24000축)로 남으면 같은 A{steps} 가 **에러
        없이 1/8 부피**로 토출된다(검증 P0-2). 여기서는 모터가 이미 idle(홈·주차 완료 후)이라
        NAK 여지가 없고, `_settle` 이 즉답을 정직하게 판정한다 — 실패면 그 코드를 그대로 반환해
        캐시 등록을 막는다(다음 시도 재셋업).

        왜 ②(관측 전용 — 판정 없음): `?76`(Report Pump Configuration)·`&`(firmware version)의
        응답 포맷은 매뉴얼에 미정의라(⚠️ 실기기 미실측) **어떤 하드 판정도 걸지 않는다** —
        substring 판정은 미확정 포맷 위의 킬 스위치가 된다(검증 P1-1: 오탐 시 실물 도착 첫날 전
        제조 전멸). 원문을 로그로 채집해 브링업에서 포맷 확정 후 게이트로 승격한다. 아울러
        `&` 원문은 "실물이 정말 XCalibur 인가"(검증 P0-1 — 소프트웨어 두 키가 모두 sy01b 인데
        실물만 XCalibur 인 무성 4배 조합)의 브링업 판별 재료다.
        """
        # ⛔ 축 불일치 봉인(R3 P2-2) — 봉인 조합에선 기종 전용 프레임(N0R·readback)을 내보내지
        #   않고 통과(0). 홈·주차는 이미 끝났고 토출은 축 가드(-1001)가 거부하므로 안전하다.
        if self._axis_sealed(spec):
            return 0
        # ⚠️ poll=True·ack_tolerant=False (R3 P0-A) — poll 은 즉답 판정 뒤 busy-NAK(err15)
        #   재전송·latched 에러 표면화를 위해 유지하되, **garbled(무응답·깨진 프레임)는 정직하게
        #   실패**시킨다. ack_tolerant 의 정당성("ACK 만 깨졌고 폴이 물리 완료를 실증")은 모션
        #   명령 전제다 — N0 은 모션이 없어 폴(Q)이 "N0 적용됨"과 "N0 유실됨"을 구분하지 못하고
        #   (둘 다 idle·err0), 관대함이 곧 무성 1/8 토출의 거짓 성공이 된다. `?76` readback 을
        #   실기기 포맷 확정 후 게이트로 승격하기 전까지, 유실은 실패로 남기는 것이 유일하게
        #   정직한 판정이다(CLAUDE.md 제1원칙 2).
        code = self._settle(addr, "N0R", self.read_timeout_s, poll=True, ack_tolerant=False)
        if code != 0:
            if self._log is not None:
                self._log.warn(
                    f"XCalibur N0(표준축) 확정 송신 실패(code {code}) — 셋업 실패 처리(1/8 토출 방지)",
                    stage="step_exec", pumpAddr=addr, engineCode=code,
                )
            return code
        # readback 관측은 주소당 1회(R3 P2-3) — 재셋업(오버로드 복구 직후 포함)마다 왕복 2회가
        #   임계경로에 붙는 것을 막는다. 관측 목적(포맷 채집)은 1회면 충분.
        if addr not in self._readback_observed:
            self._readback_observed.add(addr)
            for report_cmd in ("?76", "&"):
                try:
                    raw = self._txn(addr, report_cmd, read_timeout_s=self.read_timeout_s)
                except Exception:  # noqa: BLE001 — 관측 실패는 셋업 성패와 무관.
                    continue
                text = "".join(ch for ch in raw if 32 <= ord(ch) < 127)
                if self._log is not None:
                    self._log.debug(
                        f"XCalibur readback({report_cmd}) — 실기기 포맷 채집용 관측(판정 없음)",
                        stage="step_exec", pumpAddr=addr, response=text[:120],
                    )
        return 0

    # ── 기종별 차이 4: 속도 하한 — `MIN_SPEED_HZ = 50` **재선언뿐**(클래스 속성 위 참조).
    #   종전엔 부모 `_speed_cmd` 본문을 통째로 복붙하고 한 줄만 바꿨다(2026-09-03 검증 P2 —
    #   부모 공식이 진화하면 파생만 옛 공식에 남는 드리프트 폭탄). 하한을 값으로 빼고 본문은
    #   상속한다: top 이 50 바닥에 오르면 start=min(max_start,top)≥50 ·
    #   cutoff=max(min(max_cutoff,top),start)≥start 로 단조성과 하한이 동시에 성립(동일 증명).

    # ── 링크 리셋 캐시 무효화 — 부모(지문·셋업) + 이 기종의 관측 캐시(검증 P3) ──────────
    def _invalidate_link_caches(self) -> None:
        super()._invalidate_link_caches()
        if hasattr(self, "_readback_observed"):
            self._readback_observed.clear()  # 실물 교체/전원 사이클 후 ?76/& 재관측.
