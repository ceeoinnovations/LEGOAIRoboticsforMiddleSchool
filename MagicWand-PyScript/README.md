# Magic Wand (LEGO Education, browser/PyScript)

A browser-based app that teaches machine learning by having students train a
LEGO Education Double Motor's built-in IMU (accelerometer + gyroscope) to
recognize "spells" — hand gestures performed while holding the wand. Runs
entirely client-side via [PyScript](https://pyscript.net) (Pyodide/WASM) over
**Web Bluetooth** — no server, no install, no `legoeducation` pip package on
the student's machine. Same architecture as the sibling `QLearn-*-PyScript`
apps in `FETLab-Summer-2026`.

## The five lessons (tabs)

1. **Teach Your Wand a Spell** (supervised learning) — record several examples
   of one gesture ("Fireball"), train a classifier, then test recognition.
2. **Can Your Friend Use It?** (generalization & bias) — a different person
   tries the *same* trained model, with no retraining. Usually degrades —
   a natural entry point for talking about training-data bias.
3. **Improve It** (fine-tuning) — fold the friend's attempts into the
   training set, retrain, and compare before/after accuracy.
4. **Discover Hidden Magic Styles** (unsupervised learning) — record several
   *unlabeled* mystery motions and let k-means find the groups on its own.
5. **Make New Spells** — name the discovered clusters and cast them for real,
   with the wand lighting up and beeping differently per recognized spell.

## Files

- **`index.html`** — layout, styling, the five lesson tabs, and a `<script>`
  block of DOM-manipulation helpers exposed on `window` (tab switching,
  press-and-hold button state, live trace sparkline, results tables, the
  cluster scatter plot, cluster-naming inputs). Called from Python — the JS
  itself holds no ML/training logic. Also loads PyScript and points it at
  `main.py`/`pyscript.toml`.
- **`main.py`** — runs in-browser as Python via PyScript:
  - **BLE device wiring** — bridges the `legoeducation` package's
    `DoubleMotor` class (normally backed by a background thread + `bleak`,
    i.e. real OS Bluetooth) through `ble.js`'s Web Bluetooth wrapper instead,
    via the same WASM worker patch used in the QLearn apps.
  - **Gesture capture** — press-and-hold buttons. While held, polls the
    Double Motor's live IMU reading (`dm_device.imu_device`, kept
    up to date by `legoeducation`'s own notification parsing) every 50ms and
    buffers `[ax, ay, az, gx, gy, gz]`.
  - **Lesson logic** — one set of handlers per lesson: recording examples,
    training/retraining, running test casts, clustering, naming clusters,
    and light/beep feedback on recognized spells.
- **`wand_ml.py`** — the actual ML, independent of hardware/UI (same role
  `qlearn.py` plays in the QLearn apps): `resample_trace` (fixed-length
  interpolation so gestures of different durations compare directly),
  `KNNClassifier` (nearest-neighbor recognition with a distance-based
  confidence score), `KMeansCluster` (from-scratch k-means). No
  scikit-learn — implemented from scratch so the algorithm stays
  inspectable/explainable to students.
- **`ble.js`** — the Web Bluetooth wrapper (`navigator.bluetooth`), imported
  into `main.py` as a PyScript JS module. Verbatim copy from
  `QLearn-NewActivities-PyScript` — this is the only place that talks to
  real Bluetooth hardware, and is shared across every LEGO Education app in
  this style since they all speak the same BLE service/characteristic UUIDs.
- **`pyscript.toml`** — declares the Python packages to install
  (`legoeducation`, `numpy`), the local Python file to make importable
  (`wand_ml.py`), and the JS module mapping (`ble.js` → `ble`).

## Running locally

Web Bluetooth requires a "secure context" — `https://` or `http://localhost`.
Opening `index.html` directly as a `file://` URL will NOT work. From this
directory:

```bash
python3 serve.py
```

(Not plain `python3 -m http.server` — that sends no `Cache-Control` header,
so the browser can silently keep running a stale cached copy of `main.py`/
`wand_ml.py` after you edit them. `serve.py` is the same server with
no-cache headers added, so a normal reload always picks up your latest
edit.)

Then open **http://localhost:8000** in Chrome or Edge (Web Bluetooth isn't
supported in Safari/Firefox). Click **Connect** in the sidebar, pick your
Double Motor hub from the browser's device picker, and it streams IMU data
straight into the page.

If you ever suspect stale code is still running (behavior doesn't match a
recent edit), a hard refresh may not be enough on its own — close every tab
with this app open, then reopen a fresh one, or use a private/incognito
window.

## Notes / things to tune with a class

- **Recognition threshold** (Lesson 1 slider): how close a cast needs to be
  to count as "recognized." Lower it if a class's gestures are sloppy/varied;
  raise it to make recognition stricter.
- **Number of groups** (Lesson 4 slider): k-means needs to be told how many
  clusters to look for. If a teacher secretly assigned 3 distinct motions
  across the class, 3 is the "right" answer — but it's worth having students
  try other values and see how the grouping changes.
- Datasets and trained models live only in the browser tab's memory — a page
  reload clears everything. There's no save/load yet; each class period (or
  each lesson-to-lesson handoff) starts fresh unless you keep the tab open.
