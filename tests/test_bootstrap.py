"""bootstrap 테스트 — 실 어댑터 조립(ServerConfig 결정·엔진 분기·정체성)·fail-fast.

등록(실 HTTP)은 register=False + identity_store 선주입으로 네트워크 없이 조립을 검증하고,
등록 경로는 별도로 로컬 fake register 서버로 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from senlyt_pi.adapters.device_identity_store import DeviceIdentity, DeviceIdentityStore
from senlyt_pi.adapters.fake_engine_adapter import FakeEnginePort
from senlyt_pi.adapters.serial_port_discovery import SerialPortInfo
from senlyt_pi.adapters.sy01b_engine_adapter import Sy01bEngineAdapter
from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter
from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter
from senlyt_pi.adapters.valve_adapter import FakeValveAdapter
from senlyt_pi.app.bootstrap import (
    BootstrapError,
    _identity_path,
    _server_slug,
    build_components,
    build_engine,
    build_valve,
    pump_map_from_addresses_env,
)
from senlyt_pi.config.server_target import SENLYT_ENV_KEY, ServerTargetError
from support_http import FakeHttpServer

_CH340 = [SerialPortInfo(device="/dev/ttyUSB0", vid=0x1A86, pid=0x7523)]

IDENTITY = DeviceIdentity(device_id="dev-A", dispenser_token="tok-1", exp=9_999_999_999)


def _store(tmp_path) -> DeviceIdentityStore:
    store = DeviceIdentityStore(tmp_path / "identity.json")
    store.save(IDENTITY)
    return store


def test_assembles_real_adapters_from_env(tmp_path) -> None:
    env = {SENLYT_ENV_KEY: "v1_1_0"}
    comp = build_components(env, identity_store=_store(tmp_path), register=False)
    assert comp.device_id == "dev-A"
    assert comp.server_config.base_url == "https://v1-1-0.env.senlyt.com"
    # 어댑터가 동일 base·동일 토큰을 소비.
    assert comp.command_source.base_url == "https://v1-1-0.env.senlyt.com"
    assert comp.command_source.bearer_token == "tok-1"
    assert comp.status_sink.base_url == "https://v1-1-0.env.senlyt.com"
    assert comp.status_sink.bearer_token == "tok-1"
    # 엔진 기본 = 선언 없음 → Undeclared(모션 거부). 자동 fake 는 폐기(2026-09-04).
    assert isinstance(comp.engine, UndeclaredEngineAdapter)
    # logger 에 deviceId 바인딩.
    assert comp.logger.device_id == "dev-A"


def test_fail_fast_when_server_target_unset(tmp_path) -> None:
    with pytest.raises(ServerTargetError):
        build_components({}, identity_store=_store(tmp_path), register=False)


def test_missing_identity_without_register_raises(tmp_path) -> None:
    empty = DeviceIdentityStore(tmp_path / "none.json")
    with pytest.raises(BootstrapError):
        build_components(
            {SENLYT_ENV_KEY: "dev"}, identity_store=empty, register=False
        )


def test_explicit_base_url_escape_hatch(tmp_path) -> None:
    env = {"SENLYT_SERVER_BASE_URL": "http://web:3000"}
    comp = build_components(env, identity_store=_store(tmp_path), register=False)
    assert comp.server_config.base_url == "http://web:3000"


def test_engine_default_is_undeclared_not_fake() -> None:
    """선언 없음 = Undeclared. 호스트가 비-Pi 여도 자동 fake 로 떨어지지 않는다(2026-09-04)."""
    assert isinstance(build_engine({}), UndeclaredEngineAdapter)
    assert isinstance(build_engine({}, on_pi=lambda: False), UndeclaredEngineAdapter)


def test_engine_fake_only_by_explicit_switch_and_never_on_pi() -> None:
    """SENLYT_FAKE_ENGINE=1 은 테스트·E2E 전용 — 비-Pi 에선 fake, 실 Pi(GPIO)에선 거부."""
    assert isinstance(build_engine({"SENLYT_FAKE_ENGINE": "1"}, on_pi=lambda: False), FakeEnginePort)
    with pytest.raises(BootstrapError):
        build_engine({"SENLYT_FAKE_ENGINE": "1"}, on_pi=lambda: True)


def test_engine_injection_wins() -> None:
    injected = FakeEnginePort()
    assert build_engine({"SENLYT_ENGINE": "sy01b"}, engine=injected) is injected


# ── 자동감지("URL만" — SENLYT_ENGINE/VALVE 없이) ─────────────────────────────
def test_engine_declared_sy01b_on_pi_is_sy01b() -> None:
    """실 Pi + 서버 선언 sy01b → Sy01b (2026-09-02 단일 키 — 선택자는 pump_model 인자)."""
    eng = build_engine(
        {}, on_pi=lambda: True, port_lister=lambda: list(_CH340), pump_model="sy01b"
    )
    assert isinstance(eng, Sy01bEngineAdapter)


def test_engine_declared_sy01b_without_serial_is_still_sy01b() -> None:
    """실 Pi + 어댑터 미발견 + 선언 sy01b → **그래도 sy01b** (fake 조용 후퇴 금지 원칙 유지).

    (2026-07-19 개정 근거 그대로: fake 후퇴는 명령이 모의로 조용히 성공해 운영자가 오판한다.
    sy01b 로 기동해 정직하게 실패하고 핫플러그 자가 재연결이 붙는다.)"""
    eng = build_engine({}, on_pi=lambda: True, port_lister=list, pump_model="sy01b")
    assert isinstance(eng, Sy01bEngineAdapter)


def test_engine_undeclared_on_pi_assembles_undeclared_adapter() -> None:
    """실 Pi + 선언 없음(스냅샷·캐시 무효) → Undeclared **조립**(2026-09-02 fail-closed).

    ⛔ 폴백 sy01b 금지 — tecan 이라 선언됐던 기기가 미확정 부팅에서 sy01b 로 조립되면
    초기화 프리앰블 U…R 이 XCalibur NVM 에 기록된다.

    R6.5 P2 — 조립 선택(N4: clamp 금지)과 거부 코드(N11: -1002)는 서로 다른 급소라
    그물을 **두 테스트로 분리**한다. 한 테스트에 얹으면 그 테스트가 약해질 때 둘이 함께
    무방비가 된다."""
    from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter

    eng = build_engine({}, on_pi=lambda: True, port_lister=lambda: list(_CH340))
    assert isinstance(eng, UndeclaredEngineAdapter)


def test_engine_undeclared_refuses_all_motion_with_dedicated_code() -> None:
    """Undeclared 어댑터의 전 모션 = -1002(UNDECLARED_HW_RAW_CODE) 거부 — 성공 위장 금지.

    N11 그물 — initialize 가 0(성공)을 돌려주면 상위가 "펌프 준비됨"으로 오판해
    미확정 하드웨어에 토출 흐름이 이어진다."""
    from senlyt_pi.adapters.undeclared_engine_adapter import UndeclaredEngineAdapter
    from senlyt_pi.core.pump_guard import UNDECLARED_HW_RAW_CODE

    eng = UndeclaredEngineAdapter()
    assert eng.initialize().raw_error_code == UNDECLARED_HW_RAW_CODE
    assert eng.probe(1) is False


def test_engine_declared_tecan_on_pi_is_tecan() -> None:
    """실 Pi + 서버 선언 tecan_xcalibur → Tecan 어댑터(3000축)."""
    from senlyt_pi.adapters.tecan_xcalibur_engine_adapter import TecanXCaliburEngineAdapter

    eng = build_engine(
        {}, on_pi=lambda: True, port_lister=lambda: list(_CH340), pump_model="tecan_xcalibur"
    )
    assert isinstance(eng, TecanXCaliburEngineAdapter)


def test_engine_non_pi_with_declaration_builds_real_adapter() -> None:
    """비-Pi(맥북 등)도 서버 선언이 있으면 실물 어댑터 — 펌프를 꽂으면 진짜, 안 꽂으면 탐색대로 미연결."""
    eng = build_engine(
        {}, on_pi=lambda: False, port_lister=lambda: list(_CH340), pump_model="tecan_xcalibur"
    )
    assert isinstance(eng, TecanXCaliburEngineAdapter)
    eng2 = build_engine({}, on_pi=lambda: False, port_lister=lambda: [], pump_model="sy01b")
    assert isinstance(eng2, Sy01bEngineAdapter)


def test_engine_env_is_deprecated_and_ignored() -> None:
    """SENLYT_ENGINE 잔재는 **무시**된다(2026-09-02 단일 키 — 결정권은 서버 선언뿐).

    env=fake 여도 실 Pi + 선언 sy01b 면 sy01b 로 조립된다(env 가 선택에 관여하면 두 키가
    부활한다). 비-Pi 개발환경 fake 는 on_pi 게이트가 담당(아래 non_pi 테스트)."""
    eng = build_engine(
        {"SENLYT_ENGINE": "fake"},
        on_pi=lambda: True,
        port_lister=lambda: list(_CH340),
        pump_model="sy01b",
    )
    assert isinstance(eng, Sy01bEngineAdapter)


def test_valve_autodetect_non_pi_is_fake() -> None:
    # GPIO 없는 호스트 = 밸브 없음(None). 가짜 밸브가 "열렸다"고 답하는 자동 fake 는 폐기(2026-09-04).
    assert build_valve({}, on_pi=lambda: False) is None
    # fake 는 명시(테스트·E2E)일 때만.
    assert isinstance(build_valve({"SENLYT_VALVE": "fake"}, on_pi=lambda: False), FakeValveAdapter)


def test_valve_autodetect_pi_no_crash_and_no_fake() -> None:
    """실 Pi 자동감지 — gpio 시도. gpiozero 부재(개발기)면 None(밸브 없음)·부팅 중단 없음.
    어느 경우에도 가짜 밸브로 폴백하지 않는다(2026-09-04)."""
    v = build_valve({}, on_pi=lambda: True)
    assert not isinstance(v, FakeValveAdapter)


def test_valve_off_is_none() -> None:
    assert build_valve({"SENLYT_VALVE": "off"}) is None


def test_register_path_over_socket(tmp_path) -> None:
    """실 등록 경로 — 로컬 fake register 서버로 build_components(register=True) 왕복.

    [D-A] deviceId = SENLYT_HARDWARE_ID(수집 시리얼)로 확정 — 서버 echo(다른 값)는 무시.
    """
    ok_body = {"deviceId": "server-echo-ignored", "dispenserToken": "tok-X", "exp": 9_999_999_999}
    with FakeHttpServer() as srv:
        srv.set_handler(lambda req: {"status": 200, "json": ok_body})
        # TOFU(2026-07-17): 공유키 env 없음 — deviceId 만 제시. (서버가 200 승인 응답을 즉시 준 경우.)
        env = {
            "SENLYT_SERVER_BASE_URL": srv.base_url,
            "SENLYT_HARDWARE_ID": "hw-e2e",
        }
        store = DeviceIdentityStore(tmp_path / "id.json")
        comp = build_components(env, identity_store=store, register=True)
        assert comp.device_id == "hw-e2e"  # 시리얼 = deviceId(서버 echo 아님).
        # 등록 요청이 올바른 경로·인증헤더 없음·deviceId(시리얼)로 갔는지(TOFU).
        reg = srv.requests[-1]
        assert reg.path == "/api/dispensers/register"
        assert reg.header("Authorization") is None  # 공유키 제거 — 인증 헤더 없음.
        assert reg.json()["deviceId"] == "hw-e2e"
        # 정체성 파일 영속.
        assert store.load().device_id == "hw-e2e"


# ── PUMP_ADDRESSES 부트스트랩 pump_map (운영자 override·기설치 잔존값 계약) ────
#
# env 다이어트(2026-09-02)로 install.sh 는 이 키를 더는 각인하지 않는다(주소 SoT = 부팅
# 버스 스캔 자동인식). 그러나 ① 재설치 전 기존 기기의 각인값 ② 운영자가 넣는 고정 구성
# override(⚠️ 관리 키라 **다음 재설치가 걷어낸다** — 재설치 전까지만 유효)는 계속 이
# 파서로 들어온다 — 그 파싱 계약을 고정한다(비면 pump_map 이 비어
# 모든 레시피 스텝이 CMD_VALIDATION_FAILED 로 drop(토출 0) → 주문 실패).

_INSTALL_SH_PUMP_ADDRESSES = "flavor:1,2;fragrance:1,2,3;aroma:1,2,3"


def test_pump_map_from_install_sh_value() -> None:
    """구 설치 각인값/override 형식 그대로 → 유효 addr 전부 매핑(빈 pump_map = 토출 0 회귀 방지).

    식향 2펌프(addr 1,2)·향장향 3펌프(addr 1,2,3) — 2026-07-17 확정.
    """
    m = pump_map_from_addresses_env(_INSTALL_SH_PUMP_ADDRESSES)
    assert set(m.keys()) == {1, 2, 3}, "서버가 쓰는 pumpAddr ⊆ pi pump_map 이어야 drop 없음"
    for addr, spec in m.items():
        assert spec.pump_full_stroke == 12000, "sy01b 프리셋 스트로크"
        assert spec.syringe_capacity_ml == 0.5, "양 모드 공통 기본 용량(2026-07-17 확정)"
        assert spec.max_volume_ul == 500


def test_pump_map_never_maps_broadcast_addr_0() -> None:
    """⚠️ addr 0 = RS485 브로드캐스트 — 표준 구성값이 0 을 기기주소로 쓰지 않는다."""
    assert 0 not in pump_map_from_addresses_env(_INSTALL_SH_PUMP_ADDRESSES)


def test_pump_map_flavor_default_capacity_is_05ml() -> None:
    """식향 기본 용량 회귀 고정 — 1.25mL 로 되돌아가면 과흡입이 게이트를 통과한다(F9)."""
    m = pump_map_from_addresses_env("flavor:1,2")
    assert set(m.keys()) == {1, 2}
    assert all(s.syringe_capacity_ml == 0.5 for s in m.values())


def test_pump_map_empty_env_is_empty_map() -> None:
    """미설정 → 빈 매핑(= 전 스텝 drop·안전측). 실기기는 버스 스캔(②)이 채우고, 스캔까지
    실패하면 펌프 응답 감지 → 자동 재기동 재스캔(R8 P1-1)이 복구한다."""
    assert pump_map_from_addresses_env(None) == {}
    assert pump_map_from_addresses_env("") == {}


class TestPerEnvIdentityPath:
    """환경별 정체성 파일(2026-07-23) — 서버 URL 로 파일명을 파생해 서버마다 신분증을 따로 둔다."""

    def test_server_slug_from_url(self) -> None:
        assert _server_slug("https://dev-env.senlyt.com") == "dev-env.senlyt.com"
        assert _server_slug("https://v1-2-0.env.senlyt.com") == "v1-2-0.env.senlyt.com"
        assert _server_slug("https://senlyt.com/") == "senlyt.com"
        assert _server_slug("http://localhost:8080") == "localhost_8080"  # 포트 `:` → `_`
        assert _server_slug("dev-env.senlyt.com") == "dev-env.senlyt.com"  # 스킴 없어도
        assert _server_slug("") == "default"

    def test_identity_path_per_server(self) -> None:
        """server_base_url 다르면 파일 경로도 다르다(= 각 서버 신분증 분리)."""
        env = {"SENLYT_STATE_DIR": "/var/lib/senlyt"}
        dev = _identity_path(env, "https://dev-env.senlyt.com")
        v120 = _identity_path(env, "https://v1-2-0.env.senlyt.com")
        prod = _identity_path(env, "https://senlyt.com")
        assert str(dev) == "/var/lib/senlyt/identities/dev-env.senlyt.com.json"
        assert str(v120) == "/var/lib/senlyt/identities/v1-2-0.env.senlyt.com.json"
        assert str(prod) == "/var/lib/senlyt/identities/senlyt.com.json"
        assert dev != v120 != prod  # 서버별 분리 = 전환해도 서로 안 덮음.

    def test_explicit_path_overrides(self) -> None:
        """SENLYT_IDENTITY_PATH 명시 시 그 단일 파일로 고정(하위호환 override — 서버 파생 무시)."""
        env = {"SENLYT_IDENTITY_PATH": "/custom/id.json", "SENLYT_STATE_DIR": "/var/lib/senlyt"}
        assert str(_identity_path(env, "https://dev-env.senlyt.com")) == "/custom/id.json"

    def test_no_server_falls_back_to_single_file(self) -> None:
        """서버 미상(구 폴백) → 단일 device-identity.json."""
        env = {"SENLYT_STATE_DIR": "/var/lib/senlyt"}
        assert str(_identity_path(env)) == "/var/lib/senlyt/device-identity.json"

    def test_state_dir_precedence(self) -> None:
        """base = SENLYT_STATE_DIR > LOG_DIR > cwd."""
        env = {"SENLYT_STATE_DIR": "/var/lib/senlyt", "LOG_DIR": "/var/log/senlyt"}
        assert _identity_path(env, "https://senlyt.com").parent == Path(
            "/var/lib/senlyt/identities"
        )
        env2 = {"LOG_DIR": "/var/log/senlyt"}  # state 없으면 log_dir
        assert _identity_path(env2, "https://senlyt.com").parent == Path(
            "/var/log/senlyt/identities"
        )
