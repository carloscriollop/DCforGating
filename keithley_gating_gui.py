# Keithley 2636B (TSP-only) - ID and IG vs VGS, VDS or time (stacked subplots)
#
# A (smua): drives VGS, measures IG and VGS
# B (smub): holds or sweeps VDS (output curves), measures ID and VDS
# TSP-only (no SCPI) to avoid -285/-286. One query per point.
#
# SWEEP ORDER: interleave_for_drift is wired into the sweep behind a checkbox
# and is OFF by default. See the INTERLEAVE note in CHANGELOG.md for when to
# turn it on.
#
# Design notes and the full revision history live in CHANGELOG.md.
#
# Requires: pyvisa, matplotlib, numpy, tkinter

import os
import re
import csv
import sys
import json
import time
import subprocess
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from typing import NamedTuple
from tkinter import ttk, messagebox, filedialog

import pyvisa
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# Written into every CSV and contact-check report, so each file says which
# version of this program produced it.
APP_VERSION = "2026-11-19.2"
APP_TITLE = f"2636B TSP - ID/IG vs VGS or VDS (A = gate, B = drain)   v{APP_VERSION}"
APP_SIZE = "1280x860"
LEFT_PANEL_W = 500
UI_POLL_MS = 50

PALETTE = {
    "bg": "#f7f7f9", "panel": "#ffffff", "border": "#e5e7eb", "text": "#1f2937",
    "muted": "#6b7280", "primary": "#2f4858", "primary2": "#3e5f74",
    "warn": "#b45309",
}

MODE_ZERO_SAFE = "Zero-safe branches"
MODE_HYSTERESIS = "Hysteresis loop"
MODE_SINGLE = "Single ramp"
MODE_TRACE = "Time trace"
MODE_OUTPUT = "Output curves"

SEG_LABELS = {
    MODE_ZERO_SAFE: ("Positive branch", "Negative branch"),
    MODE_HYSTERESIS: ("Forward", "Reverse"),
    MODE_SINGLE: ("Forward", "Reverse"),
    MODE_TRACE: ("Trace", "Unused"),
    # Output curves carry one segment label per VGS value instead.
    MODE_OUTPUT: ("Output", "Unused"),
}

# Output curves: a curve is called LINEAR when the slope changes by less
# than this many percent between VDS = 0 and VDS = +/-max, and the two VDS
# polarities differ by less than the same amount. A rule of thumb, not a
# statistical test: compare curves taken with the same settings.
OUTPUT_LINEAR_PCT = 2.0

# Time trace: ID is called stable when its rate of change over the last
# DRIFT_WINDOW_S seconds is below this many percent per minute. 0.2 %/min
# keeps two loops taken 5 min apart within about 1 % of each other.
DRIFT_STABLE_PCT_PER_MIN = 0.2
DRIFT_WINDOW_S = 60.0

# Sample name, data folder and autosave choice survive a restart in this
# file, next to the program. It is in .gitignore.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_settings.json")

# Measurement name used in file names, per sweep mode.
RUN_KINDS = {
    MODE_ZERO_SAFE: "transfer_zero_safe",
    MODE_HYSTERESIS: "transfer_hysteresis",
    MODE_SINGLE: "transfer_single",
    MODE_TRACE: "time_trace",
    MODE_OUTPUT: "output_curves",
}


def default_data_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=2)
    except Exception:
        pass


def safe_name(text, fallback="sample"):
    """Make a sample name safe for a file name: keep letters, digits, . _ -"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text).strip()).strip("._")
    return s or fallback


@dataclass
class Point:
    """
    One acquired sweep point.

    idx and t record WHEN the point was taken, not just what was applied.
    Without them a saved sweep cannot distinguish hysteresis from drift: a
    loop that fails to close may be charge trapping, or it may be the sample
    wandering while the sweep ran, and the two are told apart by whether the
    discrepancy tracks the sweep direction or the clock.
    """
    vgs: float
    ig: float
    idrain: float
    vds: float
    comp_a: bool
    comp_b: bool
    seg: str
    stitched: bool = False
    idx: int = -1            # position in the acquisition order, 0-based
    t: float = float("nan")  # seconds since the first point of this sweep


def apply_style(root):
    root.configure(bg=PALETTE["bg"])
    s = ttk.Style()
    s.theme_use("clam")
    s.configure("TLabel", background=PALETTE["panel"], foreground=PALETTE["text"])
    s.configure("TFrame", background=PALETTE["panel"])
    s.configure("TCheckbutton", background=PALETTE["panel"], foreground=PALETTE["text"])
    s.configure("TRadiobutton", background=PALETTE["panel"], foreground=PALETTE["text"])
    s.configure("Primary.TButton", background=PALETTE["primary"], foreground="white",
                bordercolor=PALETTE["primary"], padding=6)
    s.map("Primary.TButton", background=[("active", PALETTE["primary2"])])
    s.configure("Hint.TLabel", foreground=PALETTE["muted"])
    s.configure("Warn.TLabel", foreground=PALETTE["warn"])
    s.configure("Header.TLabel", background=PALETTE["bg"], foreground=PALETTE["text"],
                font=("Segoe UI", 16, "bold"))
    s.configure("TNotebook", background=PALETTE["panel"], bordercolor=PALETTE["border"])
    s.configure("TNotebook.Tab", padding=(10, 4))
    s.map("TNotebook.Tab",
          background=[("selected", PALETTE["primary"])],
          foreground=[("selected", "white")])


def list_keithley_resources(rm, timeout_ms=1000, include_serial=False):
    """
    Return the VISA resources that answer a TSP print(1).

    Serial ports (ASRL) are skipped by default: a PC can expose several COM
    ports, each probe waits for the full timeout, and the 2636B is on USB or
    GPIB here.
    """
    out = []
    for r in rm.list_resources():
        if not include_serial and r.upper().startswith("ASRL"):
            continue
        try:
            i = rm.open_resource(r, timeout=timeout_ms)
            i.read_termination = "\n"
            i.write_termination = "\n"
            i.query("print(1)")
            i.close()
            out.append(r)
        except Exception:
            pass
    return out


# ===== SWEEP BUILDERS =====

def _parse_sweep(start, stop, step, allow_equal=False):
    v0, v1, dv = float(start), float(stop), abs(float(step))
    if dv == 0:
        raise ValueError("Step cannot be zero.")
    if not allow_equal and v0 == v1:
        raise ValueError("Start and Stop cannot be equal.")
    return v0, v1, dv


def _arange_inclusive(v0, v1, dv):
    """v0 toward v1 in steps of dv, including v1 when it lies on the grid."""
    sgn = 1.0 if v1 >= v0 else -1.0
    vals = np.round(np.arange(v0, v1 + sgn * dv / 2, sgn * dv), 12)
    return [float(v) + 0.0 for v in vals]  # + 0.0 turns -0.0 into 0.0


def build_zero_safe_sweep(start, stop, step):
    """
    Always begins at 0 V, then:
        1) 0 -> +Vmax  (labelled as the positive branch)
        2) 0 -> Vmin   (labelled as the negative branch)
    """
    v0, v1, dv = _parse_sweep(start, stop, step, allow_equal=True)
    lab_pos, lab_neg = SEG_LABELS[MODE_ZERO_SAFE]
    v_lo, v_hi = min(v0, v1), max(v0, v1)
    pos = _arange_inclusive(0.0, v_hi, dv) if v_hi > 0 else [0.0]
    sweep = [(lab_pos, v) for v in pos]
    if v_lo < 0:
        sweep += [(lab_neg, v) for v in _arange_inclusive(0.0, v_lo, dv)]
    return sweep


def build_hysteresis_sweep(start, stop, step):
    """
    A true hysteresis loop: start -> stop, then back over the SAME grid.

    The reverse branch is the forward branch reversed, so every voltage is
    visited twice even when (stop - start) is not a multiple of step. The
    turning point is measured twice, back to back.
    """
    v0, v1, dv = _parse_sweep(start, stop, step)
    lab_f, lab_r = SEG_LABELS[MODE_HYSTERESIS]
    fwd = _arange_inclusive(v0, v1, dv)
    return [(lab_f, v) for v in fwd] + [(lab_r, v) for v in reversed(fwd)]


def build_single_sweep(start, stop, step):
    """A single monotonic ramp from start to stop."""
    v0, v1, dv = _parse_sweep(start, stop, step)
    lab_f, _ = SEG_LABELS[MODE_SINGLE]
    return [(lab_f, v) for v in _arange_inclusive(v0, v1, dv)]


BUILDERS = {
    MODE_ZERO_SAFE: build_zero_safe_sweep,
    MODE_HYSTERESIS: build_hysteresis_sweep,
    MODE_SINGLE: build_single_sweep,
}


def parse_vgs_list(text):
    """
    Parse the output-curve gate voltages, e.g. "-4, -2, 0, 2, 5".
    Commas, semicolons and spaces all separate values. Order is kept as
    typed, because with gate hysteresis the order changes the result.
    """
    parts = text.replace(";", " ").replace(",", " ").split()
    if not parts:
        raise ValueError("Enter at least one VGS value.")
    return [float(x) + 0.0 for x in parts]


def output_labels(vgs_list):
    """
    One segment label per VGS value. A value that appears again (to check
    that the first curve is reproduced) gets '#2', '#3', ... so its curve
    is plotted and reported on its own.
    """
    seen = {}
    out = []
    for v in vgs_list:
        seen[v] = seen.get(v, 0) + 1
        out.append(f"VGS {v:+g} V" + (f" #{seen[v]}" if seen[v] > 1 else ""))
    return out


def build_output_vds_grid(vmax, step):
    """
    VDS from -vmax to +vmax and back over the same grid.

    The sequence is a palindrome, so the applied VDS is uncorrelated with
    time. That lets fit_conductance() separate a slow drift of ID from the
    slope, which a one-way ramp cannot do (there, drift and slope are the
    same straight line in time). Adjacent points still differ by one step,
    so the gate capacitor is barely disturbed. The turning point at +vmax is
    measured twice, back to back, as in the hysteresis loop.
    """
    vmax, step = abs(float(vmax)), abs(float(step))
    if vmax == 0 or step == 0:
        raise ValueError("VDS max and VDS step must be non-zero.")
    if step > vmax:
        raise ValueError("VDS step must not exceed VDS max.")
    up = _arange_inclusive(-vmax, vmax, step)
    return up + list(reversed(up))


# ===== FITTING & ANALYSIS =====

def eng_ohms(r):
    """Format a resistance with an engineering prefix."""
    if r is None or not np.isfinite(r):
        return "n/a"
    a = abs(r)
    for scale, unit in ((1e12, "TOhm"), (1e9, "GOhm"), (1e6, "MOhm"), (1e3, "kOhm"), (1.0, "Ohm")):
        if a >= scale:
            return f"{r / scale:.4g} {unit}"
    return f"{r:.4g} Ohm"


class Fit(NamedTuple):
    """Result of a conductance fit."""
    g: float
    g_err: float
    i0: float
    r2: float
    drift: float
    drift_err: float


def interleave_for_drift(values):
    """
    Reorder a sweep so that the applied voltage is uncorrelated with time.
    Pairs the ends inward: 0, n-1, 1, n-2, 2, n-3, etc.
    """
    vals = list(values)
    out = []
    lo, hi = 0, len(vals) - 1
    while lo <= hi:
        out.append(vals[lo])
        if hi != lo:
            out.append(vals[hi])
        lo += 1
        hi -= 1
    return out


def fit_conductance(v, i, t=None):
    """
    Fit the measured current against the applied voltage.
    With timestamps, the model is i = g*v + drift*(t - t_mean) + i0.
    Without them it falls back to i = g*v + i0.
    """
    v = np.asarray(v, dtype=float)
    i = np.asarray(i, dtype=float)
    ok = np.isfinite(v) & np.isfinite(i)
    if t is not None:
        t = np.asarray(t, dtype=float)
        ok &= np.isfinite(t)
    v, i = v[ok], i[ok]
    t = t[ok] if t is not None else None

    nan = Fit(np.nan, np.nan, np.nan, np.nan, np.nan, np.nan)
    cols = [v, np.ones_like(v)] if t is None else [v, t - t.mean(), np.ones_like(v)]
    ncoef = len(cols)
    if v.size < ncoef + 1:
        return nan
    X = np.column_stack(cols)
    try:
        beta, *_ = np.linalg.lstsq(X, i, rcond=None)
        xtx_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return nan

    resid = i - X @ beta
    ss_res = float((resid ** 2).sum())
    dof = v.size - ncoef
    s2 = ss_res / dof if dof > 0 else np.nan

    def _se(k):
        if not (np.isfinite(s2) and s2 >= 0):
            return np.nan
        var = s2 * xtx_inv[k, k]
        return float(np.sqrt(var)) if np.isfinite(var) and var >= 0 else np.nan

    g = float(beta[0])
    drift = float(beta[1]) if t is not None else 0.0
    drift_err = _se(1) if t is not None else np.nan
    ss_tot = float(((i - i.mean()) ** 2).sum())
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else np.nan
    return Fit(g, _se(0), float(beta[-1]), r2, drift, drift_err)


def resistance_from_fit(fit, k_sigma=3.0):
    """Turn a fitted conductance into (resistance, readable string, resolved?)."""
    g, g_err = fit.g, fit.g_err
    if not np.isfinite(g):
        return np.nan, "n/a", False
    if np.isfinite(g_err) and g_err > 0 and abs(g) < k_sigma * g_err:
        return np.nan, f"> {eng_ohms(1.0 / (k_sigma * g_err))} (below noise floor)", False
    if g == 0:
        return np.nan, "open", False
    r = 1.0 / g
    if r < 0:
        return r, f"{eng_ohms(abs(r))} (NEGATIVE slope)", True
    return r, eng_ohms(r), True


def analyze_output_curve(vds, idrain, ig, t):
    """
    Summarize one output curve (ID against VDS at fixed VGS).

    g, r2, i0, drift: straight-line fit i = g*v + drift*(t - t_mean) + i0.
        g is the channel conductance at this VGS; i0 is the drain current
        at VDS = 0 (a pure resistor gives zero there).
    curv_pct: how much the slope at VDS = +/-max differs from the slope at
        VDS = 0, in percent. From a cubic fit, i = g1*v + g2*v^2 + g3*v^3
        plus the same drift term; the slope is g1 + 2*g2*v + 3*g3*v^2, so
        the symmetric change is 3*g3*vmax^2 / g1.
    asym_pct: difference between the +VDS and -VDS sides, 2*g2*vmax / g1.
    """
    v = np.asarray(vds, dtype=float)
    i = np.asarray(idrain, dtype=float)
    a = np.asarray(ig, dtype=float)
    tt = np.asarray(t, dtype=float)
    ok = np.isfinite(v) & np.isfinite(i) & np.isfinite(tt)
    v, i, tt = v[ok], i[ok], tt[ok]
    a = a[np.isfinite(a)]

    lin = fit_conductance(v, i, tt)
    res = {
        "n": int(v.size), "g": lin.g, "g_err": lin.g_err, "r2": lin.r2,
        "i0": lin.i0, "drift": lin.drift,
        "curv_pct": np.nan, "asym_pct": np.nan,
        "vmax": float(np.max(np.abs(v))) if v.size else np.nan,
        "id_max": float(np.max(np.abs(i))) if i.size else np.nan,
        "ig_mean": float(np.mean(a)) if a.size else np.nan,
        "ig_max": float(np.max(np.abs(a))) if a.size else np.nan,
    }
    if v.size >= 7 and np.isfinite(lin.g) and lin.g != 0:
        X = np.column_stack([v, v ** 2, v ** 3, tt - tt.mean(), np.ones_like(v)])
        try:
            beta, *_ = np.linalg.lstsq(X, i, rcond=None)
            g1, g2, g3 = (float(b) for b in beta[:3])
            vm = res["vmax"]
            if g1 != 0:
                res["curv_pct"] = 3.0 * g3 * vm ** 2 / g1 * 100.0
                res["asym_pct"] = 2.0 * g2 * vm / g1 * 100.0
        except np.linalg.LinAlgError:
            pass
    return res


def build_output_report(curves, p, complete=True):
    """
    Plain-text summary of a set of output curves.
    curves: list of (label, vgs_set, vds, id, ig, t), in measurement order.
    """
    rows = [(lab, vg, analyze_output_curve(vd, idr, ig, t))
            for lab, vg, vd, idr, ig, t in curves]

    L = []
    L.append("OUTPUT CURVES   " + time.strftime("%Y-%m-%d %H:%M:%S")
             + f"   (software v{APP_VERSION})")
    L.append(f"VDS -{p['vds_max']:g} V to +{p['vds_max']:g} V and back, step "
             f"{p['vds_step']:g} V, {p['delay_s']:g} s per point, NPLC {p['nplc']:g}")
    L.append(f"Settle at each VGS before its curve: {p['out_settle']:g} s (VDS = 0 V meanwhile)")
    L.append(f"Compliance: gate {p['comp_a']:g} A, drain {p['comp_b']:g} A")
    L.append("VGS order as measured: " + ", ".join(f"{vg:+g}" for _, vg, _ in rows))
    if not complete:
        L.append("** RUN DID NOT FINISH: the last curve may be partial.")
    L.append("")

    head = (f"  {'curve':<14} {'G (S)':>11} {'R':>12} {'R2 line':>9} "
            f"{'curv %':>7} {'asym %':>7} {'ID@VDS=0 (A)':>13} {'IG mean (A)':>12}  verdict")
    L.append(head)
    good = []
    for lab, vg, r in rows:
        g = r["g"]
        r_str = eng_ohms(1.0 / g) if np.isfinite(g) and g != 0 else "n/a"
        lin_ok = (np.isfinite(r["curv_pct"]) and np.isfinite(r["asym_pct"])
                  and abs(r["curv_pct"]) < OUTPUT_LINEAR_PCT
                  and abs(r["asym_pct"]) < OUTPUT_LINEAR_PCT)
        verdict = "LINEAR" if lin_ok else ("too few points" if r["n"] < 7 else "CHECK")
        L.append(f"  {lab:<14} {g:>11.4e} {r_str:>12} {r['r2']:>9.6f} "
                 f"{r['curv_pct']:>+7.2f} {r['asym_pct']:>+7.2f} "
                 f"{r['i0']:>+13.3e} {r['ig_mean']:>+12.3e}  {verdict}")
        if np.isfinite(g) and g > 0:
            good.append((g, lab))
    L.append("")

    if len(good) >= 2:
        g_hi, lab_hi = max(good)
        g_lo, lab_lo = min(good)
        L.append(f"On/off from slopes: G max / G min = {g_hi / g_lo:.3f}x "
                 f"({lab_hi} / {lab_lo})")
        L.append("  Independent of the transfer curve: each G uses the whole VDS range,")
        L.append("  not one VDS value, and drift during the curve is fitted out.")
        L.append("")

    L.append("How to read this")
    L.append("  G: slope of ID against VDS, the channel conductance at that VGS.")
    L.append("  curv %: how much the slope changes between VDS = 0 and VDS = +/-max.")
    L.append("    Near 0 means a straight line, i.e. ohmic contacts. A curvature that")
    L.append("    changes with VGS means the gate is acting on the contacts, not only")
    L.append("    on the channel.")
    L.append("  asym %: difference between the +VDS and -VDS sides. Near 0 means the")
    L.append("    two contacts behave alike.")
    L.append("  ID@VDS=0: drain current with no drain bias. It should be small next to")
    L.append("    the current at VDS max. If it follows IG, gate leakage reaches the drain.")
    L.append(f"  LINEAR: |curv| and |asym| both below {OUTPUT_LINEAR_PCT:g} %. A rule of thumb.")
    L.append("")
    L.append("Raw points: use Save CSV (one Segment label per VGS).")
    return "\n".join(L)


# ===== INSTRUMENT CONTROL =====

class K2636B_TSP:
    """TSP-only control for the 2636B. smua is the gate, smub is the drain."""

    # One simultaneous I/V conversion per channel. The results are assigned
    # to locals BEFORE print(): Lua truncates a multi-value call to its first
    # value unless it is the last argument, which is what made iv() look like
    # it returned only the current.
    Q_IV = ("local ia, va = smua.measure.iv() "
            "local ib, vb = smub.measure.iv() "
            "print(ia, va, ib, vb, "
            "smua.source.compliance, smub.source.compliance)")
    # Fallback: four separate conversions.
    Q_LEGACY = ("local ia, va = smua.measure.i(), smua.measure.v() "
                "local ib, vb = smub.measure.i(), smub.measure.v() "
                "print(ia, va, ib, vb, "
                "smua.source.compliance, smub.source.compliance)")

    def __init__(self, inst):
        self.k = inst
        self._q_point = self.Q_IV

    @property
    def read_mode(self):
        return "simultaneous iv()" if self._q_point == self.Q_IV else "legacy i() then v()"

    def _w(self, cmd):
        self.k.write(cmd)

    def _q(self, cmd):
        return self.k.query(cmd).strip()

    def drain_errors(self, max_n=20):
        """Read and remove everything in the instrument error queue."""
        errs = []
        try:
            n = int(float(self._q("print(errorqueue.count)")))
        except Exception:
            return errs
        for _ in range(min(n, max_n)):
            try:
                errs.append(self._q("local c, m = errorqueue.next() print(c, m)"))
            except Exception:
                break
        return errs

    def init_instrument(self, comp_a, comp_b, nplc, use_4w_b, vds_const,
                        fix_source_range=False, vgs_max=1.0,
                        ramp_step=0.02, ramp_delay=0.01):
        """
        Configure both SMUs, check the error queue, then enable the outputs
        and soft-ramp the drain to vds_const. Raises without enabling the
        outputs if the configuration produced any instrument error.
        """
        w = self._w

        w("if abort ~= nil then abort() end")
        w("if waitcomplete ~= nil then waitcomplete() end")
        w("errorqueue.clear()")

        w("smua.reset()")
        w("smua.source.func = smua.OUTPUT_DCVOLTS")
        if fix_source_range:
            w("smua.source.autorangev = smua.AUTORANGE_OFF")
            w(f"smua.source.rangev = {abs(float(vgs_max))}")
        else:
            w("smua.source.autorangev = smua.AUTORANGE_ON")
        w(f"smua.measure.nplc = {float(nplc)}")
        w("smua.measure.autozero = smua.AUTOZERO_AUTO")
        w(f"smua.source.limiti = {float(comp_a)}")
        w("smua.sense = smua.SENSE_LOCAL")
        w("smua.source.levelv = 0")

        w("smub.reset()")
        w("smub.source.func = smub.OUTPUT_DCVOLTS")
        w("smub.source.autorangev = smub.AUTORANGE_ON")
        w(f"smub.measure.nplc = {float(nplc)}")
        w("smub.measure.autozero = smub.AUTOZERO_AUTO")
        w(f"smub.source.limiti = {float(comp_b)}")
        w("smub.sense = smub.SENSE_REMOTE" if use_4w_b else "smub.sense = smub.SENSE_LOCAL")
        w("smub.source.levelv = 0")

        errs = self.drain_errors()
        if errs:
            raise RuntimeError("Instrument reported errors during configuration, "
                               "outputs left OFF:\n" + "\n".join(errs))

        w("smua.source.output = smua.OUTPUT_ON")
        w("smub.source.output = smub.OUTPUT_ON")

        self.ramp_smub_to(vds_const, ramp_step, ramp_delay)

    def set_vgs(self, v):
        self._w(f"smua.source.levelv = {float(v)}")

    def set_vds(self, v):
        self._w(f"smub.source.levelv = {float(v)}")

    @staticmethod
    def _parse_point(s):
        vs = s.replace(",", " ").split()
        if len(vs) < 6:
            raise RuntimeError(f"Bad read, expected 6 fields, got: {s}")
        ig, vgs, idrain, vds = (float(x) for x in vs[:4])
        comp_a = vs[4].strip().lower() == "true"
        comp_b = vs[5].strip().lower() == "true"
        return vgs, ig, idrain, vds, comp_a, comp_b

    def read_point(self):
        """One query per point. Returns (vgs, ig, idrain, vds, comp_a, comp_b)."""
        try:
            return self._parse_point(self._q(self._q_point))
        except (RuntimeError, ValueError):
            if self._q_point == self.Q_LEGACY:
                raise
            # iv() answer did not parse: switch for the rest of the session.
            self._q_point = self.Q_LEGACY
            self.drain_errors()
            return self._parse_point(self._q(self._q_point))

    def _read_level(self, node):
        """
        The programmed source level. Reading it costs no A/D conversion,
        unlike measure.v(), which with autozero on takes several line cycles
        and needs the output enabled to mean anything.
        """
        return float(self._q(f"print({node}.source.levelv)"))

    def _ramp(self, node, target_v, step_v, delay_s):
        target_v = float(target_v)
        step_v = abs(float(step_v)) or 0.02
        delay_s = float(delay_s)
        try:
            v_now = self._read_level(node)
        except Exception:
            v_now = 0.0
        nsteps = int(max(1, np.ceil(abs(target_v - v_now) / step_v)))
        for i in range(1, nsteps + 1):
            v_next = v_now + (target_v - v_now) * (i / nsteps)
            self._w(f"{node}.source.levelv = {v_next}")
            time.sleep(delay_s)

    def ramp_smua_to(self, target_v, step_v, delay_s):
        self._ramp("smua", target_v, step_v, delay_s)

    def ramp_smub_to(self, target_v, step_v, delay_s):
        self._ramp("smub", target_v, step_v, delay_s)

    def outputs_off(self):
        try:
            self._w("smua.source.output = smua.OUTPUT_OFF")
            self._w("smub.source.output = smub.OUTPUT_OFF")
        except Exception:
            pass

    def zero_and_off(self, ramp_step=0.02, ramp_delay=0.01):
        """Ramp both SMUs to 0 V, then turn the outputs off. Never raises."""
        for fn in (self.ramp_smua_to, self.ramp_smub_to):
            try:
                fn(0.0, ramp_step, ramp_delay)
            except Exception:
                pass
        self.outputs_off()


# ===== APPLICATION =====

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(APP_SIZE)
        self.minsize(1020, 600)
        apply_style(self)

        self.rm = None
        self.k = None
        self.dev = None
        self.data = []
        self.stop_flag = threading.Event()
        self.sweep_thread = None
        self.detect_thread = None
        self.ui_q = queue.Queue()
        self.active_mode = MODE_ZERO_SAFE
        self.active_interleave = False
        self.seg_labels = SEG_LABELS[MODE_ZERO_SAFE]
        self.run_params = {}

        self.var_log_id = tk.BooleanVar(value=False)
        self.var_log_ig = tk.BooleanVar(value=False)
        self.var_clip_log = tk.BooleanVar(value=False)
        self.var_show_sum = tk.BooleanVar(value=False)
        self.ax_sum = None
        # Output curves: one (ID, IG, ID+IG) line triple per VGS label.
        self.out_lines = {}
        # The last measurement started: what to save and under which name.
        self.run_info = None
        self.settings = load_settings()

        self._build_ui()
        self.after(UI_POLL_MS, self._poll_ui)

    # ===== WORKER -> GUI HANDOFF =====

    def _post(self, item):
        """Thread-safe: queue a Point or a zero-argument callable for the GUI."""
        self.ui_q.put(item)

    def _status(self, msg):
        self._post(lambda m=msg: self.var_status.set(m))

    def _error(self, title, msg):
        self._post(lambda t=title, m=msg: messagebox.showerror(t, m))

    def _poll_ui(self):
        """Drain the worker queue on the GUI thread; redraw at most once per poll."""
        new_points = False
        try:
            while True:
                item = self.ui_q.get_nowait()
                if isinstance(item, Point):
                    self.data.append(item)
                    new_points = True
                else:
                    try:
                        item()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        if new_points:
            self.update_plot()
        self.after(UI_POLL_MS, self._poll_ui)

    # ===== UI =====

    def _build_ui(self):
        ttk.Label(self, text="CNT gating, Keithley 2636B (TSP)",
                  style="Header.TLabel").pack(side=tk.TOP, anchor="w", padx=18, pady=(16, 8))

        card = tk.Frame(self, bg=PALETTE["panel"], highlightthickness=1)
        card.config(highlightbackground=PALETTE["border"])
        card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=8)

        leftcol = tk.Frame(card, bg=PALETTE["panel"])
        leftcol.pack(side=tk.LEFT, fill=tk.Y, padx=(16, 0), pady=16)
        # Fixed bar above the scrolling panel, so Start and Stop are always in view.
        actbar = tk.Frame(leftcol, bg=PALETTE["panel"], highlightthickness=1,
                          width=LEFT_PANEL_W + 18)
        actbar.config(highlightbackground=PALETTE["border"])
        actbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        holder = tk.Frame(leftcol, bg=PALETTE["panel"])
        holder.pack(side=tk.TOP, fill=tk.Y, expand=True)
        self.left_canvas = tk.Canvas(holder, bg=PALETTE["panel"], highlightthickness=0,
                                     width=LEFT_PANEL_W, borderwidth=0)
        left_sb = ttk.Scrollbar(holder, orient="vertical", command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=left_sb.set)
        left_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.left_canvas.pack(side=tk.LEFT, fill=tk.Y, expand=False)

        left = tk.Frame(self.left_canvas, bg=PALETTE["panel"])
        self.left_canvas.create_window((0, 0), window=left, anchor="nw", width=LEFT_PANEL_W)
        left.bind("<Configure>",
                  lambda e: self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all")))

        sep = tk.Frame(card, width=1, bg=PALETTE["border"])
        sep.pack(side=tk.LEFT, fill=tk.Y, padx=(12, 0), pady=16)
        right = tk.Frame(card, bg=PALETTE["panel"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 16), pady=16)

        def section(parent, title):
            box = tk.Frame(parent, bg=PALETTE["panel"], highlightthickness=1)
            box.config(highlightbackground=PALETTE["border"])
            box.pack(fill=tk.X, pady=6)
            head = tk.Frame(box, bg=PALETTE["panel"])
            head.pack(fill=tk.X, padx=10, pady=(8, 2))
            ttk.Label(head, text=title).pack(side=tk.LEFT)
            body = tk.Frame(box, bg=PALETTE["panel"])
            body.pack(fill=tk.X, padx=10, pady=(2, 10))
            return body

        def entry(parent, label, var, row, col, pady=(0, 0)):
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", pady=pady)
            ttk.Entry(parent, textvariable=var, width=10).grid(
                row=row, column=col + 1, sticky="w", padx=(6, 14), pady=pady)

        # Connection
        conn = section(left, "1. Connection (TSP-only)")
        self.var_resource = tk.StringVar()
        self.cmb_resource = ttk.Combobox(conn, textvariable=self.var_resource, width=42)
        self.cmb_resource.grid(row=0, column=0, columnspan=3, sticky="we", padx=(0, 6))
        ttk.Button(conn, text="Detect", command=self.on_detect).grid(row=0, column=3, sticky="e")
        ttk.Button(conn, text="Connect", style="Primary.TButton",
                   command=self.on_connect).grid(row=0, column=4, sticky="e")
        self.lbl_mode = ttk.Label(conn, text="Expecting TSP (native 2600).", style="Hint.TLabel")
        self.lbl_mode.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        for c in range(5):
            conn.grid_columnconfigure(c, weight=1)

        # Sample and data folder
        sav = section(left, "2. Sample and data folder")
        self.var_sample = tk.StringVar(value=str(self.settings.get("sample", "sample")))
        self.var_folder = tk.StringVar(value=str(self.settings.get("folder", default_data_dir())))
        self.var_autosave = tk.BooleanVar(value=bool(self.settings.get("autosave", True)))
        ttk.Label(sav, text="Sample name").grid(row=0, column=0, sticky="w")
        ttk.Entry(sav, textvariable=self.var_sample, width=24).grid(
            row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(sav, text="Data folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(sav, textvariable=self.var_folder, width=40).grid(
            row=1, column=1, sticky="we", padx=(6, 6), pady=(6, 0))
        ttk.Button(sav, text="Browse", command=self.on_browse_folder).grid(
            row=1, column=2, sticky="e", pady=(6, 0))
        ttk.Checkbutton(sav, text="Auto-save CSV, PNG and summary at the end of each run",
                        variable=self.var_autosave).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(sav, text="Files are named sample_measurement_date_time, so nothing is "
                            "overwritten.",
                  style="Hint.TLabel", wraplength=440, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        sav.grid_columnconfigure(1, weight=1)

        # Measurement: one tab per kind of measurement
        msec = section(left, "3. Measurement (pick a tab, then Start above)")
        self.nb = ttk.Notebook(msec)
        self.nb.pack(fill=tk.X)

        def tab(title):
            outer = tk.Frame(self.nb, bg=PALETTE["panel"])
            self.nb.add(outer, text=title)
            inner = tk.Frame(outer, bg=PALETTE["panel"])
            inner.pack(fill=tk.X, padx=8, pady=8)
            for c in range(6):
                inner.grid_columnconfigure(c, weight=1)
            return inner

        def hint(parent, text, row, span=6):
            ttk.Label(parent, text=text, style="Hint.TLabel", wraplength=430,
                      justify="left").grid(row=row, column=0, columnspan=span,
                                           sticky="w", pady=(6, 0))

        # Shared by the Transfer and Time trace tabs (one variable, two boxes).
        self.var_vds = tk.StringVar(value="0.05")

        # Tab: transfer curve (VGS sweep)
        prm = tab("Transfer")
        self.var_start = tk.StringVar(value="-0.5")
        self.var_stop = tk.StringVar(value="0.5")
        self.var_step = tk.StringVar(value="0.02")
        self.var_sweep_mode = tk.StringVar(value=MODE_ZERO_SAFE)
        # Off by default: interleaving pairs the extremes, so consecutive
        # points jump the full range. Worth it for a fitted slope, not for the
        # shape of a curve.
        self.var_interleave = tk.BooleanVar(value=False)
        self.var_ramp_wait = tk.StringVar(value="10")

        entry(prm, "VGS start (V)", self.var_start, 0, 0)
        entry(prm, "VGS stop (V)", self.var_stop, 0, 2)
        entry(prm, "VGS step (V)", self.var_step, 0, 4)
        entry(prm, "VDS (V)", self.var_vds, 1, 0, pady=(6, 0))
        entry(prm, "Settle after ramp (s)", self.var_ramp_wait, 1, 2, pady=(6, 0))

        ttk.Label(prm, text="Mode").grid(row=2, column=0, sticky="w", pady=(8, 0))
        for i, m in enumerate((MODE_ZERO_SAFE, MODE_HYSTERESIS, MODE_SINGLE)):
            ttk.Radiobutton(prm, text=m, value=m, variable=self.var_sweep_mode,
                            command=self._on_mode_change).grid(
                row=3 + i // 2, column=(i % 2) * 3, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Checkbutton(prm, text="Interleave sweep order (for slope fits, not for curve shape)",
                        variable=self.var_interleave,
                        command=self._on_mode_change).grid(
            row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))

        self.lbl_mode_hint = ttk.Label(prm, text="", style="Hint.TLabel", wraplength=430, justify="left")
        self.lbl_mode_hint.grid(row=6, column=0, columnspan=6, sticky="w", pady=(6, 0))
        hint(prm, "Settling check: run the same sweep at 0.2 s and at 1.0 s per point. If the "
                  "curves differ, you are measuring transients, not steady state.", 7)

        # Tab: output curves
        out = tab("Output curves")
        self.var_out_vgs = tk.StringVar(value="-4, -2, 0, 2, 5")
        self.var_out_vmax = tk.StringVar(value="0.2")
        self.var_out_step = tk.StringVar(value="0.02")
        # Long by default: after a gate step, ID on the HfO2 back-gated
        # samples keeps creeping (29 % loop hysteresis on sample 2), and a
        # curve taken while ID creeps mixes drift into the slope.
        self.var_out_settle = tk.StringVar(value="60")
        ttk.Label(out, text="VGS values (V)").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.var_out_vgs, width=30).grid(
            row=0, column=1, columnspan=5, sticky="we", padx=(6, 0))
        entry(out, "VDS max +/- (V)", self.var_out_vmax, 1, 0, pady=(6, 0))
        entry(out, "VDS step (V)", self.var_out_step, 1, 2, pady=(6, 0))
        entry(out, "Settle per VGS (s)", self.var_out_settle, 2, 0, pady=(6, 0))
        hint(out, "For each VGS, in the order typed: drain to 0 V, gate ramps to VGS, waits "
                  "Settle, then VDS goes from -max to +max and back. Repeat a value at the end "
                  "(e.g. -4, 0, 5, -4) to check the first curve is reproduced. Straight lines "
                  "through the origin whose slope changes with VGS mean the gate is modulating "
                  "the channel. A summary with G, linearity and on/off opens at the end.", 3)

        # Tab: time trace
        trc = tab("Time trace")
        self.var_trace_vgs = tk.StringVar(value="0.0")
        self.var_trace_dur = tk.StringVar(value="600")
        self.var_trace_int = tk.StringVar(value="2.0")
        entry(trc, "VGS hold (V)", self.var_trace_vgs, 0, 0)
        entry(trc, "VDS (V)", self.var_vds, 0, 2)
        entry(trc, "Duration (s)", self.var_trace_dur, 1, 0, pady=(6, 0))
        entry(trc, "Interval (s)", self.var_trace_int, 1, 2, pady=(6, 0))
        hint(trc, "Holds VGS and VDS and plots ID and IG against time. Stop ends it early; the "
                  "data so far is still saved. The status bar turns to STABLE when ID drifts "
                  f"less than {DRIFT_STABLE_PCT_PER_MIN:g} %/min.", 2)

        # Tab: contact check
        chk = tab("Contact check")
        self.var_chk_amp = tk.StringVar(value="0.1")
        self.var_chk_pts = tk.StringVar(value="11")
        # Own settle time, independent of the sweep's Delay per point. The
        # check jumps between +amp and -amp without ramping, and a large-area
        # back gate is a capacitor of order 0.1 uF with a charging time
        # constant near 1 s. At 0.2 s per point the charging current, which
        # has the sign of the applied voltage, would be fitted as leakage.
        self.var_chk_settle = tk.StringVar(value="10")
        entry(chk, "Amplitude +/- (V)", self.var_chk_amp, 0, 0)
        entry(chk, "Points per sweep", self.var_chk_pts, 0, 2)
        entry(chk, "Settle per point (s)", self.var_chk_settle, 1, 0, pady=(6, 0))
        hint(chk, "R_GS, R_GD and R_DS from two small bipolar sweeps. Run this FIRST on a new "
                  "sample, and again on a sample that has been stored. Keep Settle near ten "
                  "gate time constants (10 s on the back-gated HfO2 samples). Gives a summary "
                  "only, no plot.", 2)

        # The Start button runs whichever tab is open.
        self._tab_runs = [
            ("Start transfer curve", self.on_start),
            ("Start output curves", self.on_output_curves),
            ("Start time trace", self.on_time_trace),
            ("Start contact check", self.on_contact_check),
        ]
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

        # Fill the fixed action bar (built above, before the scrolling panel)
        act = tk.Frame(actbar, bg=PALETTE["panel"])
        act.pack(fill=tk.X, padx=10, pady=8)
        self.btn_start = ttk.Button(act, text=self._tab_runs[0][0], style="Primary.TButton",
                                    command=self.on_run_selected)
        self.btn_start.grid(row=0, column=0, sticky="we")
        ttk.Button(act, text="Stop", command=self.on_stop).grid(row=0, column=1, sticky="we", padx=(6, 0))
        ttk.Button(act, text="Save now", command=self.on_save_now).grid(row=0, column=2, sticky="we", padx=(6, 0))
        ttk.Button(act, text="Open data folder", command=self.on_open_folder).grid(
            row=0, column=3, sticky="we", padx=(6, 0))
        self.var_saved = tk.StringVar(value="Nothing saved yet in this session.")
        ttk.Label(act, textvariable=self.var_saved, style="Hint.TLabel", wraplength=470,
                  justify="left").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        act.grid_columnconfigure(0, weight=3)
        for c in (1, 2, 3):
            act.grid_columnconfigure(c, weight=1)

        # Instrument settings shared by every measurement
        meas = section(left, "4. Instrument settings (all measurements)")
        self.var_compA = tk.StringVar(value="1e-6")
        self.var_compB = tk.StringVar(value="1e-3")
        self.var_nplc = tk.StringVar(value="1.0")
        self.var_delay = tk.StringVar(value="0.2")
        self.var_4w = tk.BooleanVar(value=False)
        self.var_fix_range = tk.BooleanVar(value=True)
        self.var_stop_on_comp = tk.BooleanVar(value=True)
        self.var_ramp_step = tk.StringVar(value="0.02")
        self.var_ramp_delay = tk.StringVar(value="0.01")

        entry(meas, "Comp A (IG) (A)", self.var_compA, 0, 0)
        entry(meas, "Comp B (ID) (A)", self.var_compB, 0, 2)
        entry(meas, "NPLC", self.var_nplc, 1, 0, pady=(6, 0))
        entry(meas, "Delay per point (s)", self.var_delay, 1, 2, pady=(6, 0))
        entry(meas, "Ramp step (V)", self.var_ramp_step, 2, 0, pady=(6, 0))
        entry(meas, "Ramp delay (s)", self.var_ramp_delay, 2, 2, pady=(6, 0))
        ttk.Checkbutton(meas, text="Fix gate source range", variable=self.var_fix_range).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(meas, text="Abort on compliance", variable=self.var_stop_on_comp).grid(
            row=3, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(meas, text="4-wire on drain only", variable=self.var_4w).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for c in range(4):
            meas.grid_columnconfigure(c, weight=1)

        # Plot options
        scale = section(left, "5. Plot options")
        ttk.Checkbutton(scale, text="Log |ID| (top)", variable=self.var_log_id,
                        command=self.apply_scales).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(scale, text="Log |IG| (bottom)", variable=self.var_log_ig,
                        command=self.apply_scales).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(scale, text="Clip zeros for log", variable=self.var_clip_log,
                        command=self.apply_scales).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(scale, text="Show ID+IG panel (leak-corrected)", variable=self.var_show_sum,
                        command=self._toggle_sum_panel).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(scale, text="Off by default: on a well-insulated gate this trace is identical to "
                              "ID. The CSV keeps the ID + IG column either way.",
                  style="Hint.TLabel", wraplength=440, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for c in range(2):
            scale.grid_columnconfigure(c, weight=1)

        # Plot
        plot = tk.Frame(right, bg=PALETTE["panel"], highlightthickness=1)
        plot.config(highlightbackground=PALETTE["border"])
        plot.pack(fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(6.2, 5.0))
        self._build_axes()

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Status
        bar = tk.Frame(self, bg=PALETTE["panel"], highlightthickness=1)
        bar.config(highlightbackground=PALETTE["border"])
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 12))
        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.var_status).pack(side=tk.LEFT, padx=10, pady=6)
        self.var_suppression = tk.StringVar(value="Leak check: waiting for data.")
        ttk.Label(bar, textvariable=self.var_suppression, style="Warn.TLabel").pack(
            side=tk.RIGHT, padx=10, pady=6)

        self._on_mode_change()
        self._refresh_legends()

    def _on_mode_change(self):
        m = self.var_sweep_mode.get()
        hints = {
            MODE_ZERO_SAFE:
                "Two branches from 0 V: 0 to +max, then 0 to min. Gate never steps to finite "
                "bias. NOT hysteresis.",
            MODE_HYSTERESIS:
                "True loop: Start to Stop to Start over the same grid. For charge trapping. "
                "Gate soft-ramps to Start first.",
            MODE_SINGLE:
                "One ramp from Start to Stop, exactly as entered.",
        }
        if self.var_interleave.get():
            extra = ("\nINTERLEAVED: points are reordered so bias sign is uncorrelated with "
                     "time, which keeps drift out of a fitted slope. Cost: consecutive points "
                     "jump the full range, so the per-point delay must be long enough to settle "
                     "after a large step, and the dielectric is cycled hard. It also does not "
                     "fully work on a rectifying gate, where |V| still falls monotonically with "
                     "index and the even part of I(V) aliases into the drift term.")
        else:
            extra = ("\nMonotonic order: adjacent points differ by one Step, so settling is "
                     "easiest and the gate is stressed least. Use this when the curve shape is "
                     "what matters. Any slow drift will appear as slope, so do not read a "
                     "fitted resistance off this ordering.")
        self.lbl_mode_hint.config(text=hints[m] + extra)

    def _build_axes(self):
        """
        Lay out two panels, or three when the leak-corrected trace is enabled.

        The third panel plots ID + IG. Current that leaves the gate and lands
        on the drain appears in ID with the sign opposite to IG, because a SMU
        reads positive current when it sources out of HI, so the sum cancels
        that shared path. It is a subtraction, not a measurement: a leak to
        source or substrate does not cancel. Read a residual as an upper bound
        on real channel modulation.
        """
        show_sum = bool(self.var_show_sum.get())
        n = 3 if show_sum else 2
        self.fig.clear()
        gs = self.fig.add_gridspec(n, 1, height_ratios=[1] * n, hspace=0.18)

        self.ax_id = self.fig.add_subplot(gs[0, 0])
        self.ax_ig = self.fig.add_subplot(gs[1, 0], sharex=self.ax_id)
        axes = [self.ax_id, self.ax_ig]

        if show_sum:
            self.ax_sum = self.fig.add_subplot(gs[2, 0], sharex=self.ax_id)
            self.ax_sum.set_ylabel("ID + IG (A)")
            axes.append(self.ax_sum)
        else:
            self.ax_sum = None

        for ax in axes:
            ax.set_facecolor("#fcfcfd")
            ax.grid(True, color="#eaecef")
        axes[-1].set_xlabel("VGS (V)")

        def pair(ax):
            a, = ax.plot([], [], marker="o", linestyle="-", linewidth=1.6)
            b, = ax.plot([], [], marker="s", linestyle="-", linewidth=1.6)
            return a, b

        self.line_id_a, self.line_id_b = pair(self.ax_id)
        self.line_ig_a, self.line_ig_b = pair(self.ax_ig)
        self.line_sum_a, self.line_sum_b = pair(self.ax_sum) if show_sum else (None, None)
        # The figure was cleared, so any per-VGS lines are gone too.
        self.out_lines = {}
        self._apply_scale_settings()

    def _apply_scale_settings(self):
        log_id, log_ig = self.var_log_id.get(), self.var_log_ig.get()
        self.ax_id.set_yscale("log" if log_id else "linear")
        self.ax_ig.set_yscale("log" if log_ig else "linear")
        self.ax_id.set_ylabel("|ID| (A)" if log_id else "ID (A)")
        self.ax_ig.set_ylabel("|IG| (A)" if log_ig else "IG (A)")
        # ID + IG changes sign, so that panel always stays linear.

    def _toggle_sum_panel(self):
        """Rebuild the layout, then redraw whatever data is already collected."""
        self._build_axes()
        self._refresh_legends()
        self.update_plot()

    def _refresh_legends(self):
        a, b = self.seg_labels
        trace = (self.active_mode == MODE_TRACE)
        output = (self.active_mode == MODE_OUTPUT)

        def lab(q, s):
            # A label starting with "_" is hidden from the legend. In output
            # mode the fixed pair is unused; the per-VGS lines carry labels.
            if output or (trace and s == b):
                return "_nolegend_"
            return f"{q} ({s})"

        self.line_id_a.set_label(lab("ID", a))
        self.line_id_b.set_label(lab("ID", b))
        self.line_ig_a.set_label(lab("IG", a))
        self.line_ig_b.set_label(lab("IG", b))
        axes = [self.ax_id, self.ax_ig]
        if self.ax_sum is not None:
            self.line_sum_a.set_label(lab("ID+IG", a))
            self.line_sum_b.set_label(lab("ID+IG", b))
            axes.append(self.ax_sum)
        axes[-1].set_xlabel("Time (s)" if trace else "VDS (V)" if output else "VGS (V)")
        for ax in axes:
            handles, labels = ax.get_legend_handles_labels()
            if not handles:
                # Output mode before the first curve: nothing labelled yet.
                if ax.get_legend() is not None:
                    ax.get_legend().remove()
                continue
            leg = ax.legend(frameon=True, fontsize=8)
            leg.get_frame().set_edgecolor(PALETTE["border"])
            leg.get_frame().set_facecolor("#ffffff")
        if hasattr(self, "canvas"):
            self.canvas.draw_idle()

    # ===== PARAMETERS =====

    def _busy(self):
        if self.sweep_thread and self.sweep_thread.is_alive():
            messagebox.showwarning("Notice", "A measurement is already running.")
            return True
        return False

    def _read_common(self):
        """Read every shared numeric entry on the GUI thread. Raises ValueError."""
        p = {
            "comp_a": float(self.var_compA.get()),
            "comp_b": float(self.var_compB.get()),
            "nplc": float(self.var_nplc.get()),
            "delay_s": float(self.var_delay.get()),
            "ramp_step": float(self.var_ramp_step.get()),
            "ramp_delay": float(self.var_ramp_delay.get()),
            "use4": bool(self.var_4w.get()),
            "fix_range": bool(self.var_fix_range.get()),
        }
        if p["comp_a"] <= 0 or p["comp_b"] <= 0:
            raise ValueError("Compliance must be positive.")
        if p["delay_s"] < 0 or p["ramp_delay"] < 0:
            raise ValueError("Delays cannot be negative.")
        return p

    # ===== INSTRUMENT =====

    def _get_rm(self):
        if self.rm is None:
            self.rm = pyvisa.ResourceManager()
        return self.rm

    def on_detect(self):
        """
        Probe the VISA resources in a background thread. Each probe can wait
        a full timeout, and on the GUI thread that froze the window.
        Refused while a measurement runs: probing would send commands to the
        instrument in the middle of it.
        """
        if self._busy():
            return
        if self.detect_thread and self.detect_thread.is_alive():
            return
        self.var_status.set("Detecting instruments...")

        def work():
            try:
                res = list_keithley_resources(self._get_rm())
            except Exception as e:
                self._error("Error", f"VISA error:\n{e}")
                self._status("Detection failed.")
                return

            def show(res=res):
                self.cmb_resource["values"] = res
                if res:
                    self.var_resource.set(res[0])
                self.var_status.set(f"Detected {len(res)} resource(s).")
            self._post(show)

        self.detect_thread = threading.Thread(target=work, daemon=True)
        self.detect_thread.start()

    def on_connect(self):
        if self._busy():
            return
        if self.detect_thread and self.detect_thread.is_alive():
            messagebox.showinfo("Notice", "Detection still running, try again in a moment.")
            return
        res = self.var_resource.get().strip()
        if not res:
            messagebox.showwarning("Notice", "Select or enter a VISA resource.")
            return
        if self.k is not None:
            try:
                self.k.close()
            except Exception:
                pass
            self.k = self.dev = None
        try:
            self.k = self._get_rm().open_resource(res, timeout=20000)
            self.k.read_termination = "\n"
            self.k.write_termination = "\n"
            try:
                model = self.k.query("print(localnode.model)").strip()
            except Exception as e:
                raise RuntimeError("Did not respond in TSP. Switch to TSP mode.") from e
            self.lbl_mode.config(text=f"Connected: {model}")
            self.dev = K2636B_TSP(self.k)
            self.var_status.set("Connected (TSP).")
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect:\n{e}")

    def on_start(self):
        if not self.dev:
            messagebox.showwarning("Notice", "Connect first (TSP).")
            return
        if self._busy():
            return

        try:
            p = self._read_common()
            p["vds"] = float(self.var_vds.get())
            p["v_start"] = float(self.var_start.get())
            p["v_stop"] = float(self.var_stop.get())
            p["v_step"] = abs(float(self.var_step.get()))
            p["ramp_wait"] = max(0.0, float(self.var_ramp_wait.get()))
            p["stop_on_comp"] = bool(self.var_stop_on_comp.get())
        except Exception as e:
            messagebox.showerror("Error", f"Invalid numeric entry:\n{e}")
            return

        if p["comp_a"] > 1e-5 and not messagebox.askyesno(
                "Confirm gate compliance",
                f"Gate compliance is {p['comp_a']:g} A (high for a leaky gate).\nContinue?"):
            return

        mode = self.var_sweep_mode.get()
        try:
            sweep = BUILDERS[mode](p["v_start"], p["v_stop"], p["v_step"])
        except Exception as e:
            messagebox.showerror("Error", f"Invalid sweep:\n{e}")
            return

        # Permuting the index list keeps each segment label attached to its
        # own voltage, so per-branch plotting and zero-point stitching work.
        interleaved = bool(self.var_interleave.get())
        if interleaved:
            sweep = [sweep[i] for i in interleave_for_drift(range(len(sweep)))]

        self.active_mode = mode
        self.active_interleave = interleaved
        self.seg_labels = SEG_LABELS[mode]
        self.run_params = dict(p, mode=mode, n_points=len(sweep),
                               started=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.data.clear()
        self._build_axes()  # drops per-VGS lines left by an output-curve run
        self._refresh_legends()
        self.update_plot()

        self._begin_run(RUN_KINDS[mode])
        self.stop_flag.clear()
        self.var_status.set("Configuring instrument...")
        self.sweep_thread = threading.Thread(target=self._sweep_worker,
                                             args=(sweep, p, mode, interleaved),
                                             daemon=True)
        self.sweep_thread.start()

    @staticmethod
    def _is_close0(x, eps=1e-9):
        try:
            return abs(float(x)) <= eps
        except Exception:
            return False

    def _sweep_worker(self, sweep, p, mode, interleaved):
        """
        Runs entirely off the GUI thread and never touches tkinter: results go
        back through self._post(). Every wait is stop_flag.wait(), which
        returns True as soon as Stop is pressed.
        """
        dev = self.dev
        wait = self.stop_flag.wait
        stitch = (mode == MODE_ZERO_SAFE)
        neg_label = SEG_LABELS[mode][1]
        halt_reason = None
        zero_sample = None
        configured = False
        try:
            dev.init_instrument(
                p["comp_a"], p["comp_b"], p["nplc"], p["use4"], p["vds"],
                fix_source_range=p["fix_range"],
                vgs_max=max(abs(p["v_start"]), abs(p["v_stop"])),
                ramp_step=p["ramp_step"], ramp_delay=p["ramp_delay"])
            configured = True

            # Let the dielectric finish charging BEFORE the first point: the
            # IG excess over its steady value decays with a time constant near
            # 1 s on this stack.
            v_first = float(sweep[0][1])
            dev.ramp_smua_to(v_first, p["ramp_step"], p["ramp_delay"])
            self._status(f"Settling {p['ramp_wait']:g} s at VGS = {v_first:g} V...")
            if wait(p["ramp_wait"]):
                return

            order_note = "interleaved" if interleaved else "monotonic"
            self._status(f"{mode}: {len(sweep)} points, {p['delay_s']:g} s each "
                         f"({order_note} order).")

            # Where the gate PHYSICALLY sits. Compared against the next target
            # voltage, not the segment label; see BRANCH TRANSITIONS in
            # CHANGELOG.md.
            last_v = v_first
            jump = 1.5 * p["v_step"]
            t0 = time.time()
            for idx, (seg, v) in enumerate(sweep):
                if self.stop_flag.is_set():
                    break

                stitched = (stitch and seg == neg_label and zero_sample is not None
                            and self._is_close0(v))
                if stitched:
                    reading = zero_sample
                else:
                    if not interleaved and abs(v - last_v) > jump:
                        dev.ramp_smua_to(v, p["ramp_step"], p["ramp_delay"])
                        if wait(p["ramp_wait"]):
                            break
                    else:
                        dev.set_vgs(v)
                    last_v = float(v)
                    if wait(p["delay_s"]):
                        break
                    reading = dev.read_point()
                    if stitch and zero_sample is None and self._is_close0(v):
                        zero_sample = reading

                vgs, ig, idr, vds, ca, cb = reading
                self._post(Point(vgs, ig, idr, vds, ca, cb, seg, stitched,
                                 idx=idx, t=time.time() - t0))

                if (ca or cb) and p["stop_on_comp"]:
                    which = ("gate (A) and drain (B)" if ca and cb
                             else "gate (A)" if ca else "drain (B)")
                    halt_reason = (f"Aborted at VGS = {vgs:.4g} V: {which} in compliance. "
                                   f"IG = {ig:.4g} A, ID = {idr:.4g} A.")
                    break

        except Exception as e:
            title = "Acquisition failed" if configured else "Configure failed"
            self._error("Error", f"{title}:\n{e}")
            halt_reason = halt_reason or f"{title}."
        finally:
            if configured:
                dev.zero_and_off(p["ramp_step"], p["ramp_delay"])
            if halt_reason:
                msg = halt_reason
            elif self.stop_flag.is_set():
                msg = "Sweep stopped."
            else:
                msg = "Sweep finished."
            self._status(msg + (" Outputs off." if configured else ""))
            if halt_reason and halt_reason.startswith("Aborted"):
                self._post(lambda m=halt_reason: messagebox.showwarning("Compliance", m))
            self._post(self._run_done)

    def on_stop(self):
        self.stop_flag.set()
        self.var_status.set("Stopping, ramping to 0 V.")

    # ===== TIME TRACE =====

    def on_time_trace(self):
        if not self.dev:
            messagebox.showwarning("Notice", "Connect first (TSP).")
            return
        if self._busy():
            return
        try:
            p = self._read_common()
            p["vds"] = float(self.var_vds.get())
            p["vgs_hold"] = float(self.var_trace_vgs.get())
            p["duration"] = float(self.var_trace_dur.get())
            p["interval"] = float(self.var_trace_int.get())
            p["stop_on_comp"] = bool(self.var_stop_on_comp.get())
        except Exception as e:
            messagebox.showerror("Error", f"Invalid numeric entry:\n{e}")
            return
        if p["duration"] <= 0 or p["interval"] <= 0:
            messagebox.showerror("Error", "Duration and interval must be positive.")
            return

        self.active_mode = MODE_TRACE
        self.active_interleave = False
        self.seg_labels = SEG_LABELS[MODE_TRACE]
        self.run_params = dict(p, mode=MODE_TRACE,
                               started=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.data.clear()
        self._build_axes()  # drops per-VGS lines left by an output-curve run
        self._refresh_legends()
        self.update_plot()

        self._begin_run(RUN_KINDS[MODE_TRACE])
        self.stop_flag.clear()
        self.var_status.set("Configuring instrument...")
        self.sweep_thread = threading.Thread(target=self._trace_worker, args=(p,), daemon=True)
        self.sweep_thread.start()

    def _trace_worker(self, p):
        """
        Hold VGS and VDS, log one point every p['interval'] seconds for
        p['duration'] seconds. Same thread rules as the sweep worker: no
        tkinter here, everything goes back through self._post().

        Points are scheduled on a fixed clock (t0 + k * interval) rather than
        "interval after the previous point", so a slow reading on a low
        current range does not stretch the whole trace.
        """
        dev = self.dev
        label = SEG_LABELS[MODE_TRACE][0]
        halt_reason = None
        configured = False
        try:
            dev.init_instrument(
                p["comp_a"], p["comp_b"], p["nplc"], p["use4"], p["vds"],
                fix_source_range=p["fix_range"],
                vgs_max=max(abs(p["vgs_hold"]), 0.2),
                ramp_step=p["ramp_step"], ramp_delay=p["ramp_delay"])
            configured = True
            dev.ramp_smua_to(p["vgs_hold"], p["ramp_step"], p["ramp_delay"])
            self._status(f"Time trace: VGS = {p['vgs_hold']:g} V, VDS = {p['vds']:g} V, "
                         f"{p['duration']:g} s, one point every {p['interval']:g} s.")

            t0 = time.time()
            idx = 0
            while not self.stop_flag.is_set():
                vgs, ig, idr, vds, ca, cb = dev.read_point()
                t_now = time.time() - t0
                self._post(Point(vgs, ig, idr, vds, ca, cb, label, False, idx=idx, t=t_now))
                if (ca or cb) and p["stop_on_comp"]:
                    which = ("gate (A) and drain (B)" if ca and cb
                             else "gate (A)" if ca else "drain (B)")
                    halt_reason = (f"Aborted at t = {t_now:.0f} s: {which} in compliance. "
                                   f"IG = {ig:.4g} A, ID = {idr:.4g} A.")
                    break
                idx += 1
                t_next = idx * p["interval"]
                if t_next > p["duration"]:
                    break
                if self.stop_flag.wait(max(0.0, t_next - (time.time() - t0))):
                    break
        except Exception as e:
            title = "Time trace failed" if configured else "Configure failed"
            self._error("Error", f"{title}:\n{e}")
            halt_reason = f"{title}."
        finally:
            if configured:
                dev.zero_and_off(p["ramp_step"], p["ramp_delay"])
            if halt_reason:
                msg = halt_reason
            elif self.stop_flag.is_set():
                msg = "Time trace stopped."
            else:
                msg = "Time trace finished."
            self._status(msg + (" Outputs off." if configured else ""))
            if halt_reason and halt_reason.startswith("Aborted"):
                self._post(lambda m=halt_reason: messagebox.showwarning("Compliance", m))
            self._post(self._run_done)

    # ===== OUTPUT CURVES =====

    def on_output_curves(self):
        if not self.dev:
            messagebox.showwarning("Notice", "Connect first (TSP).")
            return
        if self._busy():
            return
        try:
            p = self._read_common()
            vgs_list = parse_vgs_list(self.var_out_vgs.get())
            p["vds_max"] = abs(float(self.var_out_vmax.get()))
            p["vds_step"] = abs(float(self.var_out_step.get()))
            p["out_settle"] = float(self.var_out_settle.get())
            p["stop_on_comp"] = bool(self.var_stop_on_comp.get())
            grid = build_output_vds_grid(p["vds_max"], p["vds_step"])
        except Exception as e:
            messagebox.showerror("Error", f"Invalid entry:\n{e}")
            return
        if p["out_settle"] < 0:
            messagebox.showerror("Error", "Settle per VGS cannot be negative.")
            return
        if p["comp_a"] > 1e-5 and not messagebox.askyesno(
                "Confirm gate compliance",
                f"Gate compliance is {p['comp_a']:g} A (high for a leaky gate).\nContinue?"):
            return

        labels = output_labels(vgs_list)
        p["vgs_list"] = vgs_list
        self.active_mode = MODE_OUTPUT
        self.active_interleave = False
        self.seg_labels = SEG_LABELS[MODE_OUTPUT]
        self.run_params = dict(p, mode=MODE_OUTPUT, n_points=len(grid) * len(vgs_list),
                               started=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.data.clear()
        self._build_axes()
        self._refresh_legends()
        self.update_plot()

        minutes = len(vgs_list) * (p["out_settle"] + len(grid) * p["delay_s"]) / 60.0
        self._begin_run(RUN_KINDS[MODE_OUTPUT])
        self.stop_flag.clear()
        self.var_status.set(f"Output curves: {len(vgs_list)} VGS values x {len(grid)} points "
                            f"(about {minutes:.1f} min). Configuring instrument...")
        self.sweep_thread = threading.Thread(target=self._output_worker,
                                             args=(vgs_list, labels, grid, p), daemon=True)
        self.sweep_thread.start()

    def _output_worker(self, vgs_list, labels, grid, p):
        """
        Same thread rules as the sweep worker: no tkinter here, everything
        goes back through self._post(). The drain returns to 0 V before
        every gate move, so the gate always settles with no drain bias and
        each curve starts from the same state.
        """
        dev = self.dev
        wait = self.stop_flag.wait
        rs, rd = p["ramp_step"], p["ramp_delay"]
        n = len(vgs_list)
        curves = []
        halt_reason = None
        configured = False
        finished = False
        try:
            dev.init_instrument(
                p["comp_a"], p["comp_b"], p["nplc"], p["use4"], 0.0,
                fix_source_range=p["fix_range"],
                vgs_max=max(max(abs(v) for v in vgs_list), 0.2),
                ramp_step=rs, ramp_delay=rd)
            configured = True

            t0 = time.time()
            idx = 0
            for k, (vg, label) in enumerate(zip(vgs_list, labels), start=1):
                if self.stop_flag.is_set():
                    break
                dev.ramp_smub_to(0.0, rs, rd)
                dev.ramp_smua_to(vg, rs, rd)
                self._status(f"Output curve {k}/{n}: VGS = {vg:+g} V, settling "
                             f"{p['out_settle']:g} s with VDS = 0...")
                if wait(p["out_settle"]):
                    break
                dev.ramp_smub_to(grid[0], rs, rd)
                self._status(f"Output curve {k}/{n}: VGS = {vg:+g} V, sweeping VDS "
                             f"+/-{p['vds_max']:g} V...")

                vds_l, id_l, ig_l, t_l = [], [], [], []
                for v in grid:
                    dev.set_vds(v)
                    if wait(p["delay_s"]):
                        break
                    vgs_m, ig, idr, vds_m, ca, cb = dev.read_point()
                    t_now = time.time() - t0
                    self._post(Point(vgs_m, ig, idr, vds_m, ca, cb, label, False,
                                     idx=idx, t=t_now))
                    idx += 1
                    vds_l.append(vds_m)
                    id_l.append(idr)
                    ig_l.append(ig)
                    t_l.append(t_now)
                    if (ca or cb) and p["stop_on_comp"]:
                        which = ("gate (A) and drain (B)" if ca and cb
                                 else "gate (A)" if ca else "drain (B)")
                        halt_reason = (f"Aborted at VGS = {vg:+g} V, VDS = {vds_m:.4g} V: "
                                       f"{which} in compliance. IG = {ig:.4g} A, ID = {idr:.4g} A.")
                        break
                if vds_l:
                    curves.append((label, vg, vds_l, id_l, ig_l, t_l))
                if halt_reason or self.stop_flag.is_set():
                    break
            else:
                finished = True

            if curves:
                report = build_output_report(curves, p, complete=finished)
                self._post(lambda r=report: self._set_report(r, "Output curves"))

        except Exception as e:
            title = "Output curves failed" if configured else "Configure failed"
            self._error("Error", f"{title}:\n{e}")
            halt_reason = halt_reason or f"{title}."
        finally:
            if configured:
                dev.zero_and_off(rs, rd)
            if halt_reason:
                msg = halt_reason
            elif finished:
                msg = "Output curves finished."
            else:
                msg = "Output curves stopped."
            self._status(msg + (" Outputs off." if configured else ""))
            if halt_reason and halt_reason.startswith("Aborted"):
                self._post(lambda m=halt_reason: messagebox.showwarning("Compliance", m))
            self._post(self._run_done)

    # ===== CONTACT CHECK =====

    def on_contact_check(self):
        if not self.dev:
            messagebox.showwarning("Notice", "Connect first.")
            return
        if self._busy():
            return
        try:
            p = self._read_common()
            amp = abs(float(self.var_chk_amp.get()))
            pts = int(self.var_chk_pts.get())
            p["chk_settle"] = float(self.var_chk_settle.get())
        except Exception as e:
            messagebox.showerror("Error", f"Invalid entry:\n{e}")
            return
        if amp <= 0 or pts < 4:
            messagebox.showerror("Error", "Amplitude > 0 and points >= 4.")
            return
        if p["chk_settle"] < 0:
            messagebox.showerror("Error", "Settle per point cannot be negative.")
            return

        volts = np.linspace(-amp, amp, pts).tolist()
        self._begin_run("contact_check", has_plot=False)
        self.stop_flag.clear()
        minutes = 2 * pts * p["chk_settle"] / 60.0
        self.var_status.set(f"Contact check: 2x {pts} pts at +/-{amp:g} V, "
                            f"{p['chk_settle']:g} s each (about {minutes:.1f} min).")
        self.sweep_thread = threading.Thread(
            target=self._check_worker, args=(volts, amp, p), daemon=True)
        self.sweep_thread.start()

    def _check_worker(self, volts, amp, p):
        dev = self.dev
        order = interleave_for_drift(volts)
        configured = False

        settle = p["chk_settle"]
        n = len(order)

        def run(step_no, which, set_fn, ramp_fn):
            v_ax, ia, ib, ts = [], [], [], []
            comp_a = comp_b = False
            ramp_fn(order[0], p["ramp_step"], p["ramp_delay"])
            t0 = time.time()
            for k, v in enumerate(order, start=1):
                set_fn(v)
                self._status(f"Contact check step {step_no}/2 ({which} swept): "
                             f"point {k}/{n} at {v:+.3g} V, settling {settle:g} s...")
                if self.stop_flag.wait(settle):
                    return None
                vgs, ig, idr, vds, ca, cb = dev.read_point()
                v_ax.append(vgs if which == "gate" else vds)
                ia.append(ig)
                ib.append(idr)
                ts.append(time.time() - t0)
                comp_a, comp_b = comp_a or ca, comp_b or cb
            ramp_fn(0.0, p["ramp_step"], p["ramp_delay"])
            return v_ax, ia, ib, ts, comp_a, comp_b

        msg = "Contact check stopped."
        try:
            dev.init_instrument(p["comp_a"], p["comp_b"], p["nplc"], p["use4"], 0.0,
                                fix_source_range=p["fix_range"], vgs_max=amp,
                                ramp_step=p["ramp_step"], ramp_delay=p["ramp_delay"])
            configured = True
            r1 = run(1, "gate", dev.set_vgs, dev.ramp_smua_to)
            r2 = run(2, "drain", dev.set_vds, dev.ramp_smub_to) if r1 is not None else None
            if r2 is not None:
                report = self._build_check_report(r1, r2, amp, p)
                self._post(lambda r=report: self._set_report(r, "Contact check"))
                msg = "Contact check finished."
        except Exception as e:
            self._error("Error", f"Check failed:\n{e}")
            msg = "Contact check failed."
        finally:
            if configured:
                dev.zero_and_off(p["ramp_step"], p["ramp_delay"])
            # Reported only after the outputs are actually off.
            self._status(msg + (" Outputs off." if configured else ""))
            self._post(self._run_done)

    @staticmethod
    def _build_check_report(r1, r2, amp, p):
        vg, ia1, ib1, t1, ca1, cb1 = r1
        vd, ia2, ib2, t2, ca2, cb2 = r2
        comp_a, comp_b = p["comp_a"], p["comp_b"]

        f_g1 = fit_conductance(vg, ia1, t1)                            # all gate current
        f_gd1 = fit_conductance(vg, [-x for x in ib1], t1)
        f_gs = fit_conductance(vg, [a + b for a, b in zip(ia1, ib1)], t1)
        f_b2 = fit_conductance(vd, ib2, t2)
        f_gd2 = fit_conductance(vd, [-x for x in ia2], t2)
        f_ds = fit_conductance(vd, [a + b for a, b in zip(ia2, ib2)], t2)

        _, s_g, ok_g = resistance_from_fit(f_g1)
        _, s_gs, _ = resistance_from_fit(f_gs)
        r_gd1, s_gd1, ok_gd1 = resistance_from_fit(f_gd1)
        r_gd2, s_gd2, ok_gd2 = resistance_from_fit(f_gd2)
        _, s_ds, _ = resistance_from_fit(f_ds)
        ig_max = float(np.nanmax(np.abs(ia1))) if ia1 else np.nan

        L = []
        L.append("CONTACT CHECK   " + time.strftime("%Y-%m-%d %H:%M:%S")
                 + f"   (software v{APP_VERSION})")
        L.append(f"Amplitude +/-{amp:g} V, {len(vg)} points, {p['chk_settle']:g} s settle "
                 f"per point, NPLC {p['nplc']:g}")
        L.append(f"Compliance: gate {comp_a:g} A, drain {comp_b:g} A")
        L.append("")
        L.append("Step 1: gate swept, drain at 0 V")
        L.append(f"  R_GS = {s_gs}    R2 = {f_gs.r2:.6f}")
        L.append(f"  R_GD = {s_gd1}    R2 = {f_gd1.r2:.6f}")
        L.append(f"  Offset at 0V = {f_g1.i0:+.4e} A,  max |IG| = {ig_max:.4e} A")
        if ca1:
            L.append(f"  ** GATE IN COMPLIANCE: the numbers above are clamped, not measured.")
            L.append(f"     Total gate-to-channel resistance is below about "
                     f"{eng_ohms(amp / comp_a)}.")
        if cb1:
            L.append("  ** Drain in compliance during the gate sweep: large gate-to-drain current.")
        L.append("")
        L.append("Step 2: drain swept, gate at 0 V")
        L.append(f"  R_DS = {s_ds}    R2 = {f_ds.r2:.6f}")
        L.append(f"  R_GD = {s_gd2}    R2 = {f_gd2.r2:.6f}")
        L.append(f"  Offset at 0V = {f_b2.i0:+.4e} A")
        if cb2:
            L.append(f"  ** DRAIN IN COMPLIANCE: R_DS is below about {eng_ohms(amp / comp_b)}.")
        if ca2:
            L.append("  ** Gate in compliance during the drain sweep.")
        L.append("")
        L.append("Summary")
        L.append(f"  R_GS = {s_gs}")
        L.append(f"  R_GD = {s_gd1}  (forward)")
        L.append(f"         {s_gd2}  (reverse)")
        L.append(f"  R_DS = {s_ds}")
        if ok_gd1 and ok_gd2:
            spread = abs(r_gd1 - r_gd2) / max(abs(r_gd1), abs(r_gd2)) * 100.0
            L.append(f"  R_GD spread: {spread:.1f}%")
        L.append("")
        # Plain-language verdict on the gate. No area normalisation: compare
        # samples measured with the same pads and the same settings.
        if ca1:
            verdict = ("SHORTED. Current hit compliance at small bias. Expected if there is "
                       "no dielectric, a pinhole under the gate pad, or the probe went "
                       "through the film.")
        elif not ok_g:
            verdict = (f"INSULATING at +/-{amp:g} V: gate current is below the noise floor "
                       f"(total gate resistance {s_g}).")
        else:
            verdict = (f"FINITE LEAKAGE: total gate resistance {s_g}, max |IG| "
                       f"{ig_max:.2e} A. Compare against the other sample with the same "
                       f"pads before deciding.")
            # Charging current of the gate capacitor has the sign of the
            # applied step, so it fits as a conductance, and the drain sweep
            # charges the same capacitor, so the two R_GD values agree even
            # when both are artifacts. Only the settle time separates them:
            # real leakage does not depend on it, charging current does.
            verdict += (f" Not yet distinguished from gate charging current: repeat with "
                        f"twice the settle per point ({2 * p['chk_settle']:g} s). If the "
                        f"resistance stays the same, it is leakage. If it rises, this run "
                        f"was still reading charging current.")
        L.append("Gate dielectric: " + verdict)

        # Raw readings in acquisition order, so the report alone is enough to
        # redo the fits later without rerunning the check.
        L.append("")
        L.append("Raw data (acquisition order)")
        L.append(f"  {'step':>4} {'idx':>4} {'t_s':>9} {'V_swept_V':>11} {'IG_A':>12} {'ID_A':>12}")
        for step, (vx, ia, ib, ts) in ((1, (vg, ia1, ib1, t1)), (2, (vd, ia2, ib2, t2))):
            for k, (v, a, b, t) in enumerate(zip(vx, ia, ib, ts)):
                L.append(f"  {step:>4} {k:>4} {t:>9.2f} {v:>+11.5f} {a:>+12.4e} {b:>+12.4e}")
        return "\n".join(L)

    def _show_report(self, text, title="Contact check"):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("820x480")
        frame = tk.Frame(win, bg=PALETTE["panel"])
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        txt = tk.Text(frame, wrap="none", font=("Consolas", 10))
        ysb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ysb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        txt.insert("1.0", text)
        txt.configure(state="disabled")

        bar = tk.Frame(win, bg=PALETTE["panel"])
        bar.pack(fill=tk.X, padx=10, pady=(0, 10))

        def copy():
            self.clipboard_clear()
            self.clipboard_append(text)
            self.var_status.set("Report copied.")

        ttk.Button(bar, text="Copy", command=copy).pack(side=tk.LEFT)
        ttk.Label(bar, text=f"Saved with this run as {self._run_basename()}_summary.txt "
                            "(automatically, or with Save now).",
                  style="Hint.TLabel").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side=tk.RIGHT)

    # ===== PLOTTING =====

    def _seg(self, label):
        return [p for p in self.data if p.seg == label]

    def _prep_for_log(self, arr):
        arr = np.abs(np.asarray(arr, dtype=float))
        arr[arr <= 0] = 1e-18 if self.var_clip_log.get() else np.nan
        return arr

    @staticmethod
    def _mask_zero(vx, *ys, eps=1e-12):
        if getattr(vx, "size", 0):
            idx = np.where(np.isclose(vx, 0.0, atol=eps))[0]
            if idx.size:
                i = idx[0]
                vx[i] = np.nan
                for y in ys:
                    y[i] = np.nan
        return (vx,) + ys

    def apply_scales(self):
        self._apply_scale_settings()
        self.update_plot()

    def _branch_arrays(self, pts):
        """
        Arrays for one branch, sorted along x: x, ID, IG, ID+IG (plot copies
        only). x is VGS for a sweep and elapsed time for a time trace.
        """
        if self.active_mode == MODE_TRACE:
            vx = np.asarray([p.t for p in pts], dtype=float)
        else:
            vx = np.asarray([p.vgs for p in pts], dtype=float)
        idr = np.asarray([p.idrain for p in pts], dtype=float)
        ig = np.asarray([p.ig for p in pts], dtype=float)
        s = idr + ig
        if self.var_log_id.get():
            idr = self._prep_for_log(idr)
        if self.var_log_ig.get():
            ig = self._prep_for_log(ig)
        order = np.argsort(vx)
        return vx[order], idr[order], ig[order], s[order]

    def _output_line_triple(self, label):
        """Create, on first use, the ID / IG / ID+IG lines for one VGS."""
        if label not in self.out_lines:
            color = f"C{len(self.out_lines) % 10}"
            kw = dict(marker="o", markersize=3, linestyle="-", linewidth=1.3, color=color)
            lid, = self.ax_id.plot([], [], label=label, **kw)
            lig, = self.ax_ig.plot([], [], label=label, **kw)
            lsum = None
            if self.ax_sum is not None:
                lsum, = self.ax_sum.plot([], [], label=label, **kw)
            self.out_lines[label] = (lid, lig, lsum)
        return self.out_lines[label]

    def _update_output_plot(self):
        """
        One line per VGS, x = measured VDS, in ACQUISITION order (not sorted),
        so the -max -> +max -> -max loop shows: two branches that sit on top of
        each other mean no drift or hysteresis during the curve.
        """
        for ln in (self.line_id_a, self.line_id_b, self.line_ig_a, self.line_ig_b,
                   self.line_sum_a, self.line_sum_b):
            if ln is not None:
                ln.set_data([], [])

        groups = {}
        for p in self.data:
            groups.setdefault(p.seg, []).append(p)

        n_before = len(self.out_lines)
        xs, ids, igs, sums = [], [], [], []
        for label, pts in groups.items():
            lid, lig, lsum = self._output_line_triple(label)
            vx = np.asarray([p.vds for p in pts], dtype=float)
            idr = np.asarray([p.idrain for p in pts], dtype=float)
            ig = np.asarray([p.ig for p in pts], dtype=float)
            s = idr + ig
            if self.var_log_id.get():
                idr = self._prep_for_log(idr)
            if self.var_log_ig.get():
                ig = self._prep_for_log(ig)
            lid.set_data(vx, idr)
            lig.set_data(vx, ig)
            if lsum is not None:
                lsum.set_data(vx, s)
            xs.append(vx)
            ids.append(idr)
            igs.append(ig)
            sums.append(s)

        if len(self.out_lines) != n_before:
            self._refresh_legends()
        if xs:
            self._autoscale(self.ax_id, xs, ids)
            self._autoscale(self.ax_ig, xs, igs)
            if self.ax_sum is not None:
                self._autoscale(self.ax_sum, xs, sums)
        self._update_suppression()
        self.canvas.draw_idle()

    def update_plot(self):
        if self.active_mode == MODE_OUTPUT:
            self._update_output_plot()
            return
        lab_a, lab_b = self.seg_labels
        xa, ya, ga, sa = self._branch_arrays(self._seg(lab_a))
        xb, yb, gb, sb = self._branch_arrays(self._seg(lab_b))

        if self.active_mode == MODE_ZERO_SAFE:
            xb, yb, gb, sb = self._mask_zero(xb, yb, gb, sb)

        self.line_id_a.set_data(xa, ya)
        self.line_id_b.set_data(xb, yb)
        self.line_ig_a.set_data(xa, ga)
        self.line_ig_b.set_data(xb, gb)
        if self.ax_sum is not None:
            self.line_sum_a.set_data(xa, sa)
            self.line_sum_b.set_data(xb, sb)
            self._autoscale(self.ax_sum, (xa, xb), (sa, sb))

        self._autoscale(self.ax_id, (xa, xb), (ya, yb))
        self._autoscale(self.ax_ig, (xa, xb), (ga, gb))
        self._update_suppression()
        self.canvas.draw_idle()

    def _update_suppression(self):
        """
        Compare the swing in ID against the swing in ID + IG. A ratio near one
        means the structure in ID survives removing the gate leak; a large
        ratio means most of what ID appeared to do was the leak.
        In time-trace mode it reports the drift of ID instead.
        """
        if self.active_mode == MODE_TRACE:
            self.var_suppression.set(self._drift_status())
            return
        if self.active_mode == MODE_OUTPUT:
            # Here ID swings because VDS is swept, so the swing comparison
            # below means nothing. Compare the largest gate and drain
            # currents instead.
            idr = np.array([abs(p.idrain) for p in self.data], dtype=float)
            ig = np.array([abs(p.ig) for p in self.data], dtype=float)
            idr, ig = idr[np.isfinite(idr)], ig[np.isfinite(ig)]
            if not (idr.size and ig.size):
                self.var_suppression.set("Leak check: waiting for data.")
            elif ig.max() > 0:
                self.var_suppression.set(
                    f"Leak check: max |IG| {ig.max():.3e} A, max |ID| {idr.max():.3e} A "
                    f"({idr.max() / ig.max():.0f}x)")
            else:
                self.var_suppression.set("Leak check: IG reads zero.")
            return
        raw = np.array([p.idrain for p in self.data], dtype=float)
        cor = np.array([p.idrain + p.ig for p in self.data], dtype=float)
        raw = raw[np.isfinite(raw)]
        cor = cor[np.isfinite(cor)]
        if raw.size < 3 or cor.size < 3:
            self.var_suppression.set("Leak check: waiting for data.")
            return
        sw_raw = float(raw.max() - raw.min())
        sw_cor = float(cor.max() - cor.min())
        mean_id = float(np.mean(np.abs(raw)))
        if mean_id <= 0:
            self.var_suppression.set("Leak check: n/a.")
            return
        pct_raw = sw_raw / mean_id * 100.0
        pct_cor = sw_cor / mean_id * 100.0
        if sw_cor > 0:
            msg = (f"Leak check: ID swings {pct_raw:.3f}%, ID+IG swings {pct_cor:.3f}% "
                   f"({sw_raw / sw_cor:.1f}x suppression)")
        else:
            msg = f"Leak check: ID swings {pct_raw:.3f}%, ID+IG flat"
        closure = self._loop_closure()
        self.var_suppression.set(msg + ("   |   " + closure if closure else ""))

    def _drift_status(self):
        """
        Rate of change of ID over the last DRIFT_WINDOW_S seconds, from a
        straight-line fit, in percent of the current ID per minute. A fit
        rather than first-minus-last point, so one noisy reading does not
        swing the verdict.
        """
        t = np.array([p.t for p in self.data], dtype=float)
        i = np.array([p.idrain for p in self.data], dtype=float)
        ok = np.isfinite(t) & np.isfinite(i)
        t, i = t[ok], i[ok]
        if t.size < 2:
            return "Drift: waiting for data."
        total = (i[-1] / i[0] - 1.0) * 100.0 if i[0] != 0 else np.nan
        recent = t >= t[-1] - DRIFT_WINDOW_S
        if recent.sum() < 5 or t[-1] - t[recent][0] < 0.5 * DRIFT_WINDOW_S:
            return (f"Drift: collecting the first {DRIFT_WINDOW_S:.0f} s... "
                    f"(ID {total:+.2f}% since start)")
        slope = np.polyfit(t[recent], i[recent], 1)[0]          # A per s
        rate = slope * 60.0 / abs(np.mean(i[recent])) * 100.0   # % per min
        verdict = "STABLE" if abs(rate) < DRIFT_STABLE_PCT_PER_MIN else "still drifting"
        return (f"Drift: ID {rate:+.2f} %/min over last {DRIFT_WINDOW_S:.0f} s -> {verdict}   "
                f"|   {total:+.2f}% since start")

    def _loop_closure(self):
        """
        In hysteresis mode, compare the two places the loop revisits a voltage.

        The turning point is measured twice within a few seconds of itself.
        The start voltage is measured at the very beginning and again at the
        very end. If the loop is open at the start voltage but closed at the
        turning point, the gap tracks elapsed time rather than sweep
        direction, and calling it hysteresis would be wrong.
        """
        if self.active_mode != MODE_HYSTERESIS:
            return ""
        pts = [p for p in self.data if not p.stitched]
        if len(pts) < 8:
            return ""
        lab_f, lab_r = self.seg_labels
        fwd = [p for p in pts if p.seg == lab_f]
        rev = [p for p in pts if p.seg == lab_r]
        if len(fwd) < 3 or len(rev) < 3:
            return ""

        v_turn = fwd[-1].vgs
        v_start = fwd[0].vgs

        def nearest(seq, v):
            return min(seq, key=lambda q: abs(q.vgs - v))

        gap_turn = nearest(rev, v_turn).idrain - nearest(fwd, v_turn).idrain
        p_start_f, p_start_r = nearest(fwd, v_start), nearest(rev, v_start)
        gap_start = p_start_r.idrain - p_start_f.idrain
        dt = abs(p_start_r.t - p_start_f.t)

        scale = float(np.mean(np.abs([p.idrain for p in pts])))
        if scale <= 0:
            return ""
        gt, gs = abs(gap_turn) / scale * 100, abs(gap_start) / scale * 100
        verdict = ""
        if gs > 3 * max(gt, 1e-9):
            verdict = " -> tracks elapsed time, not direction: suspect drift"
        elif gt > 0 and gs / gt < 1.5:
            verdict = " -> gap is direction-dependent: consistent with trapping"
        return (f"Loop: gap {gt:.3f}% at turning point vs {gs:.3f}% at start "
                f"({dt:.0f} s apart){verdict}")

    @staticmethod
    def _autoscale(ax, xs, ys):
        xf = np.concatenate([np.asarray(x, dtype=float) for x in xs]) if xs else np.array([])
        yf = np.concatenate([np.asarray(y, dtype=float) for y in ys]) if ys else np.array([])
        xf = xf[np.isfinite(xf)]
        yf = yf[np.isfinite(yf)]
        if not (xf.size and yf.size):
            return
        vx = max(1e-12, float(xf.max() - xf.min()))
        ax.set_xlim(float(xf.min()) - 0.05 * vx, float(xf.max()) + 0.05 * vx)
        ymin, ymax = float(yf.min()), float(yf.max())
        if ax.get_yscale() == "log":
            pos = yf[yf > 0]
            lo = float(pos.min()) if pos.size else 1e-18
            hi = ymax if ymax > 0 else lo * 10
            ax.set_ylim(lo / 1.2, hi * 1.2)
        else:
            vy = max(1e-12, ymax - ymin)
            ax.set_ylim(ymin - 0.1 * vy, ymax + 0.1 * vy)

    # ===== RUN BOOKKEEPING AND SAVING =====

    def _on_tab_change(self, event=None):
        try:
            i = self.nb.index("current")
        except Exception:
            return
        self.btn_start.config(text=self._tab_runs[i][0])

    def on_run_selected(self):
        """The Start button: run the measurement of the open tab."""
        if self.sweep_thread and self.sweep_thread.is_alive():
            messagebox.showwarning("Notice", "A measurement is already running.")
            return
        info = self.run_info
        unsaved = bool(info and not info.get("saved") and
                       ((info["has_plot"] and self.data) or info["report"]))
        if unsaved and messagebox.askyesno(
                "Unsaved measurement",
                "The last measurement has not been saved and the next one will replace it.\n"
                "Save it now?"):
            self._save_run(auto=False)
        self._tab_runs[self.nb.index("current")][1]()

    def _begin_run(self, kind, has_plot=True):
        """
        Called on the GUI thread just before a worker starts. The time stamp
        is taken here, at the start, so every file of one run shares a name.
        """
        self.run_info = {"kind": kind, "stamp": time.strftime("%Y%m%d_%H%M%S"),
                         "has_plot": has_plot, "report": None, "saved": False,
                         "sample": safe_name(self.var_sample.get())}

    def _run_basename(self):
        info = self.run_info or {}
        return f"{info.get('sample', 'sample')}_{info.get('kind', 'run')}_{info.get('stamp', '')}"

    def _set_report(self, text, title):
        """Keep the summary of this run for saving, and show it."""
        if self.run_info is not None:
            self.run_info["report"] = text
        self._show_report(text, title=title)

    def _run_done(self):
        """Posted by every worker as its last action, after the outputs are off."""
        if self.var_autosave.get():
            self._save_run(auto=True)

    def _data_folder(self):
        folder = self.var_folder.get().strip() or default_data_dir()
        os.makedirs(folder, exist_ok=True)
        return folder

    def _save_run(self, auto=False):
        """
        Save everything the last run produced into the data folder: CSV and
        PNG when it plotted data, the summary TXT when it made one. Saving the
        same run again rewrites the same files.
        """
        info = self.run_info
        has_data = bool(info and info["has_plot"] and self.data)
        has_report = bool(info and info["report"])
        if not (has_data or has_report):
            if not auto:
                messagebox.showinfo("Info", "Nothing to save yet: run a measurement first.")
            return
        try:
            folder = self._data_folder()
            base = os.path.join(folder, self._run_basename())
            made = []
            if has_data:
                self._write_csv(base + ".csv")
                self._write_png(base + ".png")
                made += [".csv", ".png"]
            if has_report:
                with open(base + "_summary.txt", "w", encoding="utf-8", newline="") as fh:
                    r = info["report"]
                    fh.write(r if r.endswith("\n") else r + "\n")
                made.append("_summary.txt")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save in the data folder:\n{e}")
            return
        info["saved"] = True
        self._remember_settings()
        name = os.path.basename(base)
        self.var_saved.set(f"Saved {name} ({', '.join(made)}) in {folder}")
        self.var_status.set(("Auto-saved " if auto else "Saved ") + name)

    def on_save_now(self):
        self._save_run(auto=False)

    def on_browse_folder(self):
        start = self.var_folder.get().strip()
        path = filedialog.askdirectory(
            initialdir=start if os.path.isdir(start) else os.path.dirname(default_data_dir()),
            title="Folder for measurement files")
        if path:
            self.var_folder.set(path)
            self._remember_settings()

    def on_open_folder(self):
        try:
            folder = self._data_folder()
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open the folder:\n{e}")

    def _remember_settings(self):
        self.settings.update(sample=self.var_sample.get().strip(),
                             folder=self.var_folder.get().strip(),
                             autosave=bool(self.var_autosave.get()))
        save_settings(self.settings)

    def _write_png(self, path):
        self.fig.savefig(path, dpi=150, bbox_inches="tight")

    def _write_csv(self, path):
        """Write the plotted run as CSV, rows in acquisition order. Raises on error."""
        rp = self.run_params
        read_mode = self.dev.read_mode if self.dev else "unknown"
        # Rows in ACQUISITION order, not sorted by VGS: the order is the
        # only record of when each point was taken, which is what tells
        # hysteresis from drift. Plotting sorts its own copy.
        rows = [p for p in self.data if not p.stitched]
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(f"# TSP-only (2636B), software v{APP_VERSION}\n")
            f.write(f"# Started: {rp.get('started', 'n/a')}\n")
            f.write("# Row order: acquisition order (NOT sorted by VGS)\n")
            if self.active_mode != MODE_OUTPUT:
                f.write(f"# Acquisition order: {'interleaved' if self.active_interleave else 'monotonic'}\n")
            f.write(f"# Read mode: {read_mode}\n")
            f.write(f"# Mode: {self.active_mode}\n")
            if self.active_mode == MODE_OUTPUT:
                vgs_txt = ", ".join(f"{v:g}" for v in rp.get("vgs_list", []))
                f.write(f"# VGS values in measured order: {vgs_txt}\n")
                f.write(f"# VDS from -{rp.get('vds_max')} to +{rp.get('vds_max')} and back, "
                        f"step={rp.get('vds_step')}, Settle per VGS [s]={rp.get('out_settle')}\n")
                f.write("# Segment = the VGS of each curve; VDS_V is the swept quantity.\n")
                f.write(f"# Comp_A={rp.get('comp_a')}, Comp_B={rp.get('comp_b')}\n")
                f.write(f"# NPLC={rp.get('nplc')}, Delay per point [s]={rp.get('delay_s')}\n")
            else:
                f.write(f"# Labels: {self.seg_labels[0]} / {self.seg_labels[1]}\n")
                if self.active_mode == MODE_TRACE:
                    f.write(f"# VGS hold={rp.get('vgs_hold')}, Duration [s]={rp.get('duration')}, "
                            f"Interval [s]={rp.get('interval')}\n")
                else:
                    f.write(f"# Start={rp.get('v_start')}, Stop={rp.get('v_stop')}, Step={rp.get('v_step')}\n")
                f.write(f"# VDS={rp.get('vds')}, Comp_A={rp.get('comp_a')}, Comp_B={rp.get('comp_b')}\n")
                f.write(f"# NPLC={rp.get('nplc')}, Delay per point [s]={rp.get('delay_s')}, "
                        f"Settle after ramp [s]={rp.get('ramp_wait')}\n")
            f.write(f"# Fixed gate range={rp.get('fix_range')}, 4-wire drain={rp.get('use4')}\n")
            f.write("# Point_index counts acquisitions from 0; Time_s is seconds since the\n")
            f.write("#   first point. Use them to separate a direction-dependent effect from a\n")
            f.write("#   time-dependent one before calling anything hysteresis.\n")
            f.write("# ID_plus_IG_A = ID + IG, which cancels any current flowing from the gate\n")
            f.write("#   into the drain. It is an upper bound on real channel modulation, not a\n")
            f.write("#   measurement of it: a gate leak to source or substrate does not cancel.\n")
            w = csv.writer(f)
            w.writerow(["Point_index", "Time_s", "VGS_V", "IG_A", "ID_A",
                        "ID_plus_IG_A", "VDS_V", "CompA", "CompB", "Segment"])
            for p in rows:
                w.writerow([p.idx, f"{p.t:.4f}", p.vgs, p.ig, p.idrain,
                            p.idrain + p.ig, p.vds,
                            int(p.comp_a), int(p.comp_b), p.seg])

    # ===== SHUTDOWN =====

    def on_closing(self):
        self._remember_settings()
        self.stop_flag.set()
        worker_alive = False
        if self.sweep_thread and self.sweep_thread.is_alive():
            # The worker ramps to 0 and switches off in its own finally block.
            self.sweep_thread.join(timeout=10.0)
            worker_alive = self.sweep_thread.is_alive()
        if self.dev:
            if worker_alive:
                # Worker is stuck (e.g. in a VISA timeout). Do not ramp in
                # parallel with it; just switch the outputs off.
                self.dev.outputs_off()
            else:
                try:
                    rs, rd = float(self.var_ramp_step.get()), float(self.var_ramp_delay.get())
                except Exception:
                    rs, rd = 0.02, 0.01
                self.dev.zero_and_off(rs, rd)
        try:
            if self.k:
                self.k.close()
            if self.rm:
                self.rm.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()