# DCforGating

Python GUI for DC characterization of gated thin films with a Keithley 2636B
(TSP mode): transfer curves, hysteresis loops, output curves (ID vs VDS at
fixed VGS), time traces and a contact check of gate, drain and source.

- `keithley_gating_gui.py`: the program. Channel A (smua) drives the gate,
  channel B (smub) holds VDS on the drain, source to LO.
- `CHANGELOG.md`: design notes and revision history.

## Setup

```
pip install -r requirements.txt
python keithley_gating_gui.py
```

Needs a VISA backend (NI-VISA or Keysight IO Libraries) and the 2636B set to
TSP command mode. Python's standard installer for Windows already includes
tkinter.

## Updating

```
git pull
```

## Use

1. Connect (Detect, then Connect).
2. Type the sample name and pick the data folder.
3. Open the tab of the measurement you want and set its parameters.
4. Press Start (the button says which measurement it will run).

Each run is saved automatically in the data folder as
`<sample>_<measurement>_<date>_<time>.csv`, `.png` and, when there is a
summary, `_summary.txt`.

Measurement files and `gui_settings.json` are ignored by git; keep them in
your data folder, not in this repository.
