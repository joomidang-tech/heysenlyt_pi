"""REGBR-08(2026-10-15) — install.sh 는 등록할 지점(SENLYT_BRANCH_ID)을 필수로 받아 device.env 에 각인한다.

지점(매장)이 없으면 서버가 등록을 400 branch_required 로 거절하므로(서버 05 §6.9 머리), 설치 단계에서 먼저 멈춘다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent / "install.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # 서버 URL 인자는 주고 root 가 아니므로, 지점 검사를 통과하면 root 검사에서 멈춘다(실제 설치는 하지 않는다)
    return subprocess.run(
        ["bash", str(INSTALL), "https://aws.dev-env.senlyt.com"],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_missing_branch_stops_install() -> None:
    r = _run({})
    assert r.returncode != 0
    assert "SENLYT_BRANCH_ID" in r.stderr
    assert "기기 등록" in r.stderr


def test_bad_branch_format_stops_install() -> None:
    r = _run({"SENLYT_BRANCH_ID": "a;rm"})
    assert r.returncode != 0
    assert "지점 id 형식" in r.stderr


def test_branch_given_passes_branch_check() -> None:
    r = _run({"SENLYT_BRANCH_ID": "seongsu"})
    assert "SENLYT_BRANCH_ID" not in r.stderr  # 지점 검사는 통과 — 다음 검사(root)에서 멈춘다


def test_branch_is_written_and_managed() -> None:
    s = INSTALL.read_text(encoding="utf-8")
    assert "SENLYT_BRANCH_ID=$BRANCH_ID" in s
    managed = next(line for line in s.splitlines() if line.startswith("_MANAGED_KEYS="))
    assert "SENLYT_BRANCH_ID" in managed


# ── REGBR-10(r35 V35-9) — 등록 실패 이유 보존 · 지점 비활성은 재시도 ─────────────────────────────


def test_regbr10_branch_required_code_kept_no_retry():
    from senlyt_pi.adapters.registration_client import RegistrationClient, RegistrationError

    calls = []

    def transport(req):
        calls.append(req)
        return 400, {"code": "branch_required", "message": "x"}

    c = RegistrationClient(transport, device_id="s1", branch_id=None, sleep=lambda _s: None)
    try:
        c.register()
        raise AssertionError("expected RegistrationError")
    except RegistrationError as e:
        assert e.code == "branch_required"
        assert e.retryable is False
    assert len(calls) == 1


def test_regbr10_branch_not_active_retries_then_recovers():
    from senlyt_pi.adapters.registration_client import RegistrationClient

    seq = [(409, {"code": "branch_not_active"}), (409, {"code": "branch_not_active"}), (202, {})]

    def transport(_req):
        return seq.pop(0)

    c = RegistrationClient(transport, device_id="s1", branch_id="seongsu", sleep=lambda _s: None)
    assert c.register() is None  # 202 pending — 지점이 다시 켜지면 스스로 회복
    assert seq == []


def test_regbr10_4xx_without_code_stays_invalid_request():
    from senlyt_pi.adapters.registration_client import RegistrationClient, RegistrationError

    c = RegistrationClient(lambda _r: (400, None), device_id="s1", sleep=lambda _s: None)
    try:
        c.register()
        raise AssertionError("expected RegistrationError")
    except RegistrationError as e:
        assert e.code == "invalid_request"
