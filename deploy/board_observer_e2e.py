"""End-to-end observer + dashboard check on the board, on a spare port.

Runs its OWN agent with a SIMULATED Arc on port 9738 so the production agent on
9737 is untouched. Nothing here can energise real hardware: the registry only
contains a simulated device.

Proves, against the code actually installed on the board:
  1. the agent grants an observer session and says so in the welcome
  2. an observer is refused every mutating verb
  3. an observer polling flat out does NOT keep an armed bench alive
  4. AgentFeed reaches a trustworthy headline and tracks arm state
"""
import json
import sys
import threading
import time

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import OBSERVER_METHODS, AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.net.client import RemoteClient
from benchctrl.net.errors import PolicyError
from benchctrl.sim import SimulatedOtiiArc

TOKEN = "board-observer-check"
PORT = 9738

# register_open, not register: Governor.trip() drives the *open device object*
# to its safe state, so a bare factory leaves `devices` empty and every trip
# outcome is FAILED with output_armed still set. Mirrors tests/conftest usage.
sim = SimulatedOtiiArc()
sim.start()
smu = OtiiArc.open(sim.port)

registry = DeviceRegistry()
registry.register_open("otii_arc", smu)

agent = BenchAgent(registry, token=TOKEN, deadman_s=1.0, heartbeat_s=0.3)
server = AgentServer(agent, host="127.0.0.1", port=PORT)
server.start()
ep = EndpointConfig(host="127.0.0.1", port=server.port, token=TOKEN)
print("agent up on port", server.port)

fails = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not ok:
        fails.append(label)


# --- 1. the welcome echoes the role ------------------------------------
print("\n[1] observer session is granted and announced")
obs = RemoteClient(ep, observer=True).connect()
check("welcome.observer is True", obs.welcome.get("observer") is True,
      repr(obs.welcome.get("observer")))
norm = RemoteClient(ep).connect()
check("a normal client is not marked observer",
      norm.welcome.get("observer") in (False, None),
      repr(norm.welcome.get("observer")))

# --- 2. every mutating verb is refused --------------------------------
print("\n[2] mutating verbs are refused to an observer")
for method, params in [
    ("agent.open", {"device": "otii_arc", "open": {}}),
    ("agent.claim", {"device": "otii_arc"}),
    ("device.call", {"device": "otii_arc", "method": "arm_output", "args": {}}),
    ("run.abort", {"run_id": "x"}),
    ("agent.totally.made.up", {}),
]:
    try:
        obs.call(method, params)
        check(method + " refused", False, "it was ALLOWED")
    except PolicyError as exc:
        check(method + " refused", True, str(exc)[:50])
    except Exception as exc:  # noqa: BLE001
        check(method + " refused", False, type(exc).__name__ + ": " + str(exc)[:50])

check("allowlist has no mutating verb",
      not any(m.split(".")[-1] in {"open", "claim", "call", "abort", "close", "release"}
              for m in OBSERVER_METHODS),
      str(sorted(OBSERVER_METHODS)))

# --- 3. the hazard: polling must not starve the deadman ---------------
print("\n[3] an observer polling does not keep an armed bench alive")
agent.governor.state_for("otii_arc").output_armed = True
check("bench is armed to begin with", agent.governor.any_armed)


def tripped():
    return any(t["reason"] == "heartbeat_lost" for t in agent.governor.status()["trips"])


polls = 0
deadline = time.monotonic() + agent.deadman_s * 8
hit = False
while time.monotonic() < deadline:
    obs.status()
    polls += 1
    if tripped():
        hit = True
        break
    time.sleep(0.05)
check("enough polls to prove anything", polls > 10, "polls=%d" % polls)
check("the deadman tripped despite the polling", hit, "polls=%d" % polls)
check("the trip actually disarmed the device", not agent.governor.any_armed)
obs.close()
norm.close()

# --- 4. the feed itself ------------------------------------------------
print("\n[4] AgentFeed reaches a trustworthy view")
feed = AgentFeed(ep, poll_s=0.5).start()
snap = {}
for _ in range(20):
    time.sleep(0.5)
    snap = feed.snapshot()
    if snap["trustworthy"]:
        break
print("     snapshot:", json.dumps(
    {k: snap.get(k) for k in ("headline", "severity", "trustworthy", "armed",
                              "stale_reason", "dropped_events", "reconnects")}))
check("feed became trustworthy", snap.get("trustworthy") is True,
      str(snap.get("stale_reason")))
check("feed shows a real headline", snap.get("headline") in ("IDLE", "ARMED", "RECORDING"),
      str(snap.get("headline")))
check("feed dropped no events", snap.get("dropped_events") == 0)

# Arm again and confirm the feed notices. This needs a NORMAL client connected
# for the duration: an observer does not feed the deadman (that is phase 3), so
# with only the panel attached the bench correctly disarms itself within
# deadman_s and there is no ARMED state left to observe. The operator client is
# what makes ARMED a legitimate steady state at all.
# ARMED needs its OWN agent with a realistic deadman. RemoteClient's heartbeat
# loop floors its interval at 1.0s (client.py:289), deliberately -- so no
# client can hold open the 1.0s deadman that phase 3 needs in order to observe
# a trip quickly. Trying to serve both from one agent is what made this check a
# coin flip. 10s/2s is the ratio a real bench uses.
sim4 = SimulatedOtiiArc()
sim4.start()
reg4 = DeviceRegistry()
reg4.register_open("otii_arc", OtiiArc.open(sim4.port))
agent4 = BenchAgent(reg4, token=TOKEN, deadman_s=10.0, heartbeat_s=2.0)
server4 = AgentServer(agent4, host="127.0.0.1", port=9742).start()
ep4 = EndpointConfig(host="127.0.0.1", port=server4.port, token=TOKEN,
                     heartbeat_s=1.0, deadman_s=10.0)

feed.stop()
feed = AgentFeed(ep4, poll_s=0.5).start()
for _ in range(20):
    time.sleep(0.5)
    if feed.snapshot()["trustworthy"]:
        break

operator = RemoteClient(ep4).connect()
agent4.governor.state_for("otii_arc").output_armed = True
seen_armed = False
for _ in range(16):
    time.sleep(0.25)
    if feed.snapshot()["headline"] == "ARMED":
        seen_armed = True
        break
check("feed reports ARMED when the bench arms", seen_armed,
      "headline=%s armed=%s" % (feed.snapshot()["headline"],
                                feed.snapshot()["armed"]))
check("the feed names the armed device",
      feed.snapshot()["armed"] == ["otii_arc"], str(feed.snapshot()["armed"]))

# It must STAY armed for as long as the operator keeps heartbeating -- a single
# flicker of ARMED would also satisfy the check above, and would not prove the
# panel tracks a steady state rather than catching one frame of a transient.
held = True
for _ in range(16):
    time.sleep(0.25)
    if feed.snapshot()["headline"] != "ARMED":
        held = False
        break
check("ARMED is held while the operator heartbeats, not a flicker", held,
      "headline=%s" % feed.snapshot()["headline"])

# Drop the operator: the bench must disarm itself, and the panel must follow
# back to IDLE rather than leaving a scary frame up forever. deadman_s is 10s
# here, so allow for it plus the grace period.
operator.close()
back_to_idle = False
for _ in range(120):
    time.sleep(0.25)
    if feed.snapshot()["headline"] == "IDLE":
        back_to_idle = True
        break
check("panel follows the bench back to IDLE after the deadman disarms it",
      back_to_idle, str(feed.snapshot()["headline"]))

feed.stop()
server4.stop()
sim4.close()
server.stop()
sim.close()

# --- 5. the agent going away, for real ---------------------------------
# server.stop() is NOT this test: ThreadingTCPServer.shutdown() only stops
# accepting new connections, so an already-established session keeps being
# served by its handler thread and the panel is correctly still connected.
# The honest version of "the agent went away" is the process dying, which is
# what every `systemctl restart benchctrl-agent` does. So run one in a
# subprocess and kill it.
print("\n[5] the agent process dying and coming back")
AGENT_SRC = """
import sys
from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.sim import SimulatedOtiiArc
sim = SimulatedOtiiArc(); sim.start()
reg = DeviceRegistry(); reg.register_open("otii_arc", OtiiArc.open(sim.port))
a = BenchAgent(reg, token=sys.argv[1], deadman_s=30.0, heartbeat_s=1.0)
s = AgentServer(a, host="127.0.0.1", port=int(sys.argv[2])).start()
print("READY", flush=True)
import time
while True: time.sleep(1)
"""

import os
import subprocess

SUB_PORT = 9739
env = dict(os.environ, PYTHONPATH="/home/arduino/benchctrl-1.2.0/src",
           PYTHONUNBUFFERED="1")


def spawn():
    proc = subprocess.Popen(
        [sys.executable, "-c", AGENT_SRC, TOKEN, str(SUB_PORT)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for _ in range(200):
        line = proc.stdout.readline()
        if line.startswith("READY"):
            return proc
        if proc.poll() is not None:
            raise SystemExit("subprocess agent died before READY")
    raise SystemExit("subprocess agent never became READY")


proc = spawn()
sub_ep = EndpointConfig(host="127.0.0.1", port=SUB_PORT, token=TOKEN)
feed2 = AgentFeed(sub_ep, poll_s=0.5).start()

ok = False
for _ in range(20):
    time.sleep(0.5)
    if feed2.snapshot()["trustworthy"]:
        ok = True
        break
check("panel is trustworthy against the subprocess agent", ok,
      str(feed2.snapshot()["stale_reason"]))

# SIGKILL, not SIGTERM: this is the ugly case, no orderly close frame.
proc.kill()
proc.wait(timeout=10)
said = False
for _ in range(30):
    time.sleep(0.5)
    if feed2.snapshot()["headline"] == "NO AGENT":
        said = True
        break
snap2 = feed2.snapshot()
check("panel says NO AGENT when the agent process is killed", said,
      "headline=%s" % snap2["headline"])
check("and gives a real reason rather than a blank",
      bool(snap2["stale_reason"]), str(snap2["stale_reason"])[:70])
check("and stops claiming to be trustworthy", snap2["trustworthy"] is False)

# Back it comes: the panel must rejoin on its own. This is the actual boot
# order on the board -- lightdm and the panel start before the agent is up.
proc = spawn()
rejoined = False
for _ in range(40):
    time.sleep(0.5)
    if feed2.snapshot()["trustworthy"]:
        rejoined = True
        break
check("panel reconnects on its own when the agent returns", rejoined,
      "headline=%s stale=%s" % (feed2.snapshot()["headline"],
                                feed2.snapshot()["stale_reason"]))
check("and recorded the reconnect", feed2.snapshot()["reconnects"] >= 1,
      "reconnects=%d" % feed2.snapshot()["reconnects"])

feed2.stop()
proc.kill()
proc.wait(timeout=10)

alive = [t.name for t in threading.enumerate() if "dashboard-feed" in t.name]
check("feed thread did not outlive stop()", not alive, str(alive))

print("\n" + "=" * 58)
print("  RESULT: %s" % ("ALL CHECKS PASSED" if not fails else "FAILED: %s" % fails))
print("=" * 58)
raise SystemExit(1 if fails else 0)
