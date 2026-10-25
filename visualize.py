"""Visual demo of the rate limiter.

Runs scripted scenarios against the real RateLimiter (manual clock, wired the
same way demo.py does), records every call as an event, and writes:

    trace.json   the raw trace
    visual.html  a self-contained page (no network) with the same trace embedded

Run:  python3 visualize.py      then open visual.html by double-clicking it.
"""

import json
from pathlib import Path

from rate_limiter import (
    Channel,
    FailClosed,
    FailOpen,
    Limits,
    LogStore,
    MappingCache,
    Membership,
    Outcome,
    RateLimiter,
)

HERE = Path(__file__).resolve().parent
LIMITS = Limits(user_cap=3, team_cap=5, company_cap=8)
CAPS = {"user": LIMITS.user_cap, "team": LIMITS.team_cap, "company": LIMITS.company_cap}
LANES = ("user", "team", "company")
MEMBERS = {
    "alice": Membership(team_id="design", company_id="acme"),
    "bob": Membership(team_id="design", company_id="acme"),
    "carol": Membership(team_id="design", company_id="acme"),
}

# Each send is (simulated time in seconds, user, request_id). Every scenario
# starts from an empty limiter at t=0.
SCENARIOS = [
    {
        "id": "user-cap",
        "title": "User cap",
        "description": "Alice sends 4 emails, 60 s apart. Her cap is 3, so the 4th send waits until the oldest one leaves the window.",
        "sends": [(0, "alice", "a1"), (60, "alice", "a2"), (120, "alice", "a3"), (180, "alice", "a4")],
    },
    {
        "id": "same-request",
        "title": "Same request_id",
        "description": "Retrying a send that is already in the log is allowed, but it is not counted a second time.",
        "sends": [(0, "alice", "a1"), (30, "alice", "a2"), (45, "alice", "a1")],
    },
    {
        "id": "team-cap",
        "title": "Team cap",
        "description": "Alice and Bob fill the design team's 5 slots. Carol has sent nothing, but the team lane is full, so she is blocked.",
        "sends": [
            (0, "alice", "a1"),
            (60, "alice", "a2"),
            (120, "alice", "a3"),
            (180, "bob", "b1"),
            (240, "bob", "b2"),
            (300, "carol", "c1"),
        ],
    },
    {
        "id": "window-slide",
        "title": "Window slides",
        "description": "A send stops counting exactly 600 s after it was made. At t=600 the send from t=0 expires and alice is allowed again.",
        "sends": [
            (0, "alice", "a1"),
            (60, "alice", "a2"),
            (120, "alice", "a3"),
            (300, "alice", "a4"),
            (599, "alice", "a5"),
            (600, "alice", "a6"),
            (1000, "alice", "a7"),
        ],
    },
]


class Clock:
    """A clock we move by hand, same idea as demo.py."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class StaticOrgLookup:
    def __init__(self, members: dict[str, Membership]) -> None:
        self._members = members

    def lookup(self, user_id: str) -> Membership | None:
        return self._members.get(user_id)


def make_limiter() -> tuple[RateLimiter, Clock]:
    clock = Clock()
    limiter = RateLimiter(
        mapping_cache=MappingCache(org_lookup=StaticOrgLookup(MEMBERS), ttl_seconds=300, clock=clock),
        log_store=LogStore(limits=LIMITS, clock=clock),
        limits=LIMITS,
        policies={Channel.EMAIL: FailOpen(), Channel.SMS: FailClosed()},
    )
    return limiter, clock


def fmt(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.1f}"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def lane_keys(user_id: str) -> dict[str, str]:
    m = MEMBERS[user_id]
    return {
        "user": f"user:{user_id}",
        "team": f"team:{m.team_id}",
        "company": f"company:{m.company_id}",
    }


def snapshot(limiter: RateLimiter, keys: dict[str, str]) -> dict[str, list[list]]:
    """Read-only copy of the log timestamps for the three keys of this user."""
    logs = limiter.log_store._logs
    return {key: [[ts, rid] for ts, rid in logs.get(key, [])] for key in keys.values()}


def run_scenario(spec: dict) -> dict:
    limiter, clock = make_limiter()
    window = LIMITS.window_seconds
    attempts: dict[str, int] = {}
    events: list[dict] = []

    for step, (t, user, rid) in enumerate(spec["sends"]):
        clock.now = float(t)
        keys = lane_keys(user)
        before = snapshot(limiter, keys)
        # Same test the limiter makes on the user key: this request_id is still in the window.
        duplicate = any(r == rid and ts > t - window for ts, r in before[keys["user"]])

        decision = limiter.allow(rid, user, Channel.EMAIL)
        after = snapshot(limiter, keys)

        blocked: list[str] = []
        limiting = None
        if decision.outcome == Outcome.REJECTED:
            # Which lanes are full now, and which one frees last (the one retry_after comes from).
            best = -1.0
            for lane in LANES:
                entries = after[keys[lane]]
                if len(entries) >= CAPS[lane]:
                    blocked.append(lane)
                    wait = entries[0][0] + window - t
                    if wait > best:
                        best, limiting = wait, lane
            assert abs(best - decision.retry_after) < 1e-9, (
                f"retry_after mismatch at t={t}: {best} vs {decision.retry_after}"
            )

        if not duplicate:
            attempts[user] = attempts.get(user, 0) + 1
        who = f"{user}'s {ordinal(attempts.get(user, 0))} send"

        # If an entry from before this call reached exactly ts+window now, say so.
        note = ""
        for ts, _ in before[keys["user"]]:
            if ts + window == t:
                note = f" The send at t={fmt(ts)} s reached {fmt(ts)} + {fmt(window)} and stopped counting."

        if decision.outcome == Outcome.REJECTED:
            parts = " and ".join(
                f"{lane} lane is full ({len(after[keys[lane]])} of {CAPS[lane]})" for lane in blocked
            )
            caption = f"{who}: {parts}, retry in {fmt(decision.retry_after)} s."
        elif duplicate:
            caption = (
                f"{user} retries {rid}: it is already in the log, so it is allowed "
                f"without being counted again (user lane {len(after[keys['user']])} of {CAPS['user']})."
            )
        else:
            counts = ", ".join(f"{lane} {len(after[keys[lane]])} of {CAPS[lane]}" for lane in LANES)
            caption = (
                f"{who} ({rid}) at t={fmt(t)} s: every lane has room, so it is allowed "
                f"({counts})."
            )
        caption += note

        events.append({
            "step": step,
            "t": float(t),
            "user": user,
            "request_id": rid,
            "channel": Channel.EMAIL.value,
            "outcome": decision.outcome.value,
            "rejection_reason": decision.rejection_reason.value if decision.rejection_reason else None,
            "retry_after": decision.retry_after,
            "fallback": decision.fallback,
            "error_reason": decision.error_reason.value if decision.error_reason else None,
            "duplicate": duplicate,
            "keys": keys,
            "caps": dict(CAPS),
            "window_seconds": window,
            "snapshot": after,
            "blocked_lanes": blocked,
            "limiting_lane": limiting,
            "caption": caption,
        })

    return {
        "id": spec["id"],
        "title": spec["title"],
        "description": spec["description"],
        "events": events,
    }


def build_trace() -> dict:
    return {
        "generated_by": "visualize.py",
        "limits": {
            "user_cap": LIMITS.user_cap,
            "team_cap": LIMITS.team_cap,
            "company_cap": LIMITS.company_cap,
            "window_seconds": LIMITS.window_seconds,
        },
        "window_seconds": LIMITS.window_seconds,
        "scenarios": [run_scenario(spec) for spec in SCENARIOS],
    }


def print_summary(trace: dict) -> None:
    for sc in trace["scenarios"]:
        print(f"\n{sc['title']} ({len(sc['events'])} events)")
        for ev in sc["events"]:
            reason = f" {ev['rejection_reason']}" if ev["rejection_reason"] else ""
            retry = f" retry_after={fmt(ev['retry_after'])}" if ev["retry_after"] is not None else ""
            dup = " (duplicate)" if ev["duplicate"] else ""
            print(
                f"  t={fmt(ev['t']):>4}  {ev['user']:<6} {ev['request_id']:<4}"
                f" {ev['outcome']}{reason}{retry}{dup}"
            )
            print(f"          {ev['caption']}")


PLACEHOLDER = "/*__TRACE_JSON__*/null"

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rate limiter, one window at a time</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a22;
    --ink: #e8ebf1;
    --muted: #8b94a7;
    --faint: rgba(232, 235, 241, 0.12);
    --blue: #8fb8ff;
    --red: #f08a8a;
    --green: #8fd6a4;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); }
  body { font-family: var(--font); line-height: 1.45; min-height: 100vh; -webkit-text-size-adjust: 100%; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 40px; }
  h1 { font-size: 20px; font-weight: 500; margin: 0 0 6px; }
  #desc { margin: 0; color: var(--muted); font-size: 14px; }
  .scenario-row { display: flex; gap: 10px; align-items: center; margin: 16px 0 0; }
  .scenario-row label { color: var(--muted); font-size: 13px; }
  select {
    font: inherit; color: var(--ink); background: var(--panel);
    border: 1px solid var(--faint); border-radius: 8px; padding: 7px 10px;
  }
  #stageWrap {
    margin-top: 14px; background: var(--panel); border: 1px solid var(--faint);
    border-radius: 14px; overflow: hidden;
  }
  #stage { display: block; width: 100%; height: auto; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
  .btn {
    font: inherit; font-size: 14px; color: var(--ink); background: transparent;
    border: 1px solid var(--faint); border-radius: 999px; padding: 7px 14px; cursor: pointer;
    transition: border-color 0.2s ease, background 0.2s ease;
  }
  .btn:hover { border-color: rgba(232, 235, 241, 0.4); }
  .btn.primary { border-color: rgba(143, 184, 255, 0.6); color: var(--blue); min-width: 84px; }
  .btn:focus-visible, select:focus-visible, input[type=range]:focus-visible {
    outline: 2px solid var(--blue); outline-offset: 2px;
  }
  .speed { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
  .speed input { width: 120px; accent-color: #8fb8ff; }
  #speedLabel { min-width: 42px; font-variant-numeric: tabular-nums; }
  #readout { margin-left: auto; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
  #caption { margin: 16px 0 0; min-height: 3.2em; font-size: 16px; line-height: 1.5; }
  .keys { margin: 18px 0 0; color: var(--muted); font-size: 12.5px; }
  kbd {
    font-family: var(--mono); font-size: 11.5px; border: 1px solid var(--faint);
    border-bottom-width: 2px; border-radius: 5px; padding: 0 5px;
  }
  @media (max-width: 560px) {
    .wrap { padding: 20px 16px 32px; }
    #readout { margin-left: 0; width: 100%; }
    .speed input { width: 90px; }
    #caption { font-size: 15px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>A rate limiter, one sliding window at a time</h1>
    <p id="desc"></p>
  </header>

  <div class="scenario-row">
    <label for="scenario">Scenario</label>
    <select id="scenario"></select>
  </div>

  <div id="stageWrap"><svg id="stage" role="img" aria-label="Three lanes of sends on a sliding 600 second window"></svg></div>

  <div class="controls">
    <button class="btn primary" id="play" type="button" aria-pressed="false">Play</button>
    <button class="btn" id="back" type="button">Step back</button>
    <button class="btn" id="fwd" type="button">Step forward</button>
    <button class="btn" id="restart" type="button">Restart</button>
    <label class="speed">Speed
      <input id="speed" type="range" min="0.5" max="3" step="0.25" value="1">
      <span id="speedLabel">1&times;</span>
    </label>
    <span id="readout"></span>
  </div>

  <p id="caption" aria-live="polite"></p>

  <p class="keys"><kbd>space</kbd> play / pause &nbsp; <kbd>&larr;</kbd> <kbd>&rarr;</kbd> step back / forward</p>
</div>

<script>
(function () {
  "use strict";

  const TRACE = /*__TRACE_JSON__*/null;

  const NS = "http://www.w3.org/2000/svg";
  const LANES = ["user", "team", "company"];
  const COLOR = { user: "#8fb8ff", team: "#f2d27a", company: "#c9cfdb" };
  const RED = "#f08a8a";
  const GREEN = "#8fd6a4";
  const MUTED = "#8b94a7";
  const INK = "#e8ebf1";
  const GRID = "rgba(232, 235, 241, 0.14)";
  const reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  const svg = document.getElementById("stage");
  const wrap = document.getElementById("stageWrap");
  const ui = {
    scenario: document.getElementById("scenario"),
    desc: document.getElementById("desc"),
    play: document.getElementById("play"),
    back: document.getElementById("back"),
    fwd: document.getElementById("fwd"),
    restart: document.getElementById("restart"),
    speed: document.getElementById("speed"),
    speedLabel: document.getElementById("speedLabel"),
    readout: document.getElementById("readout"),
    caption: document.getElementById("caption")
  };

  // st.i is the event shown, st.p (0..1) how far the tween from the previous event has gone.
  const st = { sc: 0, i: 0, p: 1, playing: false, speed: 1, raf: 0, timer: 0, t0: 0, dur: 0 };

  function scn() { return TRACE.scenarios[st.sc]; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function lerp(a, b, k) { return a + (b - a) * k; }
  function ease(x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }
  function fmt(x) {
    return Math.abs(x - Math.round(x)) < 1e-6 ? String(Math.round(x)) : x.toFixed(1);
  }
  function el(name, attrs, parent, text) {
    const n = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) n.textContent = text;
    parent.appendChild(n);
    return n;
  }
  function pickStep(span, maxTicks) {
    const steps = [30, 60, 120, 300, 600, 900];
    for (let i = 0; i < steps.length; i++) {
      if (span / steps[i] <= maxTicks) return steps[i];
    }
    return steps[steps.length - 1];
  }
  function geometry(w) {
    const labelW = clamp(w * 0.25, 96, 150);
    return {
      w: w,
      h: 360,
      labelW: labelW,
      x0: labelW + 16,
      x1: w - 14,
      tokenY: 46,
      bracketTop: 96,
      laneY: [150, 216, 282],
      axisY: 320
    };
  }
  function domainOf(s) {
    let maxT = 0;
    s.events.forEach(function (e) { maxT = Math.max(maxT, e.t); });
    return { min: Math.min(0, maxT - TRACE.window_seconds) - 30, max: maxT + 60 };
  }

  function drawAxis(g, d, xOf) {
    el("line", { x1: g.x0, x2: g.x1, y1: g.axisY, y2: g.axisY, stroke: GRID, "stroke-width": 1 }, svg);
    el("text", { x: 12, y: g.axisY + 19, fill: MUTED, "font-size": 11 }, svg, "simulated time");
    const step = pickStep(d.max - d.min, g.w < 520 ? 5 : 9);
    for (let t = Math.ceil(d.min / step) * step; t <= d.max; t += step) {
      if (t < 0) continue;
      const x = xOf(t);
      if (x < g.x0 - 1 || x > g.x1 + 1) continue;
      el("line", { x1: x, x2: x, y1: g.axisY, y2: g.axisY + 5, stroke: GRID, "stroke-width": 1 }, svg);
      el("text", { x: x, y: g.axisY + 19, "text-anchor": "middle", fill: MUTED, "font-size": 11 }, svg, t + " s");
    }
  }

  function drawBracket(g, xOf, clock, W) {
    const left = xOf(clock - W);
    const right = xOf(clock);
    const l = clamp(left, g.x0, g.x1);
    const r = clamp(right, g.x0, g.x1);
    const edge = "rgba(143, 184, 255, 0.6)";
    el("rect", {
      x: l, y: g.bracketTop, width: Math.max(0, r - l), height: g.axisY - g.bracketTop,
      fill: "rgba(143, 184, 255, 0.07)"
    }, svg);
    if (left >= g.x0 - 0.5) {
      el("line", { x1: left, x2: left, y1: g.bracketTop, y2: g.axisY, stroke: edge, "stroke-width": 1 }, svg);
    }
    el("line", { x1: right, x2: right, y1: g.bracketTop, y2: g.axisY, stroke: edge, "stroke-width": 1 }, svg);
    el("line", { x1: l, x2: r, y1: g.bracketTop, y2: g.bracketTop, stroke: edge, "stroke-width": 1 }, svg);
    if (r - l > 120) {
      el("text", { x: l + 8, y: g.bracketTop + 16, fill: MUTED, "font-size": 11 }, svg, "last " + fmt(W) + " s");
    }
    el("text", { x: r - 8, y: g.bracketTop + 16, fill: INK, "font-size": 11, "text-anchor": "end" },
      svg, "now " + fmt(Math.round(clock)) + " s");
  }

  function drawLane(g, xOf, prev, ev, name, idx, e, W) {
    const y = g.laneY[idx];
    const color = COLOR[name];
    const key = ev.keys[name];
    const pKey = prev ? prev.keys[name] : null;
    const sameKey = !!prev && pKey === key;
    const cur = ev.snapshot[key] || [];
    const old = prev ? (prev.snapshot[pKey] || []) : [];
    const curN = cur.length;
    const n = lerp(prev ? old.length : 0, curN, e);
    const cap = ev.caps[name];
    const full = curN >= cap;
    const who = key.slice(key.indexOf(":") + 1);
    const mw = g.labelW - 24;

    // Label and capacity meter in the left column.
    el("text", { x: 12, y: y - 18, fill: MUTED, "font-size": 10, "letter-spacing": "1.2" }, svg, name.toUpperCase());
    el("text", { x: 12, y: y - 4, fill: INK, "font-size": 13 }, svg, who);
    el("rect", { x: 12, y: y + 6, width: mw, height: 6, rx: 3, fill: "rgba(232, 235, 241, 0.08)" }, svg);
    el("rect", {
      x: 12, y: y + 6, width: mw * clamp(n / cap, 0, 1), height: 6, rx: 3, fill: full ? RED : color
    }, svg);
    for (let c = 1; c < cap; c++) {
      const sx = 12 + mw * c / cap;
      el("line", { x1: sx, x2: sx, y1: y + 4, y2: y + 14, stroke: "#0f1115", "stroke-width": 1.5 }, svg);
    }
    el("text", { x: 12, y: y + 30, fill: full ? RED : MUTED, "font-size": 12 },
      svg, Math.round(n) + " of " + cap + (full ? " (full)" : ""));

    // The lane line, the dots, and their expiry ticks on the axis.
    el("line", { x1: g.x0, x2: g.x1, y1: y, y2: y, stroke: GRID, "stroke-width": 1 }, svg);
    const idOf = function (en) { return en[0] + "|" + en[1]; };
    const curSet = {};
    const oldSet = {};
    cur.forEach(function (en) { curSet[idOf(en)] = true; });
    old.forEach(function (en) { oldSet[idOf(en)] = true; });

    cur.forEach(function (en) {
      const isNew = !(sameKey && oldSet[idOf(en)]);
      const x = xOf(en[0]);
      const a = isNew ? e : 1;
      if (isNew && ev.outcome === "ALLOWED" && en[0] === ev.t && en[1] === ev.request_id) {
        el("circle", {
          cx: x, cy: y, r: 5.5 + 12 * e, fill: "none", stroke: GREEN,
          "stroke-width": 1.5, "stroke-opacity": 1 - e
        }, svg);
      }
      el("circle", { cx: x, cy: y, r: 5.5, fill: color, "fill-opacity": a }, svg);
      el("text", {
        x: x, y: y - 11, "text-anchor": "middle", fill: MUTED, "font-size": 10, "fill-opacity": a
      }, svg, en[1]);
      const xe = xOf(en[0] + W);
      if (xe >= g.x0 && xe <= g.x1) {
        el("line", {
          x1: xe, x2: xe, y1: g.axisY - 6, y2: g.axisY + 6, stroke: color,
          "stroke-width": 1.5, "stroke-opacity": 0.8
        }, svg);
      }
    });

    // Entries that were counted before and are gone now fade out and drift down.
    old.forEach(function (en) {
      if (sameKey && curSet[idOf(en)]) return;
      el("circle", {
        cx: xOf(en[0]), cy: y + 18 * e, r: 5.5, fill: color, "fill-opacity": 0.9 * (1 - e)
      }, svg);
    });
  }

  function drawRequest(g, xOf, ev, clock, k, e, W) {
    const tx = xOf(ev.t);
    const rejected = ev.outcome === "REJECTED";
    const blocked = ev.blocked_lanes || [];

    if (rejected) {
      // A wall on every full lane, and a countdown on the lane that sets retry_after.
      blocked.forEach(function (name) {
        const wy = g.laneY[LANES.indexOf(name)];
        el("line", {
          x1: tx, x2: tx, y1: wy - 18, y2: wy + 18, stroke: RED,
          "stroke-width": 2.5, "stroke-linecap": "round", "stroke-opacity": e
        }, svg);
      });
      if (ev.limiting_lane) {
        const li = LANES.indexOf(ev.limiting_lane);
        const snap = ev.snapshot[ev.keys[ev.limiting_lane]];
        const frees = snap[0][0] + W;
        const yb = g.laneY[li] + 12;
        const xs = clamp(xOf(clock), g.x0, g.x1);
        const xe = clamp(xOf(frees), g.x0, g.x1);
        el("line", {
          x1: xs, x2: xe, y1: yb, y2: yb, stroke: RED, "stroke-width": 3,
          "stroke-linecap": "round", "stroke-opacity": 0.85 * e
        }, svg);
        el("text", {
          x: xe, y: yb + 16, "text-anchor": xe - g.x0 < 80 ? "start" : "end",
          fill: RED, "font-size": 11, "fill-opacity": e
        }, svg, "retry in " + fmt(Math.round(Math.max(0, frees - clock))) + " s");
      }
    }

    // The request itself: a pill that drops onto the lanes, or bounces off the first full one.
    const target = rejected && blocked.length
      ? g.laneY[LANES.indexOf(blocked[0])] - 24
      : g.laneY[0] - 24;
    let y;
    let opacity = 1;
    if (rejected) {
      y = k < 0.5 ? lerp(g.tokenY, target, ease(k * 2)) : lerp(target, g.tokenY, ease((k - 0.5) * 2));
    } else {
      y = lerp(g.tokenY, target, ease(k));
      opacity = k < 0.75 ? 1 : Math.max(0, (1 - k) / 0.25);
    }
    const label = ev.user + " · " + ev.request_id + (ev.duplicate ? " (retry)" : "");
    const pw = 18 + label.length * 6.8;
    const ph = 22;
    const cx = clamp(tx, g.x0 + 60, g.x1 - 60);
    el("rect", {
      x: cx - pw / 2, y: y - ph / 2, width: pw, height: ph, rx: ph / 2,
      fill: "#0f1115", stroke: rejected ? RED : GREEN, "stroke-width": 1, opacity: opacity
    }, svg);
    el("text", {
      x: cx, y: y + 4, "text-anchor": "middle", fill: INK, "font-size": 12, opacity: opacity
    }, svg, label);
    if (rejected) {
      el("text", {
        x: cx, y: g.tokenY + 36, "text-anchor": "middle", fill: RED, "font-size": 12,
        opacity: clamp((k - 0.4) * 3, 0, 1)
      }, svg, "RATE_LIMITED · retry in " + fmt(ev.retry_after) + " s");
    }
  }

  function render() {
    const s = scn();
    const d = s.domain;
    const ev = s.events[st.i];
    const prev = st.i > 0 ? s.events[st.i - 1] : null;
    const k = st.p;
    const e = ease(k);
    const clock = prev ? lerp(prev.t, ev.t, e) : ev.t;
    const W = TRACE.window_seconds;
    const w = Math.max(280, Math.floor(wrap.clientWidth));
    const g = geometry(w);
    const xOf = function (t) { return g.x0 + (t - d.min) / (d.max - d.min) * (g.x1 - g.x0); };

    svg.setAttribute("width", w);
    svg.setAttribute("height", g.h);
    svg.setAttribute("viewBox", "0 0 " + w + " " + g.h);
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    drawAxis(g, d, xOf);
    drawBracket(g, xOf, clock, W);
    LANES.forEach(function (name, idx) { drawLane(g, xOf, prev, ev, name, idx, e, W); });
    drawRequest(g, xOf, ev, clock, k, e, W);

    ui.readout.textContent = "step " + (st.i + 1) + " of " + s.events.length + "  ·  t = " + fmt(Math.round(clock)) + " s";
    ui.caption.textContent = ev.caption;
  }

  function updateControls() {
    ui.play.textContent = st.playing ? "Pause" : "Play";
    ui.play.setAttribute("aria-pressed", String(st.playing));
    ui.speedLabel.textContent = String(st.speed) + "×";
  }

  function frame(now) {
    st.p = clamp((now - st.t0) / st.dur, 0, 1);
    render();
    if (st.p < 1) {
      st.raf = requestAnimationFrame(frame);
    } else {
      afterTween();
    }
  }

  function startTween() {
    cancelAnimationFrame(st.raf);
    clearTimeout(st.timer);
    st.dur = reduceMotion ? 0 : 900 / st.speed;
    st.p = 0;
    render();
    if (st.dur === 0) {
      st.p = 1;
      render();
      afterTween();
      return;
    }
    st.t0 = performance.now();
    st.raf = requestAnimationFrame(frame);
  }

  function afterTween() {
    if (!st.playing) return;
    if (st.i >= scn().events.length - 1) {
      pause();
      return;
    }
    st.timer = setTimeout(function () { goTo(st.i + 1); }, 1100 / st.speed);
  }

  function goTo(i) {
    st.i = clamp(i, 0, scn().events.length - 1);
    startTween();
  }

  function pause() {
    st.playing = false;
    cancelAnimationFrame(st.raf);
    clearTimeout(st.timer);
    updateControls();
  }

  function play() {
    if (st.playing) return;
    st.playing = true;
    updateControls();
    if (st.i >= scn().events.length - 1 && st.p >= 1) {
      goTo(0);
      return;
    }
    if (st.p < 1 && st.dur > 0) {
      st.t0 = performance.now() - st.p * st.dur;
      st.raf = requestAnimationFrame(frame);
      return;
    }
    st.timer = setTimeout(function () { goTo(st.i + 1); }, 400 / st.speed);
  }

  function togglePlay() {
    if (st.playing) pause(); else play();
  }

  function stepBy(delta) {
    pause();
    goTo(st.i + delta);
  }

  function selectScenario(idx) {
    pause();
    st.sc = idx;
    st.i = 0;
    st.p = 1;
    ui.desc.textContent = scn().description;
    render();
  }

  function clicked(fn) {
    return function (ev) {
      fn();
      ev.currentTarget.blur();
    };
  }

  // Build the controls.
  TRACE.scenarios.forEach(function (s, idx) {
    s.domain = domainOf(s);
    const o = document.createElement("option");
    o.value = String(idx);
    o.textContent = (idx + 1) + ". " + s.title;
    ui.scenario.appendChild(o);
  });
  ui.scenario.addEventListener("change", function () {
    selectScenario(Number(ui.scenario.value));
    ui.scenario.blur();
  });
  ui.play.addEventListener("click", clicked(togglePlay));
  ui.back.addEventListener("click", clicked(function () { stepBy(-1); }));
  ui.fwd.addEventListener("click", clicked(function () { stepBy(1); }));
  ui.restart.addEventListener("click", clicked(function () { pause(); goTo(0); }));
  ui.speed.addEventListener("input", function () {
    st.speed = Number(ui.speed.value);
    updateControls();
  });
  ui.speed.addEventListener("change", function () { ui.speed.blur(); });

  document.addEventListener("keydown", function (ev) {
    const tag = ev.target && ev.target.tagName;
    if (tag === "SELECT" || tag === "INPUT" || tag === "BUTTON" || tag === "TEXTAREA") return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === " ") {
      ev.preventDefault();
      togglePlay();
    } else if (ev.key === "ArrowRight") {
      ev.preventDefault();
      stepBy(1);
    } else if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      stepBy(-1);
    }
  });

  if (window.ResizeObserver) {
    new ResizeObserver(function () { render(); }).observe(wrap);
  } else {
    window.addEventListener("resize", render);
  }

  selectScenario(0);
  updateControls();
})();
</script>
</body>
</html>
"""


def main() -> None:
    trace = build_trace()

    trace_path = HERE / "trace.json"
    trace_path.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")

    if PLACEHOLDER not in HTML_TEMPLATE:
        raise SystemExit("HTML template is missing the trace placeholder")
    payload = json.dumps(trace, separators=(",", ":")).replace("</", "<\\/")
    html_path = HERE / "visual.html"
    html_path.write_text(HTML_TEMPLATE.replace(PLACEHOLDER, payload), encoding="utf-8")

    print_summary(trace)
    print(f"\nwrote {trace_path}")
    print(f"wrote {html_path}")


if __name__ == "__main__":
    main()
