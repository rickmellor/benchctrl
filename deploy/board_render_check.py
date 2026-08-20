"""Render the real panel against the LIVE agent and show what a human would see.

HTTP 200 from Streamlit proves the server is up, not that the panel says
anything true -- Streamlit pushes content over a websocket. So drive the real
render() with a recorder, using a snapshot from the live agent.
"""
import json
import time

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.dashboards.panel import COLOURS, render

tok = json.load(open("/etc/benchctrl/agent.json"))["token"]
ep = EndpointConfig(host="127.0.0.1", port=9737, token=tok)


class Rec:
    def __init__(self):
        self.calls = []
        self.depth = 0

    def _r(self, kind, text):
        self.calls.append((kind, str(text), self.depth))

    def markdown(self, t, **k):
        self._r("markdown", t)

    def warning(self, t, **k):
        self._r("warning", t)

    def error(self, t, **k):
        self._r("error", t)

    def write(self, t, **k):
        self._r("write", t)

    def caption(self, t, **k):
        self._r("caption", t)

    def subheader(self, t, **k):
        self._r("subheader", t)

    def expander(self, label, **k):
        self._r("expander", label)
        rec = self

        class E:
            def __enter__(self_):
                rec.depth += 1
                return rec

            def __exit__(self_, *a):
                rec.depth -= 1
        return E()


feed = AgentFeed(ep, poll_s=1.0).start()
for _ in range(12):
    time.sleep(1)
    snap = feed.snapshot()
    if snap["trustworthy"]:
        break
feed.stop()

print("live snapshot:", json.dumps({k: snap[k] for k in
      ("headline", "severity", "trustworthy", "armed", "stale_reason",
       "dropped_events")}))
print()

rec = Rec()
render(rec, snap)

print("WHAT THE SCREEN SHOWS")
print("-" * 62)
for kind, text, depth in rec.calls:
    body = text if len(text) < 200 else text[:200] + "..."
    if kind == "markdown":
        # pull the headline and colour out of the banner html
        import re
        m = re.search(r">([A-Z ]+)</span>", text)
        c = re.search(r"background:(#[0-9a-f]{6})", text)
        if m:
            print("  BANNER   %-9s colour=%s  size=%s"
                  % (m.group(1), c.group(1) if c else "?",
                     "5rem" if "font-size:5rem" in text else "??"))
            continue
    print("  %-9s%s%s" % (kind.upper(), "  " * depth, body.replace("\n", " ")))
print("-" * 62)

# The honesty assertions, on the real render of real data.
kinds = [k for k, _, _ in rec.calls]
banner = [t for k, t, _ in rec.calls if k == "markdown"][0]
ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not cond:
        ok = False


print()
check("the headline is the biggest thing on screen", "font-size:5rem" in banner)
check("the headline text matches the model", snap["headline"] in banner,
      snap["headline"])
check("the banner colour matches the severity",
      COLOURS[snap["severity"]] in banner, snap["severity"])
if snap["stale_reason"]:
    check("a stale view warns outside the collapsed expander",
          "warning" in kinds and kinds.index("warning") < kinds.index("expander"))
else:
    check("a trustworthy view draws no warning", "warning" not in kinds)
check("armed devices, if any, are shown as an error banner",
      bool(snap["armed"]) == any(k == "error" for k in kinds),
      "armed=%s" % snap["armed"])
check("the instruments section is always present", "Instruments" in
      [t for k, t, _ in rec.calls if k == "subheader"])
# Feed health must be the LAST thing drawn and its content must be nested
# inside the expander (depth > 0), so diagnostics never outrank bench state.
health = [(i, d) for i, (k, _, d) in enumerate(rec.calls) if k == "expander"]
check("feed health is drawn last, below everything about the bench",
      bool(health) and health[0][0] == len(kinds) - 2,
      "expander at %s of %d" % (health[0][0] if health else "?", len(kinds)))
check("feed health content is nested inside the expander, not loose",
      all(d > 0 for _, _, d in rec.calls[health[0][0] + 1:]) if health else False,
      "depths=%s" % [d for _, _, d in rec.calls[health[0][0] + 1:]] if health else "")

print()
print("RESULT:", "PANEL RENDERS HONESTLY" if ok else "PANEL RENDER PROBLEM")
raise SystemExit(0 if ok else 1)
