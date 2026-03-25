# Keithley 2636B (TSP-only) — ID vs VGS + IG vs VGS (two stacked subplots)
# A (smua): sweep VGS, measure IG & VGS
# B (smub): hold VDS const, measure ID
# TSP-only (no SCPI) → avoids -285/-286. 1 query/point.
# Added:
#   - Soft ramp to first point before sweep; soft ramp to 0 V on Stop/Close.
#   - GUI controls for ramp step and ramp delay.
#   - Log-scale handling toggle: clip nonpositive to tiny value or hide as NaN.
#   - CSV with metadata header lines (UTF-8, sin Unicode raro).
#   - ZERO-SAFE SWEEP: start at 0 V -> +Vmax, then 0 V -> Vmin.
#   - "Cosido" en 0 V: se usa la MISMA lectura de 0 V en Up y Down (sin doble medición).
#   - Plot y CSV ordenados por VGS ascendente (negativo -> positivo).
#   - Ocultamos el 0 V SOLO en el trazo Down al graficar para evitar la “aguja”.
# Reqs: pyvisa, matplotlib, numpy, tkinter

import os, csv, time, threading, tkinter as tk
from tkinter import ttk, messagebox, filedialog
import pyvisa
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

APP_TITLE = "2636B TSP — ID/IG vs VGS (A sweep, B VDS const)"
APP_SIZE = "1060x800"

PALETTE = {
    "bg":"#f7f7f9","panel":"#ffffff","border":"#e5e7eb","text":"#1f2937",
    "muted":"#6b7280","primary":"#2f4858","primary2":"#3e5f74"
}

def apply_style(root):
    root.configure(bg=PALETTE["bg"])
    s = ttk.Style(); s.theme_use("clam")
    s.configure("TLabel", background=PALETTE["panel"], foreground=PALETTE["text"])
    s.configure("TFrame", background=PALETTE["panel"])
    s.configure("TCheckbutton", background=PALETTE["panel"], foreground=PALETTE["text"])
    s.configure("Primary.TButton", background=PALETTE["primary"], foreground="white",
               bordercolor=PALETTE["primary"], padding=6)
    s.map("Primary.TButton", background=[("active", PALETTE["primary2"])])
    s.configure("Hint.TLabel", foreground=PALETTE["muted"])
    s.configure("Header.TLabel", background=PALETTE["bg"], foreground=PALETTE["text"],
               font=("Segoe UI", 16, "bold"))

def list_keithley_resources():
    rm = pyvisa.ResourceManager(); out=[]
    for r in rm.list_resources():
        try:
            i=rm.open_resource(r, timeout=8000); i.read_termination="\n"; i.write_termination="\n"
            i.query("print(1)")  # TSP probe
            i.close(); out.append(r)
        except Exception:
            pass
    return out

# ------------- ZERO-SAFE SWEEP (incluye 0 en ambos tramos) -------------
def build_zero_safe_sweep(start, stop, step):
    """
    Siempre inicia en 0 V. Hace:
      1) 0 -> +Vmax (incluye 0)
      2) 0 -> Vmin (incluye 0)
    Devuelve [("up", v), ("down", v), ...].
    El signo del paso se ignora: se usa abs(step).
    """
    v0 = float(start); v1 = float(stop); dv = abs(float(step))
    if dv == 0:
        raise ValueError("Step cannot be zero.")

    v_lo = min(v0, v1)
    v_hi = max(v0, v1)

    sweep = []

    # 0 -> +Vmax (incluye 0)
    if v_hi > 0:
        pos = np.round(np.arange(0.0, v_hi + dv/2, dv), 12).tolist()
        if not pos or pos[0] != 0.0:
            pos = [0.0] + pos
        sweep += [("up", float(v)) for v in pos]
    else:
        sweep += [("up", 0.0)]  # al menos un 0 en up

    # 0 -> Vmin (incluye 0 y luego negativos)
    if v_lo < 0:
        neg = np.round(np.arange(0.0, v_lo - dv/2, -dv), 12).tolist()
        if not neg or neg[0] != 0.0:
            neg = [0.0] + neg
        sweep += [("down", float(v)) for v in neg]

    return sweep

# (Se conserva el builder legacy por si lo quieres usar en el futuro)
def build_sweep(v0, v1, dv, hyst=False):
    v0=float(v0); v1=float(v1); dv=float(dv)
    if dv==0: raise ValueError("Step cannot be zero.")
    up  = np.round(np.arange(v0, v1 + np.sign(dv)*dv/2, dv), 12).tolist()
    if not hyst: return [("up", float(v)) for v in up]
    dn  = np.round(np.arange(v1, v0 - np.sign(dv)*dv/2, -dv), 12).tolist()
    return [("up", float(v)) for v in up] + [("down", float(v)) for v in dn]

class K2636B_TSP:
    """ TSP-only control for 2636B: smua (gate), smub (drain). """
    def __init__(self, inst):
        self.k = inst

    def _w(self, cmd: str):
        self.k.write(cmd)

    def _q(self, cmd: str) -> str:
        return self.k.query(cmd).strip()

    def init_instrument(self, comp_a, comp_b, nplc, use_4w, vds_const):
        w = self._w
        # ----- Pre-vuelo SEGURO (tolerante a firmware) -----
        w("if abort ~= nil then abort() end")
        w("if waitcomplete ~= nil then waitcomplete() end")
        w("if errorqueue and errorqueue.clear then errorqueue.clear() end")
        w("if eventlog and eventlog.clear then eventlog.clear() end")
        w("if localnode ~= nil and localnode.clearevents ~= nil then localnode.clearevents() end")

        # A (VGS sweep)
        w("smua.reset()")
        w("smua.source.func = smua.OUTPUT_DCVOLTS")
        w("smua.source.autorangev = smua.AUTORANGE_ON")
        w(f"smua.measure.nplc = {float(nplc)}")
        w(f"smua.source.limiti = {float(comp_a)}")
        # FIX: asignación completa en ambos casos
        w("smua.sense = smua.SENSE_REMOTE" if use_4w else "smua.sense = smua.SENSE_LOCAL")
        w("smua.source.levelv = 0")

        # B (VDS const)
        w("smub.reset()")
        w("smub.source.func = smub.OUTPUT_DCVOLTS")
        w("smub.source.autorangev = smub.AUTORANGE_ON")
        w(f"smub.measure.nplc = {float(nplc)}")
        w(f"smub.source.limiti = {float(comp_b)}")
        # FIX: asignación completa en ambos casos
        w("smub.sense = smub.SENSE_REMOTE" if use_4w else "smub.sense = smub.SENSE_LOCAL")
        w(f"smub.source.levelv = {float(vds_const)}")

        # Outputs ON al final de la configuración
        w("smua.source.output = smua.OUTPUT_ON")
        w("smub.source.output = smub.OUTPUT_ON")

        # Silenciar beeper si existe
        w("if beeper and beeper.enable ~= nil then beeper.enable = 0 end")

    def set_vgs(self, v):
        self._w(f"smua.source.levelv = {float(v)}")

    def read_vgs_ig_id(self):
        # 1 query/point (TSP print con 3 valores)
        s = self._q("print(smua.measure.v(), smua.measure.i(), smub.measure.i())")
        vs = s.replace(',', ' ').split()
        if len(vs) < 3:
            raise RuntimeError(f"Bad read: {s}")
        vgs = float(vs[0]); ig = float(vs[1]); id_ = float(vs[2])
        return vgs, ig, id_

    def _read_v(self, which="a"):
        node = "smua" if which.lower()=="a" else "smub"
        return float(self._q(f"print({node}.measure.v())"))

    def ramp_smua_to(self, target_v, step_v, delay_s):
        target_v = float(target_v); step_v = abs(float(step_v)); delay_s = float(delay_s)
        try:
            v_now = self._read_v("a")
        except:
            v_now = 0.0
        if step_v <= 0: step_v = 0.02
        nsteps = int(max(1, np.ceil(abs(target_v - v_now)/step_v)))
        for i in range(1, nsteps+1):
            v_next = v_now + (target_v - v_now) * (i / nsteps)
            self._w(f"smua.source.levelv = {v_next}")
            time.sleep(delay_s)

    def ramp_smub_to(self, target_v, step_v, delay_s):
        target_v = float(target_v); step_v = abs(float(step_v)); delay_s = float(delay_s)
        try:
            v_now = self._read_v("b")
        except:
            v_now = 0.0
        if step_v <= 0: step_v = 0.02
        nsteps = int(max(1, np.ceil(abs(target_v - v_now)/step_v)))
        for i in range(1, nsteps+1):
            v_next = v_now + (target_v - v_now) * (i / nsteps)
            self._w(f"smub.source.levelv = {v_next}")
            time.sleep(delay_s)

    def outputs_off(self):
        try:
            self._w("smua.source.output = smua.OUTPUT_OFF")
            self._w("smub.source.output = smub.OUTPUT_OFF")
        except: pass

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE); self.geometry(APP_SIZE); self.minsize(1000, 700)
        apply_style(self)
        self.rm=None; self.k=None; self.dev=None
        # data: (VGS, IG, ID, dir)
        self.data=[]
        self.stop_flag=threading.Event(); self.sweep_thread=None

        # log-scale toggles
        self.var_log_id = tk.BooleanVar(value=False)
        self.var_log_ig = tk.BooleanVar(value=False)
        self.var_clip_log = tk.BooleanVar(value=False)

        # guardamos la lectura de 0 V para coser Up/Down
        self.zero_sample = None  # tuple (vgs, ig, id_)

        self._build_ui()

    def _build_ui(self):
        ttk.Label(self, text="ID–VGS (top) and IG–VGS (bottom) — TSP-only",
                  style="Header.TLabel").pack(side=tk.TOP, anchor="w", padx=18, pady=(16, 8))

        card = tk.Frame(self, bg=PALETTE["panel"], highlightthickness=1); card.config(highlightbackground=PALETTE["border"])
        card.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=8)
        left = tk.Frame(card, bg=PALETTE["panel"]); left.pack(side=tk.LEFT, fill=tk.Y, padx=(16,12), pady=16)
        sep = tk.Frame(card, width=1, bg=PALETTE["border"]); sep.pack(side=tk.LEFT, fill=tk.Y, pady=16)
        right = tk.Frame(card, bg=PALETTE["panel"]); right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12,16), pady=16)

        def section(parent, title):
            box=tk.Frame(parent, bg=PALETTE["panel"], highlightthickness=1); box.config(highlightbackground=PALETTE["border"])
            box.pack(fill=tk.X, pady=6); head=tk.Frame(box, bg=PALETTE["panel"]); head.pack(fill=tk.X, padx=10, pady=(8,2))
            ttk.Label(head, text=title).pack(side=tk.LEFT)
            body=tk.Frame(box, bg=PALETTE["panel"]); body.pack(fill=tk.X, padx=10, pady=(2,10)); return body

        # Connection
        conn=section(left,"Connection (TSP-only)")
        self.var_resource=tk.StringVar()
        self.cmb_resource=ttk.Combobox(conn, textvariable=self.var_resource, width=42)
        self.cmb_resource.grid(row=0,column=0,columnspan=3,sticky="we",padx=(0,6))
        ttk.Button(conn,text="Detect",command=self.on_detect).grid(row=0,column=3,sticky="e")
        ttk.Button(conn,text="Connect",style="Primary.TButton",command=self.on_connect).grid(row=0,column=4,sticky="e")
        self.lbl_mode=ttk.Label(conn,text="Expecting TSP (native 2600).",style="Hint.TLabel")
        self.lbl_mode.grid(row=1,column=0,columnspan=5,sticky="w",pady=(6,0))
        for c in range(5): conn.grid_columnconfigure(c,weight=1)

        # VGS sweep
        prm=section(left,"VGS Sweep (Channel A)")
        self.var_start=tk.StringVar(value="-1.0")
        self.var_stop =tk.StringVar(value="1.0")
        self.var_step =tk.StringVar(value="0.05")
        self.var_hyst =tk.BooleanVar(value=False)  # legacy, no se usa en modo cero-seguro
        ttk.Label(prm,text="Start (V)").grid(row=0,column=0,sticky="w"); ttk.Entry(prm,textvariable=self.var_start,width=10).grid(row=0,column=1,sticky="w",padx=(6,14))
        ttk.Label(prm,text="Stop (V)").grid(row=0,column=2,sticky="w");  ttk.Entry(prm,textvariable=self.var_stop ,width=10).grid(row=0,column=3,sticky="w",padx=(6,14))
        ttk.Label(prm,text="Step (V)").grid(row=0,column=4,sticky="w");  ttk.Entry(prm,textvariable=self.var_step ,width=10).grid(row=0,column=5,sticky="w",padx=(6,0))
        ttk.Checkbutton(prm,text="Hysteresis (legacy up & down)",variable=self.var_hyst).grid(row=1,column=0,columnspan=6,sticky="w",pady=(6,0))
        for c in range(6): prm.grid_columnconfigure(c,weight=1)

        # Bias & measurement + RAMP controls
        meas=section(left,"Bias & Measurement")
        self.var_vds  =tk.StringVar(value="0.5")    # B
        self.var_compA=tk.StringVar(value="0.001")  # IG compliance
        self.var_compB=tk.StringVar(value="0.01")   # ID compliance
        self.var_nplc =tk.StringVar(value="1.0")
        self.var_delay=tk.StringVar(value="0.05")
        self.var_4w   =tk.BooleanVar(value=False)
        self.var_ramp_step=tk.StringVar(value="0.02")
        self.var_ramp_delay=tk.StringVar(value="0.01")

        ttk.Label(meas,text="VDS const (B) (V)").grid(row=0,column=0,sticky="w"); ttk.Entry(meas,textvariable=self.var_vds,width=10).grid(row=0,column=1,sticky="w",padx=(6,14))
        ttk.Label(meas,text="Comp A (IG) (A)").grid(row=0,column=2,sticky="w"); ttk.Entry(meas,textvariable=self.var_compA,width=10).grid(row=0,column=3,sticky="w",padx=(6,14))
        ttk.Label(meas,text="Comp B (ID) (A)").grid(row=0,column=4,sticky="w"); ttk.Entry(meas,textvariable=self.var_compB,width=10).grid(row=0,column=5,sticky="w",padx=(6,0))
        ttk.Label(meas,text="NPLC").grid(row=1,column=0,sticky="w",pady=(6,0)); ttk.Entry(meas,textvariable=self.var_nplc,width=10).grid(row=1,column=1,sticky="w",padx=(6,14),pady=(6,0))
        ttk.Label(meas,text="Delay per point (s)").grid(row=1,column=2,sticky="w",pady=(6,0)); ttk.Entry(meas,textvariable=self.var_delay,width=10).grid(row=1,column=3,sticky="w",padx=(6,14),pady=(6,0))
        ttk.Checkbutton(meas,text="4-wire (Kelvin) on both",variable=self.var_4w).grid(row=1,column=4,sticky="w",pady=(6,0))

        ttk.Label(meas,text="Ramp Step (V)").grid(row=2,column=0,sticky="w",pady=(6,0)); ttk.Entry(meas,textvariable=self.var_ramp_step,width=10).grid(row=2,column=1,sticky="w",padx=(6,14),pady=(6,0))
        ttk.Label(meas,text="Ramp Delay (s)").grid(row=2,column=2,sticky="w",pady=(6,0)); ttk.Entry(meas,textvariable=self.var_ramp_delay,width=10).grid(row=2,column=3,sticky="w",padx=(6,14),pady=(6,0))

        for c in range(6): meas.grid_columnconfigure(c,weight=1)

        # Actions
        act=section(left,"Actions")
        ttk.Button(act,text="Start (ID–VGS)",style="Primary.TButton",command=self.on_start).grid(row=0,column=0,sticky="we")
        ttk.Button(act,text="Stop",command=self.on_stop).grid(row=0,column=1,sticky="we",padx=6)
        ttk.Button(act,text="Save CSV",command=self.on_save_csv).grid(row=1,column=0,sticky="we",pady=(6,0))
        ttk.Button(act,text="Save PNG",command=self.on_save_png).grid(row=1,column=1,sticky="we",padx=6,pady=(6,0))

        # Y-Scale + log handling
        scale = section(left, "Y-Scale")
        ttk.Checkbutton(scale, text="Log ID (top)", variable=self.var_log_id, command=self.apply_scales)\
            .grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(scale, text="Log IG (bottom)", variable=self.var_log_ig, command=self.apply_scales)\
            .grid(row=0, column=1, sticky="w", padx=(12,0))
        ttk.Checkbutton(scale, text="Clip nonpositive for log", variable=self.var_clip_log, command=self.apply_scales)\
            .grid(row=1, column=0, columnspan=2, sticky="w", pady=(6,0))
        for c in range(2): scale.grid_columnconfigure(c, weight=1)

        ttk.Label(left,text="Tip: Instrument MUST be in TSP (native 2600). No SCPI is sent.",
                  style="Hint.TLabel").pack(fill=tk.X,padx=4,pady=(6,0))

        # --------- Plot (two stacked subplots, shared X) ----------
        plot= tk.Frame(right,bg=PALETTE["panel"],highlightthickness=1); plot.config(highlightbackground=PALETTE["border"])
        plot.pack(fill=tk.BOTH,expand=True)

        self.fig = Figure(figsize=(7.4, 5.8))
        gs = self.fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.15)

        # Top: ID vs VGS
        self.ax_id = self.fig.add_subplot(gs[0, 0])
        self.ax_id.set_facecolor("#fcfcfd")
        self.ax_id.set_ylabel("ID (A)")
        self.ax_id.grid(True, color="#eaecef")
        self.line_id_up, = self.ax_id.plot([], [], marker="o", linestyle="-", label="ID (Up)", linewidth=1.6)
        self.line_id_dn, = self.ax_id.plot([], [], marker="s", linestyle="-", label="ID (Down)", linewidth=1.6)
        leg1 = self.ax_id.legend(frameon=True); leg1.get_frame().set_edgecolor(PALETTE["border"]); leg1.get_frame().set_facecolor("#ffffff")

        # Bottom: IG vs VGS
        self.ax_ig = self.fig.add_subplot(gs[1, 0], sharex=self.ax_id)
        self.ax_ig.set_facecolor("#fcfcfd")
        self.ax_ig.set_xlabel("VGS (V)")
        self.ax_ig.set_ylabel("IG (A)")
        self.ax_ig.grid(True, color="#eaecef")
        self.line_ig_up, = self.ax_ig.plot([], [], marker="o", linestyle="-", label="IG (Up)", linewidth=1.6)
        self.line_ig_dn, = self.ax_ig.plot([], [], marker="s", linestyle="-", label="IG (Down)", linewidth=1.6)
        leg2 = self.ax_ig.legend(frameon=True); leg2.get_frame().set_edgecolor(PALETTE["border"]); leg2.get_frame().set_facecolor("#ffffff")

        self.canvas=FigureCanvasTkAgg(self.fig,master=plot)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH,expand=True,padx=10,pady=10)

        # Status
        bar=tk.Frame(self,bg=PALETTE["panel"],highlightthickness=1); bar.config(highlightbackground=PALETTE["border"])
        bar.pack(side=tk.BOTTOM,fill=tk.X, padx=16, pady=(0,12))
        self.var_status=tk.StringVar(value="Ready."); ttk.Label(bar,textvariable=self.var_status).pack(side=tk.LEFT,padx=10,pady=6)

    # ----------------- VISA / Instrument -----------------
    def on_detect(self):
        res=list_keithley_resources()
        self.cmb_resource["values"]=res
        if res: self.var_resource.set(res[0])
        self.var_status.set(f"Detected {len(res)} resource(s) that answered TSP.")

    def on_connect(self):
        res=self.var_resource.get().strip()
        if not res:
            messagebox.showwarning("Notice","Select/enter a VISA Resource."); return
        try:
            self.rm=pyvisa.ResourceManager()
            self.k =self.rm.open_resource(res, timeout=20000)
            self.k.read_termination="\n"; self.k.write_termination="\n"
            # Verify TSP
            try:
                model=self.k.query("print(localnode.model)").strip()
                self.lbl_mode.config(text=f"Connected (TSP): {model}")
            except Exception as e:
                raise RuntimeError("Instrument did not respond in TSP. Switch to TSP (2600) and power-cycle.") from e
            self.dev=K2636B_TSP(self.k)
            self.var_status.set("Connected (TSP).")
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect:\n{e}")

    def on_start(self):
        if not self.dev:
            messagebox.showwarning("Notice","Connect first (TSP)."); return
        try:
            compA=float(self.var_compA.get()); compB=float(self.var_compB.get())
            nplc =float(self.var_nplc.get());  vds  =float(self.var_vds.get())
            use4 =self.var_4w.get()
            ramp_step=float(self.var_ramp_step.get()); ramp_delay=float(self.var_ramp_delay.get())
            self.dev.init_instrument(compA, compB, nplc, use4, vds)
        except Exception as e:
            messagebox.showerror("Error",f"Configure failed:\n{e}"); return
        try:
            sweep = build_zero_safe_sweep(self.var_start.get(), self.var_stop.get(), self.var_step.get())
            self.var_status.set(f"Zero-safe sweep: 0->+max then 0->min (points={len(sweep)})")
        except Exception as e:
            messagebox.showerror("Error",f"Invalid sweep:\n{e}"); return

        # Soft ramp SMUA to the first sweep point (será 0.0)
        try:
            first_target = sweep[0][1]
            self.dev.ramp_smua_to(first_target, ramp_step, ramp_delay)
        except Exception as e:
            messagebox.showwarning("Notice", f"Soft ramp to first point failed, continuing anyway:\n{e}")

        self.data.clear()
        self.zero_sample = None
        self.update_plot()
        self.stop_flag.clear()
        self.sweep_thread=threading.Thread(
            target=self._worker,
            args=(sweep, float(self.var_delay.get())),
            daemon=True
        )
        self.sweep_thread.start()

    # helper para comparar con 0
    @staticmethod
    def _is_close0(x, eps=1e-9):
        try:
            return abs(float(x)) <= eps
        except:
            return False

    def _worker(self, sweep, delay_s):
        try:
            for direction, v in sweep:
                if self.stop_flag.is_set(): break

                # Si es 0 V en "down" y ya tenemos la lectura de 0, reusar sin medir de nuevo
                if direction == "down" and self._is_close0(v) and (self.zero_sample is not None):
                    vgs, ig, id_ = self.zero_sample
                else:
                    self.dev.set_vgs(v)
                    time.sleep(delay_s)
                    vgs, ig, id_ = self.dev.read_vgs_ig_id()
                    # guardar la lectura de 0 V (primera vez que pasamos por 0)
                    if self._is_close0(v) and self.zero_sample is None:
                        self.zero_sample = (vgs, ig, id_)

                self.data.append((vgs, ig, id_, direction))
                self.after(0, self.update_plot)

            self.after(0, lambda: self.var_status.set("Sweep stopped." if self.stop_flag.is_set() else "Sweep finished."))
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", f"Acquisition failed:\n{e}"))
        finally:
            # Soft ramp ambos canales a 0 V y apagar salidas
            try:
                ramp_step = float(self.var_ramp_step.get()); ramp_delay = float(self.var_ramp_delay.get())
            except:
                ramp_step, ramp_delay = 0.02, 0.01
            try:
                self.dev.ramp_smua_to(0.0, ramp_step, ramp_delay)
            except: pass
            try:
                self.dev.ramp_smub_to(0.0, ramp_step, ramp_delay)
            except: pass
            try: self.dev.outputs_off()
            except: pass
            # limpiar memoria del 0
            self.zero_sample = None

    def on_stop(self):
        self.stop_flag.set()
        self.var_status.set("Stopping (soft ramp to 0 V)…")

    # ----------------- Plot helpers -----------------
    def _split_dirs(self, series):
        up  = [x for x in series if x[3]=="up"]
        dn  = [x for x in series if x[3]=="down"]
        return up, dn

    def _filter_for_log(self, ylist):
        arr = np.array(ylist, dtype=float)
        if self.var_clip_log.get():
            tiny = 1e-18
            arr[arr<=0] = tiny
        else:
            arr[arr<=0] = np.nan
        return arr

    def _sort_xy(self, xs, ys):
        xs = np.array(xs, dtype=float)
        ys = np.array(ys, dtype=float)
        if xs.size == 0: return xs, ys
        idx = np.argsort(xs)  # negativo -> positivo
        return xs[idx], ys[idx]

    def _mask_zero_in_dn_for_plot(self, vx_dn_s, y_dn_s, g_dn_s, eps=1e-12):
        # ocultar el punto de 0 V en la serie Down para evitar la “aguja”
        if getattr(vx_dn_s, "size", 0):
            import numpy as _np
            idx = _np.where(_np.isclose(vx_dn_s, 0.0, atol=eps))[0]
            if idx.size:
                i = idx[0]
                vx_dn_s[i] = _np.nan
                y_dn_s[i]  = _np.nan
                g_dn_s[i]  = _np.nan
        return vx_dn_s, y_dn_s, g_dn_s

    def apply_scales(self):
        self.ax_id.set_yscale("log" if self.var_log_id.get() else "linear")
        self.ax_ig.set_yscale("log" if self.var_log_ig.get() else "linear")
        self.update_plot(rescale_only=True)

    def update_plot(self, rescale_only=False):
        # Separar por dirección
        up, dn = self._split_dirs(self.data)
        vx_up  = [v for (v,ig,id_,d) in up]
        vx_dn  = [v for (v,ig,id_,d) in dn]
        id_up  = [id_ for (v,ig,id_,d) in up]
        id_dn  = [id_ for (v,ig,id_,d) in dn]
        ig_up  = [ig for (v,ig,id_,d) in up]
        ig_dn  = [ig for (v,ig,id_,d) in dn]

        # Log scale handling
        if self.var_log_id.get():
            y_up = self._filter_for_log(id_up); y_dn = self._filter_for_log(id_dn)
            if (np.isnan(y_up).all() and np.isnan(y_dn).all()) and (id_up or id_dn) and not self.var_clip_log.get():
                self.var_status.set("Log ID ON: nonpositive data hidden.")
        else:
            y_up = np.array(id_up, dtype=float); y_dn = np.array(id_dn, dtype=float)

        if self.var_log_ig.get():
            g_up = self._filter_for_log(ig_up); g_dn = self._filter_for_log(ig_dn)
            if (np.isnan(g_up).all() and np.isnan(g_dn).all()) and (ig_up or ig_dn) and not self.var_clip_log.get():
                self.var_status.set("Log IG ON: nonpositive data hidden.")
        else:
            g_up = np.array(ig_up, dtype=float); g_dn = np.array(ig_dn, dtype=float)

        # Ordenar por VGS ascendente para líneas limpias
        vx_up_s, y_up_s = self._sort_xy(vx_up, y_up)
        vx_dn_s, y_dn_s = self._sort_xy(vx_dn, y_dn)
        _,      g_up_s  = self._sort_xy(vx_up, g_up)
        _,      g_dn_s  = self._sort_xy(vx_dn, g_dn)

        # Ocultar el 0 V solo en Down para el plot (CSV queda intacto)
        vx_dn_s, y_dn_s, g_dn_s = self._mask_zero_in_dn_for_plot(vx_dn_s, y_dn_s, g_dn_s)

        # Set data
        self.line_id_up.set_data(vx_up_s, y_up_s)
        self.line_id_dn.set_data(vx_dn_s, y_dn_s)
        self.line_ig_up.set_data(vx_up_s, g_up_s)
        self.line_ig_dn.set_data(vx_dn_s, g_dn_s)

        # Autoscale
        def autoscale(ax, xs, ys):
            allx = np.array(list(xs[0]) + list(xs[1]), dtype=float) if (len(xs[0]) or len(xs[1])) else np.array([])
            ally = np.concatenate([ys[0], ys[1]]) if (getattr(ys[0], "size", 0) or getattr(ys[1], "size", 0)) else np.array([])
            if allx.size and np.isfinite(allx).any() and ally.size and np.isfinite(ally).any():
                xfinite = allx[np.isfinite(allx)]
                yfinite = ally[np.isfinite(ally)]
                if xfinite.size and yfinite.size:
                    vx = max(1e-12, float(np.nanmax(xfinite) - np.nanmin(xfinite)))
                    vy = max(1e-12, float(np.nanmax(yfinite) - np.nanmin(yfinite)))
                    ax.set_xlim(float(np.nanmin(xfinite))-0.05*vx, float(np.nanmax(xfinite))+0.05*vx)
                    ymin = float(np.nanmin(yfinite))
                    ymax = float(np.nanmax(yfinite))
                    if ax.get_yscale() == "log":
                        ymin_pos = ymin if ymin > 0 else (np.nanmin(yfinite[yfinite>0]) if np.any(yfinite>0) else 1e-18)
                        ax.set_ylim(ymin_pos/1.2, (ymax*1.2 if ymax>0 else 1e-18*10))
                    else:
                        ax.set_ylim(ymin-0.1*vy, ymax+0.1*vy)

        autoscale(self.ax_id, (vx_up_s, vx_dn_s), (y_up_s, y_dn_s))
        autoscale(self.ax_ig, (vx_up_s, vx_dn_s), (g_up_s, g_dn_s))

        self.canvas.draw_idle()

    # ----------------- Save -----------------
    def on_save_csv(self):
        if not self.data: messagebox.showinfo("Info","No data to save."); return
        path=filedialog.asksaveasfilename(defaultextension=".csv",
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            filetypes=[("CSV files","*.csv")], initialfile="id_ig_vs_vgs_TSP.csv")
        if not path: return
        try:
            data_sorted = sorted(self.data, key=lambda x: float(x[0]))
            # IMPORTANTE: encoding='utf-8' y usar ASCII en headers (-> en vez de flechas Unicode)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w=csv.writer(f)
                # Metadata header
                f.write("# Mode: TSP-only (2636B)\n")
                f.write("# ZERO-SAFE sweep (0->+max, 0->min) with 0V stitched: reuse same 0V reading in Up/Down.\n")
                f.write(f"# User range: start={self.var_start.get()}, stop={self.var_stop.get()}, step={self.var_step.get()}\n")
                f.write(f"# VDS const (B) [V]: {self.var_vds.get()}\n")
                f.write(f"# NPLC: {self.var_nplc.get()}, Delay per point [s]: {self.var_delay.get()}\n")
                f.write(f"# Ramp step [V]: {self.var_ramp_step.get()}, Ramp delay [s]: {self.var_ramp_delay.get()}\n")
                f.write(f"# 4-wire: {self.var_4w.get()}, LogID: {self.var_log_id.get()}, LogIG: {self.var_log_ig.get()}, ClipLog: {self.var_clip_log.get()}\n")
                # Data header
                w.writerow(["VGS_meas_V","IG_A","ID_A","SweepDir","Mode"])
                for vgs, ig, id_, d in data_sorted:
                    w.writerow([vgs, ig, id_, d, "TSP"])
            self.var_status.set(f"Saved CSV -> {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save CSV:\n{e}")

    def on_save_png(self):
        if not self.data: messagebox.showinfo("Info","No data to save."); return
        path=filedialog.asksaveasfilename(defaultextension=".png",
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            filetypes=[("PNG files","*.png")], initialfile="id_ig_vs_vgs_TSP.png")
        if not path: return
        try:
            self.fig.savefig(path, dpi=150, bbox_inches="tight")
            self.var_status.set(f"Saved PNG -> {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save PNG:\n{e}")

    def _soft_ramp_to_zero_and_off(self):
        if not self.dev: return
        try:
            ramp_step = float(self.var_ramp_step.get()); ramp_delay = float(self.var_ramp_delay.get())
        except:
            ramp_step, ramp_delay = 0.02, 0.01
        try:
            self.dev.ramp_smua_to(0.0, ramp_step, ramp_delay)
        except: pass
        try:
            self.dev.ramp_smub_to(0.0, ramp_step, ramp_delay)
        except: pass
        try:
            self.dev.outputs_off()
        except: pass

    def on_closing(self):
        self.stop_flag.set()
        try:
            self._soft_ramp_to_zero_and_off()
            if self.k: self.k.close()
        except: pass
        self.destroy()

if __name__=="__main__":
    app=App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
