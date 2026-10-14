"""The vision sidecar's deploy tree: does it still say what the docs say?

The sidecar runs as a privileged container from a systemd unit, driven by an
env file. None of that is exercised by the Python suite, and each piece has a
way to be wrong that only shows on the bench: a script with a syntax error, a
unit that leaves a stale container blocking the port, a Dockerfile that quietly
became x86-only, a run script that publishes the unauthenticated sidecar on
the LAN. These tests pin those, the way ``test_deploy_board_sync.py`` pins the
sync scripts: ``sh -n`` for syntax, and the text for the contracts.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

import pytest

VISION = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "vision"

SCRIPTS = (
    "build.sh",
    "run-vision.sh",
    "install-vision.sh",
    "install-metis-driver.sh",
    "fetch-model.sh",
    "fetch-classifier.sh",
)

#: sha256 of Axelera's own 72-axelera.rules as shipped in metis-dkms_1.6.2_all.deb.
#: The copy in deploy/vision/udev/ is a reference; the .deb installs the real one.
RULES_SHA256 = hashlib.sha256((VISION / "udev" / "72-axelera.rules").read_bytes()).hexdigest()


def _code(name: str) -> str:
    text = (VISION / name).read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_passes_shell_syntax_check(name):
    out = subprocess.run(
        ["sh", "-n", str(VISION / name)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"{name}: {out.stderr}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_is_posix_sh_not_bash(name):
    """The Pi and the Uno Q both have /bin/sh; not every bench box has bash."""
    first = (VISION / name).read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh", f"{name}: {first}"


def test_the_dockerfile_has_no_architecture_specific_lines():
    """One Dockerfile builds on the Pi 5 (arm64) and a desktop (amd64)."""
    text = (VISION / "Dockerfile").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for word in ("amd64", "x86_64", "arm64", "aarch64", "--platform"):
        assert word not in code, f"Dockerfile names an architecture: {word}"


def test_the_dockerfile_does_not_copy_benchctrl_in():
    """The checkout is bind-mounted, so a git pull is a restart, not a rebuild."""
    code = "\n".join(
        ln
        for ln in (VISION / "Dockerfile").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert not re.search(r"^\s*(COPY|ADD)\b", code, re.M), "the Dockerfile copies files in"
    assert "PYTHONPATH=/opt/benchctrl/src" in code


def test_the_run_script_publishes_the_port_on_loopback_only():
    """The sidecar has no authentication; the agent is the network face."""
    code = _code("run-vision.sh")
    assert '-p "127.0.0.1:$PORT:$PORT"' in code
    assert re.search(r'-p\s+"?\$PORT:', code) is None, "port published on all interfaces"


def test_the_run_script_mounts_the_checkout_read_only():
    code = _code("run-vision.sh")
    assert '"$SRC_DIR:/opt/benchctrl/src:ro"' in code
    assert '"$MODEL_DIR:/models:ro"' in code


def test_the_run_script_removes_a_stale_container_first():
    """A container left behind by a crash would otherwise hold the name and the port."""
    code = _code("run-vision.sh")
    assert 'docker rm -f "$NAME"' in code


def test_the_run_script_falls_back_to_camera_only_without_a_model():
    code = _code("run-vision.sh")
    assert "--no-aipu" in code
    assert 'if [ "$NO_AIPU" = "1" ]' in code


def test_the_run_script_serves_only_the_classifiers_that_are_there():
    """CLASSIFIERS names directories under MODEL_DIR; a missing one is skipped
    with a pointer, never passed through, and none are passed without an AIPU."""
    code = _code("run-vision.sh")
    assert "for c in $CLASSIFIERS; do" in code
    assert 'if [ -f "$MODEL_DIR/$c/model.json" ]' in code
    assert "--classifier /models/$c" in code
    assert "fetch-classifier.sh" in code
    assert code.index('if [ "$NO_AIPU" != "1" ]') < code.index("for c in $CLASSIFIERS")
    assert "$model_args $classifier_args" in code


def test_the_classifier_fetch_refuses_a_directory_that_is_not_a_model():
    code = _code("fetch-classifier.sh")
    for f in ("model.json", "classes.json"):
        assert f in code
    assert "SHA256SUMS" in code and "MANIFEST" in code


def test_the_unit_clears_a_stale_container_and_requires_docker():
    unit = (VISION / "systemd" / "benchctrl-vision.service").read_text(encoding="utf-8")
    assert "Requires=docker.service" in unit
    assert "ExecStartPre=-/usr/bin/docker rm -f benchctrl-vision" in unit
    assert "ExecStart=/usr/local/bin/benchctrl-vision-run --foreground" in unit
    assert "Restart=on-failure" in unit
    assert "StartLimitBurst=" in unit


def test_every_env_key_the_run_script_reads_is_in_the_example():
    """A knob the script honours but the example omits is undiscoverable on the box."""
    code = _code("run-vision.sh")
    read = set(re.findall(r"^([A-Z_]+)=\$\{\1:-", code, re.M))
    example = (VISION / "vision.env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    missing = read - documented - {"ENV_FILE"}
    assert not missing, f"run-vision.sh reads {sorted(missing)} but vision.env.example lacks them"


def test_the_installer_keeps_an_existing_env_file():
    code = _code("install-vision.sh")
    assert "keeping existing" in code
    assert "systemctl daemon-reload" in code


def test_the_driver_installer_pins_the_package_checksum():
    """The .deb is fetched from the vendor or copied by hand; either way its
    identity is checked before dpkg touches the kernel."""
    code = _code("install-metis-driver.sh")
    assert re.search(r"DEB_SHA256=[0-9a-f]{64}", code)
    assert "sha256sum" in code and "mismatch" in code
    assert "dpkg -i" in code


def test_the_driver_installer_finds_the_card_by_vendor_id_not_bus_address():
    """The Metis moved between 0001:03 and 0001:04 when the HAT slot changed.
    Anything keyed on the address would silently check the wrong device."""
    code = _code("install-metis-driver.sh")
    assert "1f9d:1100" in code or "0x1f9d" in code
    assert not re.search(r"0001:0[0-9]:00\.0", code), "a fixed bus address is hardcoded"


def test_the_udev_rule_is_axeleras_own():
    """A reference copy of the rule metis-dkms installs. If Axelera ships a new
    one the .deb wins and this copy must be refreshed — hence the pin."""
    assert RULES_SHA256 == "965a870995e75f30b0defbe7c4f489d41ffcf55b82d7a7ed8d78446fe89a875b"
    rule = (VISION / "udev" / "72-axelera.rules").read_text(encoding="utf-8")
    assert 'GROUP="axelera"' in rule and "dma_heap" in rule


def test_only_the_view_port_may_be_published_on_the_lan():
    """The control port stays on 127.0.0.1; the LAN publish is the view port,
    and only when VIEW_PORT is set. A ``-p "$PORT:$PORT"`` form would be the
    unauthenticated control surface on the network."""
    code = _code("run-vision.sh")
    assert 'view_publish="-p $VIEW_PORT:$VIEW_PORT"' in code
    assert "--view-port $VIEW_PORT" in code
    assert 'if [ "$VIEW_PORT" != "0" ]' in code
    lan_publishes = re.findall(r'-p\s+"?\$([A-Z_]+):', code)
    assert lan_publishes == ["VIEW_PORT"], lan_publishes


def test_the_rescan_runs_once_per_boot_before_the_sidecar_and_only_when_needed():
    """The Metis resets its BARs a few seconds after power-on, after a Pi has
    already enumerated it; a remove+rescan re-assigns them. It must be
    idempotent (a healthy card is left alone), verified by the driver's own
    log rather than assumed, and ordered before the sidecar that needs it."""
    code = _code("metis-rescan.sh")
    assert "vmsi configured" in code and "vmsi (configured|not available)" in code
    assert "/sys/bus/pci/rescan" in code and "/remove" in code
    assert "1f9d:1100" in code
    assert re.search(r"if healthy .*; then\s*\n\s*echo .*nothing to do", code)
    unit = (VISION / "systemd" / "benchctrl-metis-rescan.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and "Before=benchctrl-vision.service" in unit
    assert "ExecStart=/usr/local/bin/benchctrl-metis-rescan" in unit
    vision = (VISION / "systemd" / "benchctrl-vision.service").read_text(encoding="utf-8")
    assert "benchctrl-metis-rescan.service" in vision
    installer = _code("install-vision.sh")
    assert (
        "benchctrl-metis-rescan" in installer
        and "systemctl enable benchctrl-metis-rescan.service" in installer
    )


def test_the_rescan_aligns_the_payload_size_on_both_ends_every_time():
    """The Pi 5 root port defaults to a 512-byte Max Payload Size; the Metis runs
    at 128. Left unequal, every completion the root port returns is malformed
    to the card and its firmware DMA never lands. Aligned on the healthy path
    and after a rescan alike, because re-enumeration re-derives it."""
    code = _code("metis-rescan.sh")
    assert code.count("align_payload") >= 3, "align on the healthy path and after a rescan"
    assert "CAP_EXP+0x8.w" in code and "0x70e0" in code
    assert "CAP_EXP+0xa.w=000f" in code, "clear the error bits the mismatch left behind"


def test_the_driver_installer_pins_a_single_msi_vector():
    code = _code("install-metis-driver.sh")
    assert "options metis single_msi=1" in code
    assert "/etc/modprobe.d/metis.conf" in code


def test_the_host_led_rule_is_installed_only_where_the_host_has_one():
    code = _code("install-vision.sh")
    assert "70-benchctrl-host-leds.rules" in code
    assert "/sys/class/leds/ACT" in code.split("70-benchctrl-host-leds.rules")[0]
    rule = (VISION / "udev" / "70-benchctrl-host-leds.rules").read_text(encoding="utf-8")
    assert 'SUBSYSTEM=="leds"' in rule and "ACT|PWR" in rule and "chmod g+w" in rule


def test_the_env_example_says_how_to_list_several_classifiers():
    """A space-separated CLASSIFIERS value must be quoted or `sh` runs the second
    name as a command (bench-found: the unit crash-looped on `sdg-out1: not found`)."""
    example = (VISION / "vision.env.example").read_text(encoding="utf-8")
    assert 'CLASSIFIERS="led sdg-out1"' in example
