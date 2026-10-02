"""펌프 포트를 "펌프가 응답하는가"로 고른다(2026-10-02 실기기 사고 후속).

사고: 펌프 포트를 후보 목록 순서·"열리는가"로만 골라, 펌프 없는 포트(Pi 내장 UART ttyAMA10)를 쥔 채
무응답에 갇혔다. 계약: ① 부팅·재감지는 펌프가 응답한 포트를 고른다 ② 가동 중 전 펌프 무응답이면 펌프가
응답하는 다른 포트를 찾아 재기동(→ 부팅 감지가 그 포트로 조립)한다 ③ Runze(sy01b)·Tecan 둘 다 같은 규칙(read-only `?`·`&` 만 쓴다).
"""

import dataclasses

from senlyt_pi.adapters.pump_model_detect import DetectResult, detect_on_candidates
from senlyt_pi.adapters.serial_port_discovery import SerialPortInfo
from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.app.bootstrap import build_components
from senlyt_pi.config.server_target import SENLYT_ENV_KEY
import test_pump_model_autodetect_20260914 as _auto
from test_pump_model_autodetect_20260914 import RUNZE, SY01B_SNAP, TECAN, _store
from test_sy01b_engine_adapter import FakeSerial, status_frame

_TWO_PORTS = [
    SerialPortInfo(device="/dev/ttyUSB0", vid=0x1A86, pid=0x7523),  # 목록 첫 포트(알려진 어댑터) — 펌프 없음
    SerialPortInfo(device="/dev/ttyUSB1", vid=None, pid=None),  # 펌프는 여기
]


def _det(model: str, fp: str) -> DetectResult:
    return DetectResult(model=model, responding=(1, 2, 3), fingerprints={1: fp, 2: fp, 3: fp}, strong=True)


class TestDetectOnCandidates:
    def test_picks_first_port_where_pumps_answer(self):
        seen: list[str] = []

        def detector(port, addrs):
            seen.append(port)
            return _det("tecan_xcalibur", TECAN) if port == "/dev/ttyUSB1" else DetectResult(None, ())

        port, det = detect_on_candidates(["/dev/ttyUSB0", "/dev/ttyUSB1"], [1, 2, 3], detector)
        assert port == "/dev/ttyUSB1" and det is not None and det.model == "tecan_xcalibur"
        assert seen == ["/dev/ttyUSB0", "/dev/ttyUSB1"]

    def test_no_answer_anywhere_keeps_first_candidate(self):
        port, det = detect_on_candidates(["/dev/ttyUSB0", "/dev/ttyUSB1"], [1], lambda p, a: DetectResult(None, ()))
        assert port == "/dev/ttyUSB0" and det is not None and det.responding == ()

    def test_detector_error_on_one_port_does_not_stop_search(self):
        def detector(port, addrs):
            if port == "/dev/ttyUSB0":
                raise OSError("busy")
            return _det("sy01b", RUNZE)

        port, det = detect_on_candidates(["/dev/ttyUSB0", "/dev/ttyUSB1"], [1], detector)
        assert port == "/dev/ttyUSB1" and det is not None and det.model == "sy01b"

    def test_no_candidates(self):
        assert detect_on_candidates([], [1], lambda p, a: _det("sy01b", RUNZE)) == (None, None)


class TestBootPicksAnsweringPort:
    """목록 첫 포트가 아니라 펌프가 응답한 포트로 엔진을 조립한다 — Runze·Tecan 동일."""

    def _components(self, tmp_path, detector):
        return build_components(
            {SENLYT_ENV_KEY: "v1_2_0"},
            identity_store=_store(tmp_path),
            register=False,
            fetch_settings=True,
            settings_fetcher=lambda cfg, tok, mode: dict(SY01B_SNAP),
            port_lister=lambda: list(_TWO_PORTS),
            pump_detector=detector,
        )

    def test_tecan_on_second_port(self, tmp_path):
        comp = self._components(
            tmp_path, lambda p, a: _det("tecan_xcalibur", TECAN) if p == "/dev/ttyUSB1" else DetectResult(None, ())
        )
        assert isinstance(comp.engine, TecanXCaliburEngineAdapter)
        assert comp.engine.port == "/dev/ttyUSB1"

    def test_runze_on_second_port(self, tmp_path):
        comp = self._components(
            tmp_path, lambda p, a: _det("sy01b", RUNZE) if p == "/dev/ttyUSB1" else DetectResult(None, ())
        )
        assert type(comp.engine) is Sy01bEngineAdapter
        assert comp.engine.port == "/dev/ttyUSB1"

    def test_no_pump_answers_falls_back_to_first_candidate(self, tmp_path):
        comp = self._components(tmp_path, lambda p, a: DetectResult(None, ()))
        assert comp.engine.port == "/dev/ttyUSB0"  # 종전과 같다(펌프 전원이 늦게 켜지는 경우).


def _silent() -> FakeSerial:
    return FakeSerial(default=b"")


def _answering() -> FakeSerial:
    return FakeSerial(default=status_frame(0, ready=True))


class TestFindAnsweringPort:
    """가동 중 정찰 — 펌프가 응답하는 다른 포트를 **찾기만** 한다(연결은 그대로)."""

    def _adapter(self, cls, ports: dict, cands):
        made: dict[str, list[FakeSerial]] = {}

        def factory(port, baud, timeout):
            fake = ports[port]()
            made.setdefault(port, []).append(fake)
            return fake

        ad = cls(port="/dev/ttyACM0", serial_factory=factory, port_resolver=lambda: list(cands))
        return ad, made

    def test_tecan_finds_answering_port_with_read_only_frames(self):
        ad, made = self._adapter(
            TecanXCaliburEngineAdapter,
            {"/dev/ttyACM0": _silent, "/dev/ttyUSB0": _answering},
            ["/dev/ttyUSB0", "/dev/ttyACM0"],
        )
        assert ad.find_answering_port([1, 2]) == "/dev/ttyUSB0"
        assert ad.port == "/dev/ttyACM0"  # 연결은 건드리지 않는다(옮기는 건 재기동 정책).
        sent = [f for fake in made["/dev/ttyUSB0"] for f in fake.written]
        assert sent == ["/1?\r"]  # Tecan 에도 `?` 만 — Q(래치 소진)·U·N·Z 없음.
        assert all(fake.closed for fake in made["/dev/ttyUSB0"])  # 정찰 핸들은 닫는다.

    def test_runze_finds_answering_port(self):
        ad, _ = self._adapter(
            Sy01bEngineAdapter,
            {"/dev/ttyACM0": _silent, "/dev/ttyUSB0": _answering},
            ["/dev/ttyACM0", "/dev/ttyUSB0"],
        )
        assert ad.find_answering_port([1]) == "/dev/ttyUSB0"

    def test_none_when_other_port_is_also_silent(self):
        ad, _ = self._adapter(
            TecanXCaliburEngineAdapter,
            {"/dev/ttyACM0": _silent, "/dev/ttyUSB0": _silent},
            ["/dev/ttyUSB0", "/dev/ttyACM0"],
        )
        assert ad.find_answering_port([1]) is None

    def test_single_adapter_with_pumps_off_opens_nothing(self):
        """정상 단일 어댑터에서 펌프 전원만 꺼진 경우 — 다른 후보가 없어 아무 포트도 열지 않는다."""
        ad, made = self._adapter(TecanXCaliburEngineAdapter, {"/dev/ttyACM0": _silent}, ["/dev/ttyACM0"])
        assert ad.find_answering_port([1, 2, 3, 4]) is None
        assert made == {}


class TestDaemonRestartsWhenPumpsAnswerElsewhere:
    _daemon_base = _auto.TestIdleWatchAndPolicy._daemon

    class _Eng:
        MODEL_ID = "tecan_xcalibur"
        port = "/dev/ttyACM0"

        def __init__(self, health, found="/dev/ttyUSB0"):
            self.health = health
            self.found = found
            self.scouted: list[list[int]] = []

        def health_probe(self, addr):
            return self.health[addr]

        def find_answering_port(self, addrs):
            self.scouted.append(list(addrs))
            return self.found

    def _daemon(self, eng, watch=(1, 2)):
        fired: list[int] = []
        d = self._daemon_base(eng, lambda: None, watch=watch)
        d.deps = dataclasses.replace(d.deps, on_pump_port_moved=lambda: fired.append(1))
        return d, fired

    def test_all_silent_and_found_elsewhere_restarts_once(self):
        eng = self._Eng({1: "silent", 2: "silent"})
        d, fired = self._daemon(eng)
        d._refresh_hw_health()
        assert eng.scouted == [[1, 2]] and fired == [1]
        d._refresh_hw_health()
        assert fired == [1]  # 프로세스당 1회 — 재기동 루프 방지.

    def test_scout_asks_only_boot_expected_addresses(self):
        """정찰 주소 = 부팅 감지 기대 주소 — env 고정 주소(감시엔 있으나 부팅 감지엔 없는)로 찾으면
        부팅이 그 포트를 못 골라 프로세스마다 재기동이 반복된다(검증 재현 2026-10-02)."""
        eng = self._Eng({1: "silent", 5: "silent"})
        d, _ = self._daemon(eng, watch=(1,))
        d._check_pump_port_moved({1: "silent", 5: "silent"})
        assert eng.scouted == [[1]]

    def test_any_answer_means_right_port(self):
        eng = self._Eng({1: "ok", 2: "silent"})
        d, fired = self._daemon(eng)
        d._refresh_hw_health()
        assert eng.scouted == [] and fired == []

    def test_nothing_found_does_not_restart(self):
        eng = self._Eng({1: "silent", 2: "silent"}, found=None)
        d, fired = self._daemon(eng)
        d._refresh_hw_health()
        assert eng.scouted == [[1, 2]] and fired == []

    def test_no_scouting_while_hot_applying(self):
        eng = self._Eng({1: "silent", 2: "silent"})
        d, fired = self._daemon(eng)
        d._hot_applying = True
        d._check_pump_port_moved({1: "silent", 2: "silent"})
        assert eng.scouted == [] and fired == []

    def test_incident_end_to_end_tecan(self):
        """사고 재현 — 펌프 없는 포트를 쥔 실제 Tecan 어댑터: 감시 1주기에 펌프 포트를 찾아 재기동 정책을 부른다."""
        ports = {"/dev/ttyACM0": _silent, "/dev/ttyUSB0": _answering}
        ad = TecanXCaliburEngineAdapter(
            port="/dev/ttyACM0",
            serial_factory=lambda p, *_a: ports[p](),
            port_resolver=lambda: ["/dev/ttyACM0", "/dev/ttyUSB0"],
        )
        d, fired = self._daemon(ad, watch=(1,))
        d._refresh_hw_health()
        assert fired == [1]
        assert ad.port == "/dev/ttyACM0"  # 연결 교체 없음 — 재기동 후 부팅 감지가 ttyUSB0 를 고른다.
