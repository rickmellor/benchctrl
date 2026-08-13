"""Discovery beacon: payload shape, matching, and what it must not leak."""

from __future__ import annotations

import json
import socket

import pytest

from benchctrl.net import beacon
from benchctrl.net.auth import token_fingerprint

TOKEN = "beacon-test-token"


def test_payload_round_trips():
    data = beacon.build_payload(
        host="Benchctrl", port=9737, token=TOKEN, agent_version="1.1.0", device_count=2
    )
    record = beacon.parse_payload(data, "10.0.0.7")
    assert record is not None
    assert record.host == "Benchctrl"
    assert record.port == 9737
    assert record.address == "10.0.0.7"
    assert record.device_count == 2
    assert record.endpoint == "10.0.0.7:9737"


def test_beacon_never_carries_the_token():
    """It is broadcast to the whole subnet. It must not be a credential."""
    data = beacon.build_payload(host="h", token=TOKEN)
    assert TOKEN not in data.decode()
    assert json.loads(data)["fp"] == token_fingerprint(TOKEN)


def test_beacon_carries_no_inventory():
    """Model names and serials would tell a listener what is in the room."""
    payload = json.loads(beacon.build_payload(host="h", token=TOKEN, device_count=3))
    assert set(payload) == {"svc", "v", "host", "port", "agent", "fp", "ndev"}
    assert payload["ndev"] == 3


def test_fingerprint_identifies_my_bench():
    record = beacon.parse_payload(
        beacon.build_payload(host="h", token=TOKEN), "127.0.0.1"
    )
    assert record.matches_token(TOKEN)
    assert not record.matches_token("someone-elses-token")
    assert not record.matches_token(None)


def test_foreign_traffic_is_ignored():
    assert beacon.parse_payload(b"not json at all", "1.2.3.4") is None
    assert beacon.parse_payload(b'{"svc":"other"}', "1.2.3.4") is None
    assert beacon.parse_payload(json.dumps({"svc": "benchctrl", "v": 99}).encode(), "1.2.3.4") is None
    assert beacon.parse_payload(b"\xff\xfe\x00binary", "1.2.3.4") is None


def test_malformed_port_is_rejected():
    payload = json.dumps(
        {"svc": "benchctrl", "v": 1, "host": "h", "port": "not-a-port"}
    ).encode()
    assert beacon.parse_payload(payload, "1.2.3.4") is None


def test_transmit_and_listen_over_loopback():
    """The actual mechanism, over a real UDP socket."""
    port = _free_udp_port()
    tx = beacon.BeaconTransmitter(
        host="testbench",
        port=9737,
        token=TOKEN,
        agent_version="1.1.0",
        device_count_fn=lambda: 1,
        beacon_port=port,
        interval_s=0.1,
    )
    tx.start()
    try:
        found = beacon.listen(timeout=2.0, beacon_port=port, stop_after=1)
    finally:
        tx.stop()

    assert found, "no beacon received"
    assert found[0].host == "testbench"
    assert found[0].device_count == 1
    assert found[0].matches_token(TOKEN)


def test_listen_filters_by_token():
    port = _free_udp_port()
    tx = beacon.BeaconTransmitter(
        host="someone-elses-bench",
        token="a-different-token",
        beacon_port=port,
        interval_s=0.1,
    )
    tx.start()
    try:
        assert beacon.listen(timeout=1.0, beacon_port=port, token=TOKEN) == []
    finally:
        tx.stop()


def test_avahi_service_file_is_wellformed():
    """Static XML in /etc/avahi/services is how the board already does it."""
    import xml.etree.ElementTree as ET

    xml = beacon.avahi_service_xml(port=9737, agent_version="1.1.0", fingerprint="abcd1234")
    root = ET.fromstring(xml[xml.index("<service-group>") :])
    assert root.tag == "service-group"
    service = root.find("service")
    assert service.find("type").text == "_benchctrl._tcp"
    assert service.find("port").text == "9737"
    txt = [t.text for t in service.findall("txt-record")]
    assert "fp=abcd1234" in txt
    assert not any("token" in t for t in txt)


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
