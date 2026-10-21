"""하트비트 `pumpFingerprints`(2026-09-11) — pi 는 관측·보고만, 판정은 서버.

와이어 계약: 관측값이 있으면 {addr(str): 데이터블록} 으로 방출, 없으면 키 미방출(includeIfNull:false·부록A P-4).
"""
from senlyt_pi.core.wire_messages import Heartbeat


def test_heartbeat_emits_pump_fingerprints_when_observed():
    hb = Heartbeat(device_id="d1", queue_depth=0, pump_fingerprints={1: "8.33", 2: "30064809 C"})
    m = hb.to_json()
    assert m["pumpFingerprints"] == {"1": "8.33", "2": "30064809 C"}


def test_heartbeat_omits_pump_fingerprints_when_absent_or_empty():
    assert "pumpFingerprints" not in Heartbeat(device_id="d1", queue_depth=0).to_json()
    assert "pumpFingerprints" not in Heartbeat(device_id="d1", queue_depth=0, pump_fingerprints={}).to_json()
