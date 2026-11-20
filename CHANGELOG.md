# Changelog: Keithley 2636B gating GUI

Design notes and revision history, moved out of the header of
`keithley_gating_gui.py` so the code file stays readable. Newest first.

## 2026-11-19.2

LAYOUT
  - One tab per measurement: Transfer, Output curves, Time trace, Contact
    check. Each tab holds only its own parameters. VDS for the transfer
    curve and the time trace is one value shown in both tabs; Settle after
    ramp moved into the Transfer tab, the only place it is used.
  - One Start button, in a bar fixed above the scrolling panel, runs the
    open tab; its label says which measurement it will start. Stop, Save
    now and Open data folder sit next to it. The per-section Run buttons are
    gone.
  - Instrument settings shared by every measurement (compliances, NPLC,
    delay per point, ramps, gate range, abort on compliance, 4-wire) are in
    one section, and the plot options in another.
SAVING
  - New "Sample and data folder" section. Every run saves into that folder
    as <sample>_<measurement>_<date>_<time>: .csv and .png for runs that
    plot data, _summary.txt for runs that make a summary (output curves,
    contact check). Measurement names: transfer_zero_safe,
    transfer_hysteresis, transfer_single, output_curves, time_trace,
    contact_check. The time stamp is taken when the run starts, so all files
    of one run share it.
  - Auto-save at the end of each run, on by default, including runs ended
    with Stop or by compliance. "Save now" saves the last run by hand and
    rewrites the same files.
  - With auto-save off, Start asks before a new run replaces an unsaved one.
  - Sample name, data folder and the auto-save choice are remembered in
    gui_settings.json next to the program (ignored by git). Default folder:
    data/ next to the program (also ignored by git).
  - The Save CSV / Save PNG dialogs and the Save TXT button of the summary
    window are replaced by the above.
FIXES
  - A transfer curve or time trace started after an output-curve run no
    longer keeps the per-VGS lines in the plot and legend.
UNCHANGED ON PURPOSE
  - Measurement code and CSV contents. A hysteresis loop saved with this
    version matches one saved with 2026-11-17 apart from the version and
    date lines (checked against a simulated instrument).

## 2026-11-19

OUTPUT CURVES (new)
  - ID against VDS at a list of fixed VGS values, typed in measurement order
    (default -4, -2, 0, 2, 5 V). Answers the question a transfer curve
    cannot: is the gate changing the channel, or the contacts? Straight
    lines through the origin whose slope changes with VGS mean the channel;
    curvature that changes with VGS means gate-modulated contact barriers.
  - For each VGS: drain ramps to 0 V, gate ramps to VGS, waits "Settle per
    VGS" (default 60 s, because ID keeps creeping after a gate step on the
    HfO2 back-gated samples), then VDS goes -max to +max and back (default
    +/-0.2 V in 0.02 V steps). The there-and-back sequence makes VDS
    uncorrelated with time, so a slow drift of ID is fitted out of the slope
    instead of being read as conductance. A one-way ramp cannot separate
    the two.
  - A value typed twice (e.g. -4, 0, 5, -4) gets its own curve ("#2"), to
    check that the first curve is reproduced after the gate has been cycled.
  - Summary window at the end, saveable as TXT: conductance G per VGS,
    linearity (slope change between VDS = 0 and +/-max, and +/- asymmetry,
    from a cubic fit), drain current at VDS = 0 next to the mean gate
    current, and on/off from the slopes (G max / G min).
  - Plot: one colour per VGS, x = measured VDS, drawn in acquisition order so
    both branches of each curve are visible. The status bar compares the
    largest |IG| with the largest |ID|.
  - CSV: same columns as a sweep; Segment holds the VGS of each curve. The
    header lists the VGS order, VDS range, step and settle.
UNCHANGED ON PURPOSE
  - Gate sweeps, time trace and contact check, including their CSV headers:
    a hysteresis loop saved with this version matches one saved with
    2026-11-17 apart from the version and date lines (checked against a
    simulated instrument).

## 2026-11-17

CONTACT CHECK
  - Own "Settle per point (s)" field, default 10 s. Before, the check used the
    sweep's Delay per point (0.2 s). The check jumps between +amp and -amp
    without ramping, and on a large-area back gate (about 0.1 uF, charging
    time constant near 1 s) the charging current has the sign of the applied
    voltage, so the fit read it as a conductance. Expected artifact at 0.2 s:
    an apparent gate resistance of a few MOhm on a gate that is really in the
    GOhm range. Changing the check no longer touches the sweep protocol.
  - A FINITE LEAKAGE verdict now says how to confirm it: repeat with twice
    the settle. The R_GD cross-check cannot do this job. The drain sweep
    charges the same gate capacitor, so both R_GD values carry the same
    artifact and agree with each other (offline simulation: 0.1 % spread
    with both at 18 MOhm, real leak 2 GOhm). Only the settle time separates
    charging current, which decays, from leakage, which does not.
  - The report includes the raw readings in acquisition order, so the fits
    can be redone from the saved TXT.
  - Progress per point in the status bar (the check now takes minutes).
  - "Finished. Outputs off." is reported after the outputs are actually off.
EFFICIENCY
  - Ramps read the programmed level (source.levelv) instead of measuring the
    output voltage. No A/D conversion, so no autozero cycles, and correct
    even if the output is off.
  - Detect runs in a background thread and skips serial (ASRL) ports, with a
    1 s probe timeout instead of 3 s. Before, each COM port cost a full
    timeout on the GUI thread and froze the window. Detect is refused while
    a measurement runs, and Connect waits for Detect to finish.
TRACEABILITY
  - APP_VERSION is shown in the title bar and written into every CSV header
    and contact-check report.
  - Revision notes moved from the code header to this file.
UNCHANGED ON PURPOSE
  - Sweep timing, NPLC, autozero and every default of the main sweep, so new
    loops stay comparable with the reference data of 30 Oct 2026.

## 2026-10-07
  TIME TRACE (new)
    - Holds VGS and VDS fixed and logs ID and IG against time. Answers "is
      the sample changing by itself?" without sweeping the gate, so it
      measures drift directly and stresses the oxide far less than repeated
      loops. Recording starts right after the gate reaches its hold voltage,
      so the initial charging transient is visible at the start of the trace.
    - The status bar reports the ID drift rate over the last 60 s in % per
      minute and says STABLE once it falls below 0.2 %/min. The CSV uses the
      same columns as a sweep, with Time_s as the quantity of interest.
  SPEED AND MEASUREMENT QUALITY
    - Each point now uses smuX.measure.iv(), one simultaneous I/V
      conversion per channel, instead of four separate conversions. That
      halves the integration time per point and makes each current and its
      voltage come from the same instant. The earlier note that iv() "only
      returns the current inside print()" was Lua, not the firmware: a call
      that is not the LAST argument of print() is truncated to its first
      return value. Assigning to locals first avoids it. If a firmware ever
      answers the iv() query badly, read_point() falls back to the old
      four-conversion query on its own and the CSV header records which
      read mode was used.
  GUI RESPONSIVENESS
    - Instrument configuration, the initial ramp and the settle wait now run
      in the worker thread. Before, on_start() slept for the full settle time
      (10 s by default) on the GUI thread, so the window froze and Stop could
      not be pressed.
    - Every wait (settle, per-point delay) uses stop_flag.wait(), so Stop
      acts within one point instead of after the current sleep finishes.
  THREAD SAFETY
    - The worker no longer touches tkinter. It used to read StringVars and
      call self.after() from the worker thread, which tkinter does not
      guarantee to be safe. All parameters are read once on the GUI thread,
      and the worker hands points and messages back through a queue that the
      GUI drains every 50 ms. That also batches plot redraws.
  SAFETY
    - The error queue is now actually read after configuration. Any error
      aborts the run BEFORE the outputs are enabled.
    - Contact check now reports compliance. A clamped current makes the
      fitted resistance meaningless, and on a gate with no dielectric that
      is exactly what happens. The report gives the upper bound instead and
      a plain verdict on the gate.
  SMALLER FIXES
    - Hysteresis reverse branch reuses the forward grid. When (Stop - Start)
      was not a multiple of Step the two branches landed on different
      voltages, which biased the loop-closure comparison.
    - CSV header records the parameters the run was STARTED with, not
      whatever is in the entry boxes at save time.
    - Reconnecting closes the previous VISA session instead of leaking it.
    - Shutdown logic (ramp both SMUs to 0, outputs off) lives in one method.
    - Removed unused code: dataclasses.field import, Fit.v_zero, unused
      arguments of the report builder, duplicated sweep-builder code.

## Earlier revisions
  SAFETY
    - Gate compliance default lowered from 1e-3 to 1e-6 A.
    - Drain compliance default lowered from 1e-2 to 1e-3 A.
    - VDS default lowered from 0.5 V to 0.05 V.
    - Drain is soft-ramped to VDS instead of stepping there when the output
      is enabled.
    - Optional abort when either SMU enters compliance (on by default).
  BUG FIXES
    - The hysteresis control is honoured. Three sweep modes are selectable:
      zero-safe branches, true hysteresis loop, single ramp.
    - Worker-thread error dialog no longer raises NameError.
  MEASUREMENT QUALITY
    - All four quantities (IG, VGS, ID, VDS) are acquired in a single query
      per point.
    - VDS is read back and logged, so drain compliance collapse is visible.
    - Source compliance flags are read and logged at every point.
    - Optional fixed source voltage range instead of source autorange.
    - Autozero enabled on both channels.
    - Log IG plots the magnitude of IG, so negative leakage is not discarded.
    - Default per-point delay 0.2 s. Run the same sweep at two delays and
      compare before trusting any curve.
  LABELLING
    - In zero-safe mode the two traces are labelled Positive branch and
      Negative branch rather than Up and Down. They are NOT a hysteresis
      loop. Use hysteresis mode for that.
    - The stitched 0 V point is written to the CSV once, not twice.
  INTERLEAVE
    - The main sweep can be reordered by interleave_for_drift(), which pairs
      the extremes inward so bias sign is uncorrelated with time. That keeps
      drift out of a fitted slope, which is why the contact check always
      uses it.
    - It is OFF by default for the main sweep. Pairing the extremes makes
      consecutive points jump the whole range rather than one Step, so the
      per-point delay is no longer sized for the actual settling load and
      the dielectric gets cycled hard. On a rectifying gate it also only
      half works: |V| still falls monotonically with index, so the even part
      of I(V) aliases into the fitted drift term.
    - Rule of thumb: interleave when the result is a slope, keep monotonic
      when the result is the shape of a curve.
  BRANCH TRANSITIONS
    - The gate is soft-ramped whenever the next point is more than one Step
      away from where the gate physically sits. Zero-safe ends the positive
      branch at +Vmax and restarts the negative one near 0 V, and stepping
      that charged the gate capacitance in one go: the measured IG transient
      right after the jump exceeded the steady-state current at full bias
      and took about a second to decay.
    - The test is on the applied VOLTAGE, not on the segment label. In
      zero-safe mode the first point of the negative branch is the stitched
      0 V point, which reuses an earlier reading and never touches the SMU,
      so a label test would skip the ramp exactly when it is needed.
    - Skipped when the sweep is interleaved, where every point is a large
      jump and ramping all of them would cost minutes instead of seconds.
    - Settle after ramp holds the gate at the new voltage before measuring.
      On this stack the IG excess over its steady value decays with a time
      constant near 1 s. Default 10 s, about ten time constants, paid once
      per ramp.
  LEAK-CORRECTED PANEL
    - An OPTIONAL third subplot shows ID + IG, off by default. Current that
      leaves the gate and lands on the drain appears in ID with the sign
      opposite to IG, so the sum cancels it. If ID shows structure and
      ID + IG is flat, the structure was leakage.
    - The sum is a subtraction, not a measurement. Read a residual as an
      upper bound on the real channel modulation.
  ACQUISITION TIME
    - Each point carries its acquisition index and elapsed seconds, and the
      CSV is written in acquisition order rather than sorted by VGS.
    - In hysteresis mode the status bar compares the loop gap at the
      turning point against the gap at the start voltage. A gap that
      appears only at the start voltage is drift, not trapping.
    - Save dialogs propose a timestamped filename.
