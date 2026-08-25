/* BENCH CONTROL CORE — the paint loop.
 *
 * Two clocks, on purpose, and they are independent:
 *
 *   - Truth arrives on a fetch every POLL_MS, on a timer of its own.
 *   - Decoration animates on a self-governing frame loop that may run as slow as
 *     it likes.
 *
 * They are separate so that throttling the animation NEVER makes the readouts
 * stale. A board too busy to draw a hologram must still update the numbers on
 * schedule; the frame rate is allowed to collapse, the data rate is not.
 *
 * The invariant the renderer must not break: a readout shows a real measurement
 * or it shows NO LINK. When a fetch fails, the page does not keep painting the
 * last good frame as though it were current — it raises the curtain (#dead) and
 * says every number on screen is of unknown age. A cinematic display that holds
 * a stale 12.01 V is worse than a blank one, because it looks authoritative.
 */

'use strict';

const POLL_MS = 500;

/* How many consecutive failed fetches before the curtain drops. Two, not one:
 * a single missed poll on a loaded board is ordinary jitter, and a display that
 * flickers "OFFLINE" every few minutes teaches the operator to disregard it. */
const DEAD_AFTER = 2;

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------- the frame governor */

/* Measured on the target board (Uno Q, 4 cores, 1440x900, GPU rasterisation on):
 * an unthrottled loop cost ~138% of one core — about 35% of the whole machine —
 * for a status display.
 *
 * Almost all of that turned out to be one CSS element rather than the artwork:
 * a full-viewport fixed scanline, at 48% of a core on its own (the measurements
 * are in #scan in fui.css). Stubbing out the hologram, the traces, the glow and
 * the grid *together* moved the board only 176% -> 147%. So the tiers below are
 * a backstop for a board under load, not the main fix — and they are tuned to
 * spend their savings on the frame interval, which is what actually scales, and
 * to keep the glow, which turned out to be cheap.
 *
 * Nothing here can touch a readout. The tiers change resolution, glow and frame
 * interval — all decoration. Data still lands every POLL_MS at every tier.
 *
 * Both directions of the ladder are exercised in tests/test_dashboard_fui.py,
 * which runs this section under node with a synthetic clock. That is not
 * ceremony: the first version of this governor was inert, and it read as correct.
 */

/* How far behind its own schedule the loop may fall before it gives up quality.
 *
 * Measured as achieved-vs-requested frame interval, NOT as time spent inside the
 * draw calls. Timing the draw calls was the obvious approach and it does not
 * work: canvas rasterisation happens off the main thread, so `performance.now()`
 * around the drawing reported ~0 ms while the process was in fact burning 130% of
 * a core. A governor calibrated on that number would never act — it would look
 * like it was working while doing nothing at all.
 *
 * Lateness catches what the main thread cannot see: when the board is saturated,
 * frames arrive later than asked for regardless of which thread is busy. 1.6x
 * means "taking more than half again as long as requested". */
const LATE_RATIO = 1.6;

/* Quality tiers, richest first.
 *
 * Glow has two levels rather than an on/off flag: `2` is every element, `1` is
 * only the ones that carry meaning (the board outline, the die, the sweep, the
 * waveform), with the incidental internal traces and connector banks left flat.
 * Even MINIMAL keeps level 1 — a glow-less FUI is a spreadsheet, and the point
 * of degrading is to stay readable, not to stop being the display.
 */
const TIERS = [
  { name: 'FULL', glow: 2, res: 1.0, frameMs: 1000 / 20, dsMs: 240 },
  { name: 'HIGH', glow: 2, res: 1.0, frameMs: 1000 / 15, dsMs: 300 },
  { name: 'MED', glow: 2, res: 0.85, frameMs: 1000 / 12, dsMs: 380 },
  { name: 'LOW', glow: 1, res: 0.7, frameMs: 1000 / 8, dsMs: 500 },
  { name: 'MINIMAL', glow: 1, res: 0.6, frameMs: 1000 / 5, dsMs: 750 },
];

/* The CSS handles prefers-reduced-motion for the CSS animations; a canvas loop
 * is invisible to it, so honour the same switch here. Pinned to the cheapest
 * tier rather than stopped: the hologram and the graticule still need to be on
 * screen, they just stop moving much. */
const REDUCED = window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Start one tier down from the richest. A workstation browser climbs to FULL
 * within a couple of seconds; the board never pays for a burst of expensive
 * frames on the way to discovering it cannot afford them. */
const Q = {
  tier: REDUCED ? TIERS.length - 1 : 1,
  ema: 0,           /* exponential mean of achieved frame interval, ms */
  last: 0,          /* timestamp of the previous frame */
  over: 0,          /* consecutive frames arriving late */
  under: 0,         /* consecutive frames arriving on time */
  get now() { return TIERS[this.tier]; },
};

/* Hysteresis. Stepping down is fast because an over-budget display is actively
 * stealing time from the bench; stepping back up is slow and requires a long
 * quiet stretch, so the tier cannot oscillate visibly. */
const STEP_DOWN_AFTER = 8;
const STEP_UP_AFTER = 240;

function governFrame(t) {
  if (REDUCED) return;  /* pinned by the platform switch; nothing to govern */
  const prev = Q.last;
  Q.last = t;
  if (!prev) return;                       /* no interval to measure yet */
  const gap = t - prev;
  if (gap > 2000) { Q.ema = 0; return; }   /* tab was hidden; not a slow board */

  Q.ema = Q.ema ? Q.ema * 0.85 + gap * 0.15 : gap;
  const want = Q.now.frameMs;

  if (Q.ema > want * LATE_RATIO) {
    Q.under = 0;
    if (++Q.over >= STEP_DOWN_AFTER && Q.tier < TIERS.length - 1) {
      Q.tier++;
      Q.over = 0;
      Q.ema = 0;
    }
  } else if (Q.ema < want * 1.15) {
    Q.over = 0;
    if (++Q.under >= STEP_UP_AFTER && Q.tier > 0) {
      Q.tier--;
      Q.under = 0;
      Q.ema = 0;
    }
  } else {
    Q.over = 0;
    Q.under = 0;
  }
}

/* ---------------------------------------------------------------- decoration */

const HEX = '0123456789ABCDEF';
function hexBlock(rows, cols) {
  let out = '';
  for (let r = 0; r < rows; r++) {
    let line = '';
    for (let c = 0; c < cols; c++) line += HEX[(Math.random() * 16) | 0];
    out += line + '\n';
  }
  return out;
}

/* The background chatter. Synthetic and clearly so: hex, 6px, dim, behind the
 * real log. Kept away from anything a person could read as a measurement. */
function driveDatastreams() {
  $('ds-left').textContent = hexBlock(4, 34);
  $('ds-right').textContent = hexBlock(4, 34);
  const noise = $('log-noise');
  if (noise) noise.textContent = hexBlock(22, 60);
}

/* ------------------------------------------------------------ DUT hologram */

/* An isometric board projection: outline, traces, a die, and a scanning sweep.
 * Canvas rather than SVG so the sweep and the flicker cost nothing per frame. */
function drawDut(ctx, w, h, t, opts) {
  ctx.clearRect(0, 0, w, h);
  if (w < 20 || h < 20) return;

  const live = opts.live;
  const alarm = opts.alarm;
  /* Glow budget. shadowBlur is the dominant per-frame cost under software
   * compositing, so at reduced quality it is spent only where it carries
   * meaning — the board outline and the die — and at the lowest tier not at
   * all. `blur()` returns 0 when we cannot afford it, which is a no-op for
   * canvas rather than a special case at every call site. */
  const glow = opts.glow === undefined ? 2 : opts.glow;
  const blur = (full, reduced) => (glow >= 2 ? full : glow >= 1 ? reduced : 0);
  const base = alarm ? '255,43,61' : live ? '0,229,255' : '61,124,133';
  const cx = w / 2;
  const cy = h * 0.56;
  // Fill the panel: the hologram is the centrepiece, and at half size it reads
  // as an icon rather than a projection.
  const s = Math.min(w / 2.15, h / 1.15);

  // Projection cone from below — the "it is being beamed up there" cue.
  const cone = ctx.createLinearGradient(0, cy + s * 0.5, 0, cy - s * 0.6);
  cone.addColorStop(0, `rgba(${base},0.20)`);
  cone.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = cone;
  ctx.beginPath();
  ctx.moveTo(cx - s * 0.10, cy + s * 0.55);
  ctx.lineTo(cx + s * 0.10, cy + s * 0.55);
  ctx.lineTo(cx + s * 1.05, cy - s * 0.55);
  ctx.lineTo(cx - s * 1.05, cy - s * 0.55);
  ctx.closePath();
  ctx.fill();

  // Slow rotation, and a faint flicker so it reads as a projection rather than
  // a drawing. Amplitude is small: a display that visibly strobes is fatiguing
  // to stand next to for a whole test run.
  const rot = t * 0.00018;
  const flicker = 0.9 + 0.1 * Math.sin(t * 0.004);
  const iso = (x, y, z) => {
    const xr = x * Math.cos(rot) - y * Math.sin(rot);
    const yr = x * Math.sin(rot) + y * Math.cos(rot);
    return [cx + xr * s * 0.9, cy + yr * s * 0.42 - z * s * 0.30];
  };
  const poly = (pts, z, stroke, width, fill) => {
    ctx.beginPath();
    pts.forEach(([x, y], i) => {
      const [px, py] = iso(x, y, z);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.stroke();
  };

  ctx.globalAlpha = flicker;
  ctx.shadowColor = `rgba(${base},0.85)`;
  ctx.shadowBlur = blur(live ? 14 : 4, live ? 8 : 3);

  const board = [[-1, -0.72], [1, -0.72], [1, 0.72], [-1, 0.72]];
  poly(board, 0, `rgba(${base},0.95)`, 1.6, `rgba(${base},0.07)`);

  // Internal traces: deterministic (seeded by index, not random) so the board
  // does not visibly rewire itself every frame.
  ctx.shadowBlur = blur(6, 0);
  ctx.strokeStyle = `rgba(${base},0.42)`;
  ctx.lineWidth = 1;
  for (let i = 0; i < 14; i++) {
    const y = -0.62 + (i / 13) * 1.24;
    const [x1, y1] = iso(-0.92, y, 0.01);
    const [x2, y2] = iso(0.92 - (i % 3) * 0.25, y, 0.01);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  // The SoC: a raised die, brighter than the board. Keeps its glow one tier
  // longer than anything else — it is the focal point of the projection.
  ctx.shadowBlur = blur(live ? 18 : 5, live ? 10 : 4);
  const die = [[-0.30, -0.24], [0.30, -0.24], [0.30, 0.24], [-0.30, 0.24]];
  poly(die, 0.16, `rgba(${base},1)`, 1.8, `rgba(${base},0.16)`);
  // Vertical edges of the die, so it reads as 3D rather than a floating rect.
  // Four short strokes: keep them lit, they are what makes the die read as
  // raised rather than as a rectangle lying on the board.
  ctx.shadowBlur = blur(10, 6);
  ctx.strokeStyle = `rgba(${base},0.75)`;
  ctx.lineWidth = 1;
  die.forEach(([x, y]) => {
    const [x0, y0] = iso(x, y, 0);
    const [x1, y1] = iso(x, y, 0.16);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  });

  // Two connector banks.
  ctx.shadowBlur = blur(4, 0);
  for (let i = 0; i < 8; i++) {
    const x = -0.8 + i * 0.23;
    poly([[x, -0.68], [x + 0.10, -0.68], [x + 0.10, -0.56], [x, -0.56]],
         0.05, `rgba(${base},0.7)`, 1, `rgba(${base},0.2)`);
  }

  // Scan sweep across the board: a bright line travelling nose to tail.
  const sweep = ((t * 0.00035) % 1) * 1.44 - 0.72;
  ctx.shadowBlur = blur(12, 6);
  ctx.strokeStyle = `rgba(${alarm ? '255,120,120' : '42,255,195'},0.85)`;
  ctx.lineWidth = 1.4;
  const [sx1, sy1] = iso(-1, sweep, 0.02);
  const [sx2, sy2] = iso(1, sweep, 0.02);
  ctx.beginPath();
  ctx.moveTo(sx1, sy1);
  ctx.lineTo(sx2, sy2);
  ctx.stroke();

  ctx.shadowBlur = 0;
  ctx.globalAlpha = 1;

  // When there is nothing behind the projection, say so across it. The graphic
  // is stylised enough to look plausible while representing nothing at all.
  if (!live) {
    ctx.fillStyle = 'rgba(127,233,245,0.55)';
    ctx.font = `600 ${Math.max(10, Math.min(18, w / 34))}px "Roboto Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.fillText('— NO DUT TELEMETRY —', cx, h - 10);
  }
}

/* ------------------------------------------------------- waveform / traces */

/* A phosphor-style trace with graticule.
 *
 * `series` is either an array of real samples, or null. Null draws the
 * graticule and a NO SIGNAL legend — it does NOT draw a synthesised waveform.
 * That is the difference between a bench display and a screensaver: nothing is
 * ever plotted that did not come from an instrument.
 */
function drawTrace(ctx, w, h, series, opts) {
  ctx.clearRect(0, 0, w, h);
  if (w < 20 || h < 20) return;
  const colour = opts.alarm ? '255,43,61' : '42,255,195';

  ctx.strokeStyle = 'rgba(0,229,255,0.10)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) {
    const x = (w / 8) * i;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }
  for (let i = 1; i < 5; i++) {
    const y = (h / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(0,229,255,0.22)';
  ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();

  if (!series || series.length < 2) {
    ctx.fillStyle = 'rgba(127,233,245,0.5)';
    ctx.font = `600 ${Math.max(9, Math.min(15, w / 26))}px "Roboto Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.fillText('NO SIGNAL — INSTRUMENT NOT LINKED', w / 2, h / 2 - 8);
    return;
  }

  let lo = Infinity, hi = -Infinity;
  for (const v of series) { if (v < lo) lo = v; if (v > hi) hi = v; }
  // Dynamic scaling, which is what makes one widget work for both a 12 V rail
  // and a nanoamp sleep current. Guard the degenerate flat-line case.
  const span = hi - lo || Math.abs(hi) || 1;
  const pad = span * 0.15;
  lo -= pad; hi += pad;

  // The waveform keeps its phosphor glow at every tier but the very cheapest.
  // It is one stroke over a small canvas — a trivial share of the frame — and it
  // is the element the glow is actually *for*.
  ctx.shadowColor = `rgba(${colour},0.9)`;
  ctx.shadowBlur = opts.glow >= 1 ? 8 : 4;
  ctx.strokeStyle = `rgba(${colour},1)`;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  series.forEach((v, i) => {
    const x = (i / (series.length - 1)) * w;
    const y = h - ((v - lo) / (hi - lo)) * h;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;

  // Raw min/max labels instead of an axis: readable across a room, and no
  // hover tooltip to be unreachable without a mouse.
  ctx.fillStyle = 'rgba(127,233,245,0.75)';
  ctx.font = '9px "Roboto Mono", monospace';
  ctx.textAlign = 'left';
  ctx.fillText(hi.toPrecision(4), 4, 10);
  ctx.fillText(lo.toPrecision(4), 4, h - 4);
}

/* Size the backing store and hand back a context in CSS pixels.
 *
 * `res` scales the backing store below device resolution: the canvas still
 * occupies the same box and is still drawn in the same coordinates, it is just
 * rasterised into fewer pixels and stretched by the compositor. Rendering cost
 * is roughly quadratic in this, which is why it is the second lever after the
 * frame interval — and why it only ever applies to the two decorative canvases,
 * never to text. A slightly soft hologram is atmospheric; a soft readout is
 * unreadable from across the bench.
 */
function fitCanvas(canvas, res) {
  const rect = canvas.getBoundingClientRect();
  const scale = (window.devicePixelRatio || 1) * (res || 1);
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  const bw = Math.max(1, Math.round(w * scale));
  const bh = Math.max(1, Math.round(h * scale));
  if (canvas.width !== bw || canvas.height !== bh) {
    canvas.width = bw;
    canvas.height = bh;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(bw / w, 0, 0, bh / h, 0, 0);
  return [ctx, w, h];
}

/* -------------------------------------------------------------- the model */

/* Last view received, and whether we still believe it. `view` is never used for
 * painting once `alive` is false — see the curtain in applyLiveness(). */
const M = { view: null, alive: false, misses: 0, frames: 0, fps: 0, lastFps: 0 };

/* The second line of a slot: where it is, or why it is dark. Kept short enough
 * to read at three metres, and only ever built from fields the view supplied —
 * an empty string when there is nothing true to say, never a filler. */
function slotDetail(inst) {
  if (inst.open_error) {
    // The agent's own message, trimmed. Its exact text is the useful part (a
    // permission error and a missing cable read very differently), so it is
    // shortened rather than replaced with a category.
    return String(inst.open_error).replace(/^\w*Error\(?['"]?/, '').slice(0, 40);
  }
  if (inst.linked && inst.path) return inst.path;
  if (inst.present === true) {
    // A heuristic identification is a guess, and on this rail a guess must be
    // visible as one — the QR10x behind a CH340 is the case that matters.
    return (inst.confidence && inst.confidence !== 'exact')
      ? `${inst.path || 'on bus'} · ${inst.confidence}`
      : (inst.path || 'on bus');
  }
  if (!inst.served) return 'no driver configured';
  // NO ID: the scan ran and came back without it, but this instrument has no USB
  // signature to match, so the scan proves nothing either way. Say which it is,
  // or the slot looks like an unexplained failure.
  if (inst.present === false && inst.discoverable === false) {
    return 'not identifiable by bus scan';
  }
  return '';
}

/* Who this instrument actually is, from the bus scan: model and serial.
 *
 * Empty whenever the view did not supply it, which is the disconnected case —
 * the state model drops identity when the session dies, so this line goes blank
 * rather than leaving a serial number on screen for hardware nobody has checked
 * since. A stale identity is the dangerous kind: it is a claim about the
 * physical bench, and it is what makes an operator believe the run drove the
 * supply in front of them.
 *
 * The model string is trimmed to keep the slot one line tall; the serial never
 * is, because a truncated serial is worse than none — it still looks like an
 * answer. */
function slotIdentity(inst) {
  const bits = [];
  if (inst.hw_label) bits.push(String(inst.hw_label).slice(0, 34));
  if (inst.serial_number) bits.push(`SN ${inst.serial_number}`);
  // USB ID only when nothing better is known. It identifies a model, not a
  // unit, so it is a fallback rather than an addition to a real serial.
  if (!bits.length && inst.usb_id) bits.push(inst.usb_id);
  return bits.join(' · ');
}

/* What this instrument is doing at this instant: the method its worker thread is
 * inside, and anything queued behind it.
 *
 * A separate line from .state on purpose. The status word is the slot's ranked
 * claim — ARMED, OPEN FAILED, OPEN, STANDBY — and an armed output must keep it; the
 * view carries `busy` as its own field for exactly that reason, so this line
 * adds to the status rather than competing with it.
 *
 * Empty whenever the view did not supply a value, which is where the honesty rule
 * lands: the view withholds `busy` on a stale or dead session, so this goes blank
 * rather than leaving a call frozen in flight. Not re-derived here — one place
 * decides, and it is the one CI can assert. */
// How recently a device must have been touched for its card to be highlighted as
// the one in use. Much shorter than the view's RECENT_ACTION_S (8s, sized to
// bridge the 5s status poll) because these two answer different questions: the
// `.act` line reports "last seen doing something, 6s ago", which stays true and
// useful, while the highlight asserts "this is the instrument being driven right
// now". A run's reads land every few hundred ms, so 2.5s spans the gaps between
// them without letting the glow outlive the traffic.
const TOUCH_WINDOW_S = 2.5;

function touchedNow(inst) {
  const r = inst.recent;
  // The view withholds `recent` entirely on a stale view and drops it past its own
  // window, so absence here already means "no claim to make" — this only has to
  // decide whether a real, fresh stamp is fresh *enough*.
  if (!r || typeof r.age_s !== 'number') return false;
  return r.age_s <= TOUCH_WINDOW_S;
}

function slotActivity(inst) {
  if (!inst.busy) {
    // Nothing in flight *as of the last poll*. That poll runs every 5s while a
    // device call takes ~200ms, so it almost never lands inside one: driving a
    // sweep of six setpoints left the DMM and the QR10x looking untouched for the
    // whole test. `recent` comes from action events instead, which arrive as each
    // call completes, so it sees calls the poll cannot.
    //
    // Phrased in the past tense and carrying its age, because that is what it is.
    // The view drops it after RECENT_ACTION_S and withholds it on a stale view, so
    // this cannot claim activity on a bench that has gone quiet or a feed that has
    // stopped.
    const r = inst.recent;
    if (!r) return '';
    const ago = `${r.age_s}s ago`;
    return r.action ? `${String(r.action).slice(0, 30)} · ${ago}` : ago;
  }
  const bits = [String(inst.busy).slice(0, 30)];
  // Queue depth only when something is actually waiting. "+0" is arithmetic, not
  // information, and the view already reports 0 for "nobody said".
  if (inst.queued > 0) bits.push(`+${inst.queued}`);
  return bits.join(' ');
}

function renderRail(instruments) {
  const rail = $('right');
  // Build once, then update in place: rebuilding the rail every 500ms would
  // restart the armed-slot pulse animation on every poll.
  //
  // Keyed on identity, not just length. The rail adapts to whatever the agent
  // serves, so its membership can change without its size changing — swap a DMM
  // for a scope and a length check sees nothing, leaving every row's dataset.key
  // pointing at the instrument that used to be there. The visible text would
  // update and the key underneath would lie.
  const wanted = instruments.map((i) => i.key).join(' ');
  if (rail.dataset.keys !== wanted) {
    rail.dataset.keys = wanted;
    rail.innerHTML = '';
    for (const inst of instruments) {
      const el = document.createElement('div');
      el.className = 'slot';
      el.dataset.key = inst.key;
      el.innerHTML =
        `<div class="name"></div><div class="role"></div>`
        + `<div class="ident"></div>`
        + `<div class="state"></div><div class="act"></div>`
        + `<div class="detail"></div>`;
      rail.appendChild(el);
    }
  }
  instruments.forEach((inst, i) => {
    const el = rail.children[i];
    el.querySelector('.name').textContent = inst.label;
    el.querySelector('.role').textContent = inst.role;
    // textContent: this is a USB string descriptor read off the bus, so it is
    // device-supplied and must never be interpolated into markup.
    el.querySelector('.ident').textContent = slotIdentity(inst);
    el.querySelector('.state').textContent = inst.status;
    // textContent: a driver method name, which comes from the agent's worker
    // label rather than from anything this page composes.
    const act = slotActivity(inst);
    el.querySelector('.act').textContent = act;
    // textContent, not innerHTML: this carries an agent-supplied error string
    // and a discovered device path, neither of which this page composes.
    el.querySelector('.detail').textContent = slotDetail(inst);
    el.className = slotClasses(inst, act);
  });
}

/* Which state classes a rail slot wears. A pure function of the slot and the
 * activity line, for the same reason slotActivity is one: this is where the
 * panel's visual vocabulary is decided, and a decision table that needs a DOM to
 * exercise is a decision table nobody tests. */
function slotClasses(inst, act) {
  return 'slot'
      + (inst.linked ? ' linked' : '')
      + (inst.armed ? ' armed' : '')
      + (inst.inferred ? ' inferred' : '')
      + (inst.stale ? ' stale' : '')
      // Present-but-not-open. Dimmer than linked and brighter than absent: the
      // hardware is there, which is worth seeing from across the bench.
      + (inst.ready ? ' ready' : '')
      // Something the operator can act on — an open failure, or hardware present
      // that nothing is configured to drive.
      + (inst.attention ? ' attention' : '')
      // Keyed on the line we actually drew AND on there being a live call, not on
      // `act` alone: the class drives a pulse, and an animation running over a
      // blank line is a slot that looks alive while saying nothing. `act` now also
      // carries a past-tense "…3.2s ago" line, which must NOT pulse — a pulse is
      // read across the bench as "happening now", and that is the one thing a
      // completed action is not. Withheld and stale both resolve to '' above, so
      // this cannot outlive the value it advertises.
      + (act && inst.busy ? ' working' : '')
      // Drawn as a fading trace instead: recently active, not active.
      + (act && !inst.busy ? ' recent' : '')
      // Enrolled in a live run: held for the run's whole duration, dwells
      // included, so it is a steady state rather than a pulse.
      + (inst.run ? ' inrun' : '')
      // The device being acted on *at this moment* — the whole-card highlight, as
      // opposed to the `.act` line's text. Driven off `recent.age_s` rather than
      // `busy` because `busy` is sampled on the 5s status poll while a call takes
      // ~200ms, so it misses all but ~4% of calls; action events arrive as each
      // call completes and see every one. `busy` still counts when present, since
      // a call caught in flight is the strongest possible evidence.
      //
      // TOUCH_WINDOW_S, not RECENT_ACTION_S: the `.act` text may legitimately say
      // "6s ago" while the card must have stopped claiming to be the one in use.
      // A highlight is read across the bench as "this one, now".
      //
      // KNOWN GAP, measured on hardware: during a *run* this stays dark. Action
      // events are emitted on the remote-call path in server.py, and the run
      // engine drives the device in-process without going through it, so a run's
      // reads produce none. Verified: 56/56 samples lit while a client drove the
      // DMM directly, 1/83 during a run reading the same instrument every 200ms.
      // `.inrun` covers that case — deliberately steady rather than a pulse, since
      // enrollment lasts minutes — so the rail is not silent about a running test,
      // but it cannot yet say *which* instrument a multi-device run is talking to
      // at this instant. Closing it means emitting an action event from the
      // engine's device path, which is on the bench's hot loop and needs the
      // ActionCoalescer treatment; not done here rather than done cheaply.
      + (inst.busy || touchedNow(inst) ? ' touched' : '');
}

/* The one-line summary above the rail. Counts come from the view, which derives
 * them from the same slots the rail renders, so the header cannot disagree with
 * the column under it. */
function renderRailHead(bench, connected) {
  const el = $('rail-count');
  if (!bench || !connected) {
    el.textContent = 'NO LINK';
    el.className = 'val bad';
    return;
  }
  if (!bench.inventory_taken) {
    // No scan has landed, so the bus population is unknown. Reporting "0 on bus"
    // here would be an assertion nobody has checked.
    el.textContent = `${bench.linked}/${bench.total} LINKED · SCANNING`;
    el.className = 'val';
    return;
  }
  // Denominator is what a scan can decide, not the slot count: a device with no
  // VID/PID signature would otherwise make a full bench read 4/5 forever. Falls
  // back to total for a view built before `scannable` existed.
  const of = bench.scannable == null ? bench.total : bench.scannable;
  if (!of) {
    // Nothing on this bench can be found by a scan — a bench of nothing but the
    // QR10x reaches this. "0/0 ON BUS" is arithmetic, not information, so report
    // the links and stay silent about a bus population we cannot measure.
    el.textContent = `${bench.linked}/${bench.total} LINKED`;
    el.className = 'val';
    return;
  }
  el.textContent = `${bench.linked} LINKED · ${bench.present}/${of} ON BUS`;
  el.className = 'val';
}

/* Hardware on the bus that no driver claims. Hidden entirely when there is none,
 * rather than showing an empty box: a permanent "UNCLAIMED: none" is furniture,
 * and this should only ever appear when it has something to say. */
function renderUnclaimed(items) {
  const box = $('unclaimed');
  const list = $('unclaimed-list');
  if (!items || !items.length) {
    box.hidden = true;
    list.textContent = '';
    return;
  }
  box.hidden = false;
  list.textContent = '';
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'item';
    const id = document.createElement('span');
    id.className = 'id';
    id.textContent = it.usb_id || '';
    row.appendChild(id);
    // Built by node rather than innerHTML: these strings are USB descriptors read
    // off the bus, so they are device-supplied and never interpolated into markup.
    row.appendChild(document.createTextNode(' ' + (it.label || 'unidentified')));
    list.appendChild(row);
  }
}

/* One port chip's classes. A pure function for the same reason slotClasses is,
 * and here the stakes are the reason rather than the convention: this decides
 * whether an outlet reads as energised, and getting it backwards is the worst
 * output this display can produce.
 *
 * `on` wins over `stale`. An energised outlet whose reading has aged is still an
 * outlet that was last seen energised, and the amber-strikethrough treatment the
 * rail uses for a stale value would drop it to something an eye scanning for hot
 * ports skips over. Stale gets its own dimming on top (see .port.stale in
 * fui.css), never in place of the live colour. */
function portClasses(p, stale) {
  return 'port ' + (p.on ? 'on' : 'off') + (stale ? ' stale' : '');
}

/* MAINS.MGR — the bench's mains and its outlets.
 *
 * Core harness. This panel exists because the dashboard is an OBSERVER session
 * and `device.call` is not in the agent's OBSERVER_METHODS, so a display can
 * never read the PDU itself: everything here arrived because the *bench* pushed
 * it (agent/server.py's mains sweep, plus the run engine's run_outlet events at
 * each verified switch). Nothing on this panel is polled, which is why staleness
 * gets its own treatment — there is no correcting read to fall back on.
 *
 * Hides itself when the bench has no PDU, and only then. See the comment on
 * #mains-panel in index.html for why this one panel is allowed to disappear when
 * every other absent readout stays on screen saying NO LINK. */
function renderMains(m) {
  const panel = $('mains-panel');
  if (!m || (!m.served && !m.known)) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const verdict = $('mains-verdict');
  verdict.textContent = m.status;
  // A verdict earns amber for being unvouched-for, not for being bad news: an
  // outlet map nobody has confirmed lately is the case an operator must not
  // trust, whereas MAINS LIVE with a current reading is the ordinary state of a
  // working bench. Red is not used here at all — reserved, per fui.css's header,
  // for states needing action, and a PDU sitting there powering the bench is not
  // one.
  verdict.className = 'val' + (!m.known || m.stale ? ' warn' : '');

  const setReading = (id, text) => {
    const el = $(id);
    el.textContent = text;
    // Every figure is either a number the bench sent or the literal NO LINK; the
    // view builder guarantees there is no third case, and the class follows the
    // text rather than being decided again here.
    el.className = 'v' + (text === 'NO LINK' ? ' bad' : m.stale ? ' warn' : '');
  };
  setReading('mains-voltage', m.voltage);
  setReading('mains-frequency', m.frequency);
  // Amps and watts on one line: they are two views of the same quantity, and the
  // panel is narrow. Both or neither — a bare "48 W" beside a missing current
  // reading invites the arithmetic that would recover a number nobody measured.
  setReading('mains-load',
    m.load_A === 'NO LINK' && m.load_W === 'NO LINK'
      ? 'NO LINK' : `${m.load_A} · ${m.load_W}`);

  renderPorts(m);

  // The note line carries whichever single fact most changes what the operator
  // does next, worst first. Only one, because a panel this small with three lines
  // of explanation is one nobody reads.
  const note = $('mains-note');
  if (!m.known) {
    // Both routes here are ordinary on a healthy idle bench: no sweep has landed
    // yet, or nothing has opened the PDU. Worth saying which, because "the bench
    // has not told us" and "there is a fault" look identical from an empty panel
    // and only one of them warrants walking over to the rack.
    note.textContent = 'no mains report from the bench yet';
    note.className = 'mains-note warn';
  } else if (m.aged_out) {
    // The specific fault this panel can detect and no other can: the pushes
    // stopped while the rest of the display is current. The global staleness
    // banner cannot see it, because mains runs on a clock of its own.
    note.textContent = `port states ${Math.round(m.age_s)}s old — not current`;
    note.className = 'mains-note warn';
  } else if (m.stale) {
    note.textContent = 'display is stale — ports not vouched for';
    note.className = 'mains-note warn';
  } else if (m.settling_s) {
    note.textContent = `DUT settling ${m.settling_s}s after switch`;
    note.className = 'mains-note';
  } else if (m.last_transition) {
    // A switch that happened between two sweeps. Sampling at ~10s can step
    // clean over a power cycle and render it as "on, on", so the engine's
    // event is the only record that the transition occurred at all.
    const t = m.last_transition;
    note.textContent =
      `last switch: port ${t.outlet} ${t.state ? 'ON' : 'OFF'}`;
    note.className = 'mains-note';
  } else {
    note.textContent = '';
    note.className = 'mains-note';
  }
}

/* The port chips. Rebuilt only when the count changes, matching renderRail's
 * approach: the labels below churn every sweep and reallocating eight nodes twice
 * a second on the board's GPU is a cost with nothing to show for it. */
function renderPorts(m) {
  const box = $('mains-ports');
  const ports = m.outlets || [];
  if (box.childElementCount !== ports.length) {
    box.textContent = '';
    for (let i = 0; i < ports.length; i++) {
      const chip = document.createElement('div');
      chip.className = 'port';
      const idx = document.createElement('span');
      idx.className = 'pi';
      const state = document.createElement('span');
      state.className = 'ps';
      chip.appendChild(idx);
      chip.appendChild(state);
      box.appendChild(chip);
    }
  }
  ports.forEach((p, i) => {
    const chip = box.children[i];
    chip.className = portClasses(p, m.stale);
    chip.children[0].textContent = p.index;
    chip.children[1].textContent = p.label;
  });
}

// The class for one sequence node. Extracted as a pure function for the same
// reason slotClasses is: this is a decision table, and one that needs a DOM to
// exercise is one nobody tests.
//
// `done` and `active` are mutually exclusive by construction upstream — a stage
// cannot be both before and at the active one — but the order here pins which
// wins if that ever stops holding, and `active` is the one an operator is
// watching.
function stageClasses(s) {
  if (s.active) return 'stage-node active';
  if (s.done) return 'stage-node done';
  return 'stage-node';
}

function renderFlow(stages, unknown) {
  const flow = $('flow');
  if (flow.childElementCount !== stages.length * 2 - 1) {
    flow.innerHTML = '';
    stages.forEach((s, i) => {
      if (i) {
        const link = document.createElement('div');
        link.className = 'stage-link';
        flow.appendChild(link);
      }
      const node = document.createElement('div');
      node.className = 'stage-node';
      flow.appendChild(node);
    });
  }
  let activeIdx = -1;
  stages.forEach((s, i) => {
    const node = flow.children[i * 2];
    node.textContent = s.name;
    node.className = stageClasses(s);
    if (s.active) activeIdx = i;
  });
  // Light the links *up to* the active node: the flow of power/data so far.
  for (let i = 1; i < stages.length; i++) {
    const link = flow.children[i * 2 - 1];
    link.className = 'stage-link' + (activeIdx >= i ? ' lit' : '');
  }
  // The bench named a stage this build cannot place on the row. Say so, rather
  // than showing NO SEQUENCE beside a run that is plainly in progress — the
  // reading would be "no test running", which is the lie this panel avoids.
  $('flow-verdict').textContent = activeIdx >= 0
    ? stages[activeIdx].name
    : (unknown ? unknown + ' (?)' : 'NO SEQUENCE');
}

function renderLog(log) {
  const box = $('log');
  if (!log.length) {
    box.innerHTML = '<div class="empty">no events</div>';
    return;
  }
  // Built with textContent rather than an innerHTML template. The values come
  // from our own agent, so this is not about an attacker — it is that a device
  // name or event kind containing "<" would silently swallow the rest of the
  // row, and this pane is the authoritative record of what the bench did. A log
  // that garbles the one event you came to read is worse than a slow one, and at
  // 24 rows the DOM calls cost nothing.
  box.textContent = '';
  for (const e of log.slice().reverse()) {
    const row = document.createElement('div');
    // `fail` rather than reusing the severity classes: a failed action is amber
    // or red because it FAILED, which is a different fact from its severity
    // grade, and an operator scanning the pane is looking for the failures.
    row.className = `row ${e.severity}` + (e.ok === false ? ' fail' : '');
    const add = (cls, text) => {
      if (!text) return null;
      const span = document.createElement('span');
      span.className = cls;
      span.textContent = text;
      row.appendChild(span);
      return span;
    };
    add('seq', e.seq === null || e.seq === undefined
      ? '····' : String(e.seq).padStart(4, '0'));
    // The verb, which for a device.call is the driver method ("set_voltage").
    // This is the column the eye reads first, so it gets the bright cyan.
    add('act', e.action || e.kind);
    add('dev', e.device);
    // "×47": this row stands for a burst the agent folded. Drawn so a summarised
    // log looks summarised rather than looking like 47 things never happened.
    if (e.count > 1) add('mult', `×${e.count}`);
    // The failure text wins the row's remaining width when there is one: on a
    // line that failed, the exception IS the content.
    if (e.ok === false && e.error) add('err', e.error);
    else add('detail', e.detail);
    box.appendChild(row);
  }
}

function render(v) {
  const head = $('headline');
  head.textContent = v.headline;
  head.className = `sev-${v.severity}` + (v.starting ? ' starting' : '');

  const banner = $('banner');
  if (v.stale_reason) {
    banner.textContent = '⚠ ' + v.stale_reason;
    banner.className = v.unsafe ? 'critical' : '';
  } else {
    banner.className = 'hidden';
  }

  $('operation').textContent = v.operation;
  $('operation').className = v.severity === 'critical' ? 'critical'
    : v.severity === 'alarm' ? 'alarm'
    : v.severity === 'warn' ? 'warn' : '';

  const alerts = $('alerts');
  $('alert-count').textContent = v.alerts;
  alerts.className = v.alerts > 0 ? 'live' : '';

  $('net-state').textContent = 'OK';
  $('net-state').className = 'ok';
  $('agent-state').textContent = v.connected ? 'LINKED' : 'NO LINK';
  $('agent-state').className = v.connected ? 'ok' : 'bad';

  $('sys-verdict').textContent = v.connected ? (v.trustworthy ? 'OK' : 'DEGRADED') : 'NO LINK';
  $('sys-link').textContent = v.connected ? 'LINKED' : 'NO LINK';
  $('sys-link').className = 'v' + (v.connected ? '' : ' bad');
  $('sys-view').textContent = v.trustworthy ? 'CURRENT' : (v.starting ? 'LINKING' : 'STALE');
  $('sys-view').className = 'v' + (v.trustworthy ? '' : ' warn');
  $('sys-dropped').textContent = v.dropped_events;
  $('sys-dropped').className = 'v' + (v.dropped_events ? ' bad' : '');
  $('sys-relinks').textContent = v.reconnects;

  $('state-verdict').textContent = v.headline;
  $('state-headline').textContent = v.headline;
  $('state-armed').textContent = v.armed.length ? v.armed.join(', ') : 'none';
  $('state-armed').className = 'v' + (v.armed.length ? ' bad' : '');
  $('state-runs').textContent = v.runs.length
    ? v.runs.map((r) => `${r.id}:${r.state}`).join(' ')
    : 'none';
  const active = v.stages.find((s) => s.active);
  $('state-stage').textContent = active ? active.name : 'none';

  // The pane header says how much it is NOT showing. 24 rows off a stream that
  // can run at thousands of actions a second is a summary, and a summary that
  // does not admit it is one reads as a complete record.
  const verdict = $('log-verdict');
  if (v.actions_folded > 0) {
    verdict.textContent = `${v.actions} ACT / ${v.actions_folded} FOLDED`;
  } else if (v.actions > 0) {
    verdict.textContent = `${v.actions} ACT`;
  } else if (v.link_beats > 0) {
    // Nothing has happened on this bench yet, and that is the whole difficulty:
    // an empty log looks identical whether the agent is idle or gone. The beat
    // count is the only positive evidence the link is alive, so show it rather
    // than a bare IDLE that would read the same on a dead connection.
    verdict.textContent = `LINK ${v.link_beats}`;
  } else {
    verdict.textContent = v.log.length ? 'ACTIVE' : 'IDLE';
  }
  renderLog(v.log);
  renderMains(v.mains);
  renderRail(v.instruments);
  renderRailHead(v.bench, v.connected);
  renderUnclaimed(v.unclaimed);
  renderFlow(v.stages, v.stage_unknown);

  // The DMM readout. There is no measurement in the observer status payload, so
  // this is NO LINK until one exists — deliberately not a plausible number.
  // When agent.status grows per-channel readings, they land here.
  const dmm = v.instruments.find((i) => i.kind === 'dmm');
  const readout = $('dmm-readout');
  const note = $('dmm-note');
  if (dmm && dmm.linked) {
    readout.textContent = dmm.status;
    readout.className = 'readout' + (dmm.stale ? ' stale' : '');
    // The instrument is linked but the observer status carries no measurement,
    // so say that outright. A big "IDLE" with nothing qualifying it invites
    // being read as a range or a measurement mode.
    note.textContent = dmm.stale
      ? 'reading is stale — not current'
      : 'link up · no measurement channel yet';
    note.className = 'readout-note';
    $('dmm-verdict').textContent = dmm.status;
  } else {
    readout.textContent = 'NO LINK';
    readout.className = 'readout no-link';
    note.textContent = 'instrument not linked';
    note.className = 'readout-note';
    $('dmm-verdict').textContent = 'NO LINK';
  }
  $('dmm-pills').innerHTML = ['Vdc', 'Vac', 'Idc', 'Iac', 'Ω']
    .map((m) => `<span class="pill">${m}</span>`).join('');

  const psu = v.instruments.find((i) => i.kind === 'psu');
  $('psu-verdict').textContent = psu && psu.linked ? psu.status : 'NO LINK';
  $('dut-verdict').textContent = dutLabel(v);
}

/* What to print beside DEVICE UNDER TEST. Extracted for the same reason
 * slotClasses and stageClasses are: it is a decision table, and one that needs a
 * DOM to exercise is one nobody tests.
 *
 * Four distinct things, never collapsed:
 *
 *   NO LINK       the panel cannot reach the bench, so it knows nothing
 *   NO RUN        the bench is reachable and no run is in flight
 *   UNSPECIFIED   a run IS in flight and declared no DUT (``dut`` defaults to "")
 *   <name>        the run said what it is testing on
 *
 * UNSPECIFIED rather than a blank or a plausible-looking placeholder: the spec's
 * ``dut`` is free text with an empty default, so "the author did not say" is a
 * real and common state, and it must not be renderable as a DUT that exists. The
 * stale case keeps the name and lets the panel's own staleness treatment say the
 * age — the DUT of a run does not stop being that DUT because the feed lagged. */
function dutLabel(v) {
  if (!v.connected) return 'NO LINK';
  if (!v.dut_known) return 'NO RUN';
  return v.dut || 'UNSPECIFIED';
}

/* The curtain. When the page cannot reach its own server, the numbers on screen
 * are of unknown age, and the display says exactly that rather than continuing
 * to look authoritative. */
function applyLiveness() {
  const dead = $('dead');
  if (M.alive) {
    dead.className = '';
    return;
  }
  dead.className = 'show';
  $('net-state').textContent = 'NO LINK';
  $('net-state').className = 'bad';
}

async function poll() {
  try {
    const res = await fetch('/api/view', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    M.view = await res.json();
    M.misses = 0;
    M.alive = true;
    render(M.view);
  } catch (err) {
    M.misses++;
    if (M.misses >= DEAD_AFTER) {
      M.alive = false;
      $('dead-why').textContent =
        `cannot reach the dashboard server (${M.misses} failed polls) — nothing on screen is current`;
    }
  }
  applyLiveness();
}

/* ---------------------------------------------------------------- the loop */

let lastDs = 0;
let lastFpsAt = 0;

function frame(t) {
  const q = Q.now;

  M.frames++;
  if (t - lastFpsAt > 1000) {
    M.fps = Math.round((M.frames * 1000) / (t - lastFpsAt));
    M.frames = 0;
    lastFpsAt = t;
    const el = $('sys-fps');
    // The tier is on screen next to the frame rate on purpose. If the panel is
    // drawing coarsely, that is a fact about this display worth being able to
    // read off it, rather than something to discover with a profiler.
    if (el) el.textContent = `${M.fps} FPS · ${q.name}`;
  }
  // Retexture the decoration a few times a second; any faster is unreadable
  // strobing and any slower stops looking like a data stream.
  if (t - lastDs > q.dsMs) { driveDatastreams(); lastDs = t; }

  const v = M.view;
  // Only the live, trustworthy case gets the bright treatment; everything else
  // is drawn dim, and an unsafe/armed bench is drawn in alarm red.
  const live = !!(M.alive && v && v.connected && v.trustworthy);
  const alarm = !!(v && (v.unsafe || (v.armed && v.armed.length)));

  const [dctx, dw, dh] = fitCanvas($('dut'), q.res);
  drawDut(dctx, dw, dh, t, { live, alarm, glow: q.glow });

  const [tctx, tw, th] = fitCanvas($('trace'), q.res);
  // null series: there is no waveform data in the status payload, so the scope
  // shows its graticule and says NO SIGNAL rather than plotting an invention.
  drawTrace(tctx, tw, th, null, { alarm, glow: q.glow });

  // Let the governor see how late this frame was relative to the tier's target.
  governFrame(t);
  scheduleFrame();
}

/* Re-arm on a timer at the tier's interval, then take one animation frame.
 *
 * Requesting the next frame directly from inside the callback would ask the
 * compositor for 60 frames a second and skip most of them — which measured
 * almost as expensive as drawing them all, because the frames were still being
 * produced. Sleeping first means the compositor genuinely idles in between.
 */
function scheduleFrame() {
  setTimeout(() => requestAnimationFrame(frame), Q.now.frameMs);
}

driveDatastreams();
applyLiveness();
poll();
/* The data clock, wholly independent of the frame clock above: throttling the
 * scenery must never slow down the numbers. */
setInterval(poll, POLL_MS);
requestAnimationFrame(frame);
