import asyncio
import json
from datetime import datetime, timezone
from pyscript import document, window
from pyodide.ffi import create_proxy

import legoeducation as le
import legoeducation.background_worker as bw
from pyscript.js_modules.ble import BLEDevice as _BLEDeviceJS
from legoeducation import DoubleMotor as _DM
from wand_ml import KNNClassifier, KMeansCluster, summary_features, accel_magnitude, detect_motion_start

# ── WASM WORKER PATCH ──
# legoeducation normally runs its BLE I/O on a background thread talking to
# `bleak` (real OS Bluetooth). Pyodide/PyScript is single-threaded and has no
# OS Bluetooth access at all, so this reroutes the same internal worker
# queue through ble.js's Web Bluetooth wrapper instead. Verbatim copy of the
# same patch used in the sibling QLearn-*-PyScript apps — this part has
# nothing to do with gestures/ML and shouldn't need to change here.
def _wasm_start_thread(self):
    if getattr(self, '_wasm_loop_started', False): return
    self._wasm_loop_started = True
    if not hasattr(self, '_js_ble_registry'): self._js_ble_registry = {}
    try:
        self.loop = asyncio.get_running_loop()
        self.loop_ready.set()
        asyncio.ensure_future(_wasm_worker_loop(self))
    except RuntimeError: pass

def _wasm_put_request(self, request):
    if not self.loop_ready.is_set():
        try:
            self.loop = asyncio.get_running_loop()
            self.loop_ready.set()
            if not hasattr(self, '_js_ble_registry'): self._js_ble_registry = {}
            asyncio.ensure_future(_wasm_worker_loop(self))
        except RuntimeError: return
    asyncio.ensure_future(self.async_put_request(request))

bw.Worker.start_thread = _wasm_start_thread
bw.Worker.put_request = _wasm_put_request

async def _wasm_worker_loop(worker):
    while True:
        try:
            req = await worker.request_queue.get()
            if req is None: break
            topic = req.get('topic')
            if topic == 'send':
                device = req.get('msg')
                message = req.get('msg2')
                js_ble = worker._js_ble_registry.get(id(device))
                if js_ble and message:
                    await js_ble.send(list(message))
            elif topic == 'connect':
                cb = req.get('msg3')
                if cb: cb(True)
            elif topic == 'disconnect':
                device = req.get('msg')
                js_ble = worker._js_ble_registry.pop(id(device), None)
                if js_ble: js_ble.disconnect()
        except Exception as e: print(f"Worker error: {e}")

SERVICE_UUID = '0000fd02-0000-1000-8000-00805f9b34fb'
WRITE_UUID   = '0000fd02-0001-1000-8000-00805f9b34fb'
NOTIFY_UUID  = '0000fd02-0002-1000-8000-00805f9b34fb'


def log(msg):
    term = document.getElementById('terminal')
    div = document.createElement('div')
    div.innerText = msg
    term.appendChild(div)
    term.scrollTop = term.scrollHeight
    print(msg)


# ── DEVICE MANAGEMENT (same WebDevice bridge pattern as the QLearn apps) ──
class WebDevice:
    def __init__(self, prefix):
        self.prefix = prefix
        self.connected = False
        self.js_ble = None

    async def connect_web(self):
        js_ble = _BLEDeviceJS.new()
        self._notification_proxy = create_proxy(lambda data: asyncio.ensure_future(self._on_notification(bytes(data.to_py()))))
        self._disconnect_proxy = create_proxy(self._on_disconnect)
        js_ble.callback = self._notification_proxy
        js_ble.disconnectCallback = self._disconnect_proxy

        success = await js_ble.connect(SERVICE_UUID, WRITE_UUID, NOTIFY_UUID)
        if success:
            self.js_ble = js_ble
            self.connected = True
            self.device = self
            import legoeducation.basic_device as bd
            bd.my_worker._js_ble_registry[id(self)] = self.js_ble
            try:
                self.device_notification_request(100, blocking=False)
            except: pass

            dot = document.getElementById(f'{self.prefix}-dot')
            if dot: dot.classList.add('connected')
            btn = document.getElementById(f'btn-connect-{self.prefix}')
            if btn:
                btn.innerText = 'Connected'
                btn.disabled = True
            btn_dis = document.getElementById(f'btn-disconnect-{self.prefix}')
            if btn_dis:
                btn_dis.style.display = 'inline-block'
            log(f"Wand connected.")
            check_ready()

    async def _on_notification(self, data: bytes):
        await self._device_callback(NOTIFY_UUID, data)

    def _on_disconnect(self, event):
        self.connected = False
        dot = document.getElementById(f'{self.prefix}-dot')
        if dot: dot.classList.remove('connected')
        btn = document.getElementById(f'btn-connect-{self.prefix}')
        if btn:
            btn.innerText = 'Connect'
            btn.disabled = False
        btn_dis = document.getElementById(f'btn-disconnect-{self.prefix}')
        if btn_dis:
            btn_dis.style.display = 'none'
        log("Wand disconnected.")
        check_ready()

    def disconnect_web(self):
        if self.js_ble:
            self.js_ble.disconnect()
            self._on_disconnect(None)

    def send_command(self, packet):
        if self.js_ble is None:
            return
        if isinstance(packet, (bytes, bytearray)):
            packet = list(packet)
        asyncio.ensure_future(self.js_ble.send(packet))


class DoubleMotorDevice(WebDevice, _DM):
    def __init__(self):
        _DM.__init__(self)
        WebDevice.__init__(self, 'dm')

dm_device = DoubleMotorDevice()

def connect_dm(e=None):
    asyncio.ensure_future(dm_device.connect_web())

def disconnect_dm(e=None):
    dm_device.disconnect_web()

def check_ready():
    """Gates every record/cast button on wand connection, and additionally
    on whether the relevant classifier has been trained yet — called on
    every connect/disconnect, so it must not blindly re-enable a Cast
    button whose model doesn't exist yet."""
    connected = dm_device.connected
    for btn_id in ('btn-l1-record', 'btn-l5-record'):
        el = document.getElementById(btn_id)
        if el:
            el.disabled = not connected
    for btn_id in ('btn-l1-cast', 'btn-l2-cast', 'btn-l3-cast', 'btn-l4-cast'):
        el = document.getElementById(btn_id)
        if el:
            el.disabled = not (connected and knn_fireball is not None)
    el = document.getElementById('btn-lwho-cast')
    if el:
        el.disabled = not (connected and knn_caster is not None)
    el = document.getElementById('btn-l6-cast')
    if el:
        el.disabled = not (connected and knn_spells is not None)


# ══════════════════════════════════════════════════════════════
# GESTURE CAPTURE — shared by every lesson. A "recording" is: hold a
# button down, wave the wand, let go. While held, we poll the Double
# Motor's live IMU reading (dm_device.imu_device, kept up to date by the
# legoeducation package's own internal notification parsing — same object
# the QLearn apps poll for yaw) every 50ms and buffer [ax,ay,az,gx,gy,gz].
# ══════════════════════════════════════════════════════════════

_recording = False
_record_buffer = []
_record_task = None
_record_target = None

def _read_imu_sample():
    imu = dm_device.imu_device
    try:
        return [
            float(imu.accelerometerX), float(imu.accelerometerY), float(imu.accelerometerZ),
            float(imu.gyroscopeX), float(imu.gyroscopeY), float(imu.gyroscopeZ),
        ]
    except Exception:
        return [0.0] * 6

async def _record_loop():
    global _recording
    while _recording:
        sample = _read_imu_sample()
        _record_buffer.append(sample)
        window.drawTracePreview(f'trace-canvas-{_record_target}', sample)
        await asyncio.sleep(0.05)

def _begin_record(target):
    global _recording, _record_buffer, _record_task, _record_target
    if not dm_device.connected:
        log("Connect the wand first.")
        return
    if _recording:
        return
    _record_target = target
    _record_buffer = []
    _recording = True
    window.resetTraceCanvas(f'trace-canvas-{target}')
    window.setRecordingUI(f'btn-{target}', True)
    _record_task = asyncio.ensure_future(_record_loop())

def _end_record():
    """Stops buffering and returns the raw trace, or None if it was too
    short to be a real gesture (e.g. an accidental click)."""
    global _recording, _record_task
    if not _recording:
        return None
    _recording = False
    if _record_task:
        _record_task.cancel()
        _record_task = None
    window.setRecordingUI(f'btn-{_record_target}', False)
    trace = list(_record_buffer)
    if len(trace) < 4:
        log("That was too quick — hold the button for the whole motion, then let go.")
        return None
    return trace


# ══════════════════════════════════════════════════════════════
# DATASETS — shared gesture storage across lessons.
# ══════════════════════════════════════════════════════════════

_datasets = {
    'fireball': [],      # [{'label': 'Fireball', 'trace': [...], 'source': 'you' | tester name}]
    'friend_test': [],   # [{'tester': str, 'trace': [...], 'added_to_training': bool,
                         #   'cropped': bool}]  # 'cropped' added lazily by Lesson 5
    'mystery': [],       # [{'trace': [...]}]
}

def _refresh_counts():
    for key, el_id in (('fireball', 'count-fireball'), ('friend_test', 'count-friend-test'), ('mystery', 'count-mystery')):
        el = document.getElementById(el_id)
        if el:
            el.innerText = str(len(_datasets[key]))
    btn = document.getElementById('btn-lwho-train')
    if btn:
        traces, labels = _caster_training_data()
        btn.disabled = len(set(labels)) < 2 or len(traces) < 6


# ══════════════════════════════════════════════════════════════
# LESSON 1 — Teach Your Wand a Spell (supervised learning: one gesture)
# ══════════════════════════════════════════════════════════════

knn_fireball = None  # KNNClassifier, set once Lesson 1's "Train" button is clicked
_last_cast_magnitude = None  # accel-magnitude trace of the most recent Lesson 1 test cast, or None

def _refresh_l1_overlay():
    """Redraws the 'compare your examples' chart: one line per recorded
    Fireball example (raw elapsed time, so uneven start-of-recording dead
    time shows up as visibly different), plus the most recent test cast
    highlighted on top so students can see how it lines up against the
    training set."""
    traces = [accel_magnitude(ex['trace']) for ex in _datasets['fireball']]
    window.renderExampleOverlay('l1-overlay-canvas', traces, 0.05, _last_cast_magnitude)

def l1_record_start(e=None):
    _begin_record('l1-record')

def l1_record_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    _datasets['fireball'].append({'label': 'Fireball', 'trace': trace, 'source': 'you'})
    _refresh_counts()
    _refresh_l1_overlay()
    n = len(_datasets['fireball'])
    log(f"Recorded Fireball example #{n}.")
    btn = document.getElementById('btn-l1-train')
    if btn:
        btn.disabled = n < 3
    if n < 3:
        log(f"Record {3 - n} more example(s) before training (3 minimum, 6-8 recommended).")

def l1_train(e=None):
    global knn_fireball
    examples = _datasets['fireball']
    if len(examples) < 3:
        log("Record at least 3 Fireball examples first.")
        return
    knn_fireball = KNNClassifier(k=min(3, len(examples)))
    knn_fireball.fit([ex['trace'] for ex in examples], [ex['label'] for ex in examples])
    log(f"Trained on {len(examples)} example(s). Try casting it below!")
    check_ready()

def _read_threshold(slider_id, default=0.5):
    el = document.getElementById(slider_id)
    try:
        return float(el.value) / 100.0
    except Exception:
        return default

def l1_cast_start(e=None):
    if knn_fireball is None:
        log("Train the wand first.")
        return
    _begin_record('l1-cast')

def l1_cast_stop(e=None):
    global _last_cast_magnitude
    trace = _end_record()
    if trace is None:
        return
    if knn_fireball is None:
        log("Train the wand first.")
        return
    label, confidence, _ = knn_fireball.predict(trace)
    threshold = _read_threshold('l1-threshold')
    recognized = confidence >= threshold
    window.setBadge('l1-cast-result', 'success' if recognized else 'fail',
                     f"\U0001F525 Fireball recognized! ({confidence*100:.0f}% match)" if recognized
                     else f"Not recognized ({confidence*100:.0f}% match, needs {threshold*100:.0f}%)")
    _last_cast_magnitude = accel_magnitude(trace)
    _refresh_l1_overlay()

def l1_export_session(e=None):
    """Class 1 counterpart to l5_export_motions: serializes this tab's raw
    Lesson 1/2 recordings (the source data every Class 1 lesson reads from
    — see _datasets above) to a JSON file. Trained models and lesson
    result tables are deliberately NOT saved, since KNN's 'model' is just
    the stored examples anyway — loading a file back in is meant to be
    followed by clicking Train/Retrain again, not treated as restoring a
    finished session."""
    name_el = document.getElementById('l1-student-name')
    student = (name_el.value or 'Student').strip() if name_el else 'Student'
    fireball = _datasets['fireball']
    friend_test = _datasets['friend_test']
    if not fireball and not friend_test:
        log("No Class 1 data recorded yet — nothing to save.")
        return
    payload = {
        'student': student,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'fireball': fireball,
        'friend_test': friend_test,
    }
    safe_name = ''.join(c if c.isalnum() else '_' for c in student) or 'student'
    filename = f"class1-session-{safe_name}.json"
    window.downloadJSON(filename, json.dumps(payload))
    log(f"Saved {len(fireball)} Fireball example(s) and {len(friend_test)} friend test cast(s) to {filename}.")

def _import_class1_json(text):
    global knn_fireball, knn_caster
    try:
        data = json.loads(text)
    except Exception as ex:
        log(f"Could not read that file as JSON ({ex}).")
        return
    fireball = data.get('fireball') if isinstance(data, dict) else None
    friend_test = data.get('friend_test') if isinstance(data, dict) else None
    if not isinstance(fireball, list) and not isinstance(friend_test, list):
        log("That file doesn't look like a saved Class 1 session — skipped.")
        return
    student = (data.get('student') or 'Imported') if isinstance(data, dict) else 'Imported'

    added_fireball = 0
    for ex in (fireball or []):
        trace = ex.get('trace') if isinstance(ex, dict) else None
        if isinstance(trace, list) and len(trace) >= 4:
            _datasets['fireball'].append({
                'label': ex.get('label', 'Fireball'),
                'trace': trace,
                'source': ex.get('source', 'you'),
            })
            added_fireball += 1

    added_friend = 0
    for rec in (friend_test or []):
        trace = rec.get('trace') if isinstance(rec, dict) else None
        if isinstance(trace, list) and len(trace) >= 4:
            _datasets['friend_test'].append({
                'tester': rec.get('tester', student),
                'trace': trace,
                'added_to_training': bool(rec.get('added_to_training', False)),
                'cropped': bool(rec.get('cropped', False)),
            })
            added_friend += 1

    # A freshly-loaded session has no trained model yet — clear any model
    # trained on this tab's own prior data so nobody casts against a stale
    # mix of old and newly-loaded examples.
    knn_fireball = None
    knn_caster = None
    _refresh_counts()
    _refresh_l1_overlay()
    train_btn = document.getElementById('btn-l1-train')
    if train_btn:
        train_btn.disabled = len(_datasets['fireball']) < 3
    check_ready()
    log(f"Loaded {added_fireball} Fireball example(s) and {added_friend} friend test cast(s) from {student}. "
        f"Click Train Wand to use them.")

def _on_class1_import_file(event):
    try:
        text = str(event.detail.text)
    except Exception:
        return
    _import_class1_json(text)

_class1_import_proxy = create_proxy(_on_class1_import_file)
document.addEventListener('class1-import-file', _class1_import_proxy)

def l1_clear(e=None):
    global knn_fireball, _last_cast_magnitude
    _datasets['fireball'] = []
    knn_fireball = None
    _last_cast_magnitude = None
    _refresh_counts()
    document.getElementById('btn-l1-train').disabled = True
    check_ready()
    window.setBadge('l1-cast-result', 'neutral', 'Train the wand to try casting.')
    _refresh_l1_overlay()
    log("Cleared Fireball training data.")


# ══════════════════════════════════════════════════════════════
# LESSON 2 — Can Your Friend Use It? (generalization & bias, no retraining)
# ══════════════════════════════════════════════════════════════

_lesson2_results = []

def l2_cast_start(e=None):
    if knn_fireball is None:
        log("Complete Lesson 1 first — there's no trained spell yet.")
        return
    _begin_record('l2-cast')

def l2_cast_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    tester_el = document.getElementById('l2-tester-name')
    tester = (tester_el.value or 'Friend').strip() if tester_el else 'Friend'
    label, confidence, _ = knn_fireball.predict(trace)
    threshold = _read_threshold('l1-threshold')
    recognized = confidence >= threshold

    _lesson2_results.append({'tester': tester, 'recognized': recognized, 'confidence': confidence})
    _datasets['friend_test'].append({'tester': tester, 'trace': trace, 'added_to_training': False})
    _refresh_counts()

    window.appendResultRow('l2-results-body', [tester, '✅' if recognized else '❌', f"{confidence*100:.0f}%"])
    hits = sum(1 for r in _lesson2_results if r['recognized'])
    total = len(_lesson2_results)
    pct = 100.0 * hits / total
    window.setResultsSummary('l2-summary', f"{hits}/{total} casts recognized ({pct:.0f}%)")

def l2_reset(e=None):
    _lesson2_results.clear()
    document.getElementById('l2-results-body').innerHTML = ''
    window.setResultsSummary('l2-summary', 'No attempts yet.')
    log("Cleared Lesson 2 results. Ready for a new tester.")


# ══════════════════════════════════════════════════════════════
# LESSON 3 — Improve It (fine-tuning with the friend's examples)
# ══════════════════════════════════════════════════════════════

_lesson3_results = []

def l3_add_to_training(e=None):
    added = 0
    for rec in _datasets['friend_test']:
        if not rec['added_to_training']:
            _datasets['fireball'].append({'label': 'Fireball', 'trace': rec['trace'], 'source': rec['tester']})
            rec['added_to_training'] = True
            added += 1
    _refresh_counts()
    if added:
        log(f"Added {added} example(s) from Lesson 2 into the training set.")
        document.getElementById('btn-l3-retrain').disabled = False
    else:
        log("No new examples to add — run some test casts in Lesson 2 first.")

def l3_retrain(e=None):
    global knn_fireball
    examples = _datasets['fireball']
    if len(examples) < 3:
        log("Not enough training examples yet.")
        return
    knn_fireball = KNNClassifier(k=min(3, len(examples)))
    knn_fireball.fit([ex['trace'] for ex in examples], [ex['label'] for ex in examples])
    log(f"Retrained on {len(examples)} example(s) (now including your friend's).")
    check_ready()

    hits = sum(1 for r in _lesson2_results if r['recognized'])
    total = len(_lesson2_results)
    before_pct = (100.0 * hits / total) if total else 0.0
    window.updateBeforeAfter('l3', before_pct, None)

def l3_cast_start(e=None):
    if knn_fireball is None:
        log("Retrain the wand first.")
        return
    _begin_record('l3-cast')

def l3_cast_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    tester_el = document.getElementById('l2-tester-name')
    tester = (tester_el.value or 'Friend').strip() if tester_el else 'Friend'
    label, confidence, _ = knn_fireball.predict(trace)
    threshold = _read_threshold('l1-threshold')
    recognized = confidence >= threshold

    _lesson3_results.append({'tester': tester, 'recognized': recognized, 'confidence': confidence})
    window.appendResultRow('l3-results-body', [tester, '✅' if recognized else '❌', f"{confidence*100:.0f}%"])

    hits2 = sum(1 for r in _lesson2_results if r['recognized'])
    total2 = len(_lesson2_results)
    before_pct = (100.0 * hits2 / total2) if total2 else 0.0

    hits3 = sum(1 for r in _lesson3_results if r['recognized'])
    total3 = len(_lesson3_results)
    after_pct = 100.0 * hits3 / total3
    window.updateBeforeAfter('l3', before_pct, after_pct)

def l3_reset(e=None):
    _lesson3_results.clear()
    document.getElementById('l3-results-body').innerHTML = ''
    window.updateBeforeAfter('l3', None, None)
    log("Cleared Lesson 3 retest results.")


# ══════════════════════════════════════════════════════════════
# LESSON 4 — Who Cast It? (multi-class caster-identity classification —
# a different question from Lessons 1-3's "is this a Fireball?": here the
# AI is trained to recognize WHO cast it. Reuses recordings already made
# in Lessons 1-2, so there's no new capture step, just a second,
# independent classifier (knn_caster) trained on caster-identity labels
# instead of spell labels. knn_fireball is untouched.)
# ══════════════════════════════════════════════════════════════

knn_caster = None  # KNNClassifier, set once this lesson's "Train" button is clicked
_lwho_results = []

def _caster_training_data():
    """Pool (trace, caster_label) pairs for the identity classifier: 'you'
    from Lesson 1's examples, plus every Lesson 2 friend test cast labeled
    by that tester's name (one class per distinct name recorded, so a
    class that tested multiple friends gets a multi-way classifier).
    Deliberately reads _datasets['friend_test'] rather than any
    post-Lesson-3-merge copies in _datasets['fireball'], so this doesn't
    depend on Lesson 3 having been completed and never double-counts a
    cast that Lesson 3 already merged in."""
    traces, labels = [], []
    for ex in _datasets['fireball']:
        if ex['source'] == 'you':
            traces.append(ex['trace'])
            labels.append('you')
    for rec in _datasets['friend_test']:
        traces.append(rec['trace'])
        labels.append(rec['tester'])
    return traces, labels

def lwho_train(e=None):
    global knn_caster
    traces, labels = _caster_training_data()
    n_casters = len(set(labels))
    if n_casters < 2:
        log("Record at least one Lesson 2 friend test cast first — need two different casters to compare.")
        return
    if len(traces) < 6:
        log(f"Only {len(traces)} example(s) so far — record a few more casts (yours and your friend's) in Lessons 1-2 first.")
        return
    knn_caster = KNNClassifier(k=min(3, len(traces)))
    knn_caster.fit(traces, labels)
    casters = ', '.join(sorted(set(labels)))
    log(f"Trained to recognize {n_casters} caster(s) ({casters}) from {len(traces)} example(s).")
    check_ready()

def lwho_cast_start(e=None):
    if knn_caster is None:
        log("Train the identity classifier first.")
        return
    _begin_record('lwho-cast')

def lwho_cast_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    if knn_caster is None:
        log("Train the identity classifier first.")
        return
    actual_el = document.getElementById('lwho-actual-name')
    actual = (actual_el.value or '?').strip() if actual_el else '?'
    label, confidence, _ = knn_caster.predict(trace)
    correct = actual.lower() == label.lower()

    _lwho_results.append({'actual': actual, 'guess': label, 'correct': correct})
    window.appendResultRow('lwho-results-body',
                            [actual, label, f"{confidence*100:.0f}%", '✅' if correct else '❌'])
    hits = sum(1 for r in _lwho_results if r['correct'])
    total = len(_lwho_results)
    pct = 100.0 * hits / total
    window.setResultsSummary('lwho-summary', f"{hits}/{total} correctly identified ({pct:.0f}%)")

def lwho_reset(e=None):
    _lwho_results.clear()
    document.getElementById('lwho-results-body').innerHTML = ''
    window.setResultsSummary('lwho-summary', 'No attempts yet.')
    log("Cleared Who Cast It? results.")


# ══════════════════════════════════════════════════════════════
# LESSON 5 — Improve It: Data Cleaning (trim leading dead-time from
# whatever's currently in the Fireball training set — including anything
# added back in Lesson 3 — then retrain and compare, same before/after
# pattern as Lesson 3 but for a different kind of data-quality fix).
# ══════════════════════════════════════════════════════════════

_crop_originals = {}    # example index (into _datasets['fireball']) -> its
                         # original, uncropped trace, captured at Load time
                         # so repeated "Apply Crops" clicks always crop from
                         # the same starting point rather than compounding.
_lesson4_results = []

def l4_load_examples(e=None):
    global _crop_originals
    examples = _datasets['fireball']
    if not examples:
        _crop_originals = {}
        window.renderCropExamples('l4-crop-container', [], [], [], 'fireball')
        document.getElementById('btn-l4-apply-crops').disabled = True
        log("No Fireball examples yet — record some in Lesson 1 first.")
        return
    _crop_originals = {i: ex['trace'] for i, ex in enumerate(examples)}

    indices, traces, suggested = [], [], []
    for i, ex in enumerate(examples):
        start_idx = detect_motion_start(ex['trace'])
        indices.append(i)
        traces.append(accel_magnitude(ex['trace']))
        suggested.append(round(start_idx * 0.05, 3))
    window.renderCropExamples('l4-crop-container', indices, traces, suggested, 'fireball')
    document.getElementById('btn-l4-apply-crops').disabled = False
    log(f"Loaded {len(examples)} example(s) with a suggested crop point each. Drag any marker to adjust, then Apply Crops.")

def l4_delete_example(idx):
    """Removes one example from the Fireball training set outright — for
    recordings that aren't fixable by cropping (e.g. let go of the button
    too early, or the motion itself was wrong). Reloads the crop UI
    afterward so remaining examples get fresh, correctly-shifted indices
    rather than trying to patch indices in place."""
    examples = _datasets['fireball']
    if not (0 <= idx < len(examples)):
        log("Could not remove that example (already removed?).")
        return
    removed = examples.pop(idx)
    _refresh_counts()
    log(f"Removed example #{idx + 1} (source: {removed.get('source', '?')}).")
    l4_load_examples()

def l4_apply_crops(e=None):
    if not _crop_originals:
        log("Load your examples first.")
        return
    examples = _datasets['fireball']
    changed = 0
    for i, original_trace in _crop_originals.items():
        if i >= len(examples):
            continue
        el = document.getElementById(f'l4-crop-value-{i}')
        if el is None:
            continue
        try:
            crop_seconds = float(el.value)
        except Exception:
            crop_seconds = 0.0
        start_idx = max(0, min(len(original_trace) - 4, round(crop_seconds / 0.05)))
        examples[i]['trace'] = original_trace[start_idx:]
        if start_idx > 0:
            changed += 1

    # Also auto-trim dead time from any Lesson 2 friend test casts that
    # haven't been through this crop pass yet — otherwise Lesson 4's
    # identity classifier would train 'you' on cleaned data next to
    # un-cleaned friend data, an unfair/confusing mismatch. Friend casts
    # aren't shown in this lesson's chart at all, so this applies the same
    # detect_motion_start heuristic automatically rather than via a manual
    # marker. 'cropped' guards against re-trimming the same recording every
    # time this button is clicked again.
    friend_changed = 0
    for rec in _datasets['friend_test']:
        if rec.get('cropped'):
            continue
        start_idx = detect_motion_start(rec['trace'])
        if start_idx > 0:
            rec['trace'] = rec['trace'][start_idx:]
            friend_changed += 1
        rec['cropped'] = True

    msg = f"Applied crops: trimmed dead time from {changed} of {len(_crop_originals)} example(s)."
    if friend_changed:
        msg += f" Also auto-trimmed {friend_changed} friend test cast(s) so Lesson 4's identity classifier stays consistent."
    log(msg)
    document.getElementById('btn-l4-retrain').disabled = False

def l4_retrain(e=None):
    global knn_fireball
    examples = _datasets['fireball']
    if len(examples) < 3:
        log("Not enough training examples yet.")
        return
    knn_fireball = KNNClassifier(k=min(3, len(examples)))
    knn_fireball.fit([ex['trace'] for ex in examples], [ex['label'] for ex in examples])
    log(f"Retrained on {len(examples)} cropped example(s).")
    check_ready()

    hits = sum(1 for r in _lesson3_results if r['recognized'])
    total = len(_lesson3_results)
    before_pct = (100.0 * hits / total) if total else 0.0
    window.updateBeforeAfter('l4', before_pct, None)

def l4_cast_start(e=None):
    if knn_fireball is None:
        log("Retrain the wand first.")
        return
    _begin_record('l4-cast')

def l4_cast_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    # No tester-name field of its own: unlike Lesson 2/3, this lesson is
    # about data quality, not about a specific person's casting, so each
    # attempt is just numbered rather than borrowing Lesson 2's tester name
    # (which would silently show "Friend" or whoever was last typed there,
    # regardless of who's actually testing here).
    attempt = f"#{len(_lesson4_results) + 1}"
    label, confidence, _ = knn_fireball.predict(trace)
    threshold = _read_threshold('l1-threshold')
    recognized = confidence >= threshold

    _lesson4_results.append({'recognized': recognized, 'confidence': confidence})
    window.appendResultRow('l4-results-body', [attempt, '✅' if recognized else '❌', f"{confidence*100:.0f}%"])

    hits3 = sum(1 for r in _lesson3_results if r['recognized'])
    total3 = len(_lesson3_results)
    before_pct = (100.0 * hits3 / total3) if total3 else 0.0

    hits4 = sum(1 for r in _lesson4_results if r['recognized'])
    total4 = len(_lesson4_results)
    after_pct = 100.0 * hits4 / total4
    window.updateBeforeAfter('l4', before_pct, after_pct)

def l4_reset(e=None):
    _lesson4_results.clear()
    document.getElementById('l4-results-body').innerHTML = ''
    window.updateBeforeAfter('l4', None, None)
    log("Cleared Lesson 4 retest results.")


# ══════════════════════════════════════════════════════════════
# CLASS 2, LESSON 1 — Discover Hidden Magic Styles (unsupervised learning: k-means).
# Ids stay l5/l6 (unchanged since before the Class 1/2 split) even though
# they display as "Class 2, Lesson 1/2" now — see index.html's CLASS_LESSONS.
# ══════════════════════════════════════════════════════════════

km_mystery = None
_mystery_assignments = []

def l5_record_start(e=None):
    _begin_record('l5-record')

def l5_record_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    _datasets['mystery'].append({'trace': trace, 'source': 'you'})
    _refresh_counts()
    n = len(_datasets['mystery'])
    log(f"Recorded mystery motion #{n} (label withheld from the AI).")
    document.getElementById('btn-l5-find-patterns').disabled = n < 4

def l5_export_motions(e=None):
    """No server involved: serializes this tab's mystery motions to a JSON
    string and triggers a normal browser file download. Meant to be handed
    off out-of-band (Drive, a classroom app, email, etc.) to whoever will
    import everyone's files and run Find Patterns over the pooled set."""
    name_el = document.getElementById('l5-student-name')
    student = (name_el.value or 'Student').strip() if name_el else 'Student'
    motions = [{'trace': rec['trace']} for rec in _datasets['mystery']]
    if not motions:
        log("No mystery motions recorded yet — nothing to export.")
        return
    payload = {
        'student': student,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'motions': motions,
    }
    safe_name = ''.join(c if c.isalnum() else '_' for c in student) or 'student'
    filename = f"mystery-motions-{safe_name}.json"
    window.downloadJSON(filename, json.dumps(payload))
    log(f"Exported {len(motions)} motion(s) to {filename}.")

def _import_mystery_json(text):
    try:
        data = json.loads(text)
    except Exception as ex:
        log(f"Could not read that file as JSON ({ex}).")
        return
    motions = data.get('motions') if isinstance(data, dict) else None
    if not isinstance(motions, list):
        log("That file doesn't look like an exported mystery-motions file — skipped.")
        return
    student = (data.get('student') or 'Imported') if isinstance(data, dict) else 'Imported'
    added = 0
    for m in motions:
        trace = m.get('trace') if isinstance(m, dict) else None
        if isinstance(trace, list) and len(trace) >= 4:
            _datasets['mystery'].append({'trace': trace, 'source': student})
            added += 1
    _refresh_counts()
    n = len(_datasets['mystery'])
    document.getElementById('btn-l5-find-patterns').disabled = n < 4
    log(f"Imported {added} motion(s) from {student}.")

def _on_mystery_import_file(event):
    try:
        text = str(event.detail.text)
    except Exception:
        return
    _import_mystery_json(text)

_mystery_import_proxy = create_proxy(_on_mystery_import_file)
document.addEventListener('mystery-import-file', _mystery_import_proxy)

def l5_find_patterns(e=None):
    global km_mystery, _mystery_assignments
    traces = [rec['trace'] for rec in _datasets['mystery']]
    if len(traces) < 4:
        log("Record at least 4 mystery motions first.")
        return
    k_el = document.getElementById('l5-k')
    k = int(k_el.value) if k_el else 3
    km_mystery = KMeansCluster(k=k, seed=0)
    _mystery_assignments, inertia = km_mystery.fit(traces)

    # Passed as parallel flat lists (not a list of dicts) since Pyodide
    # converts a Python dict into a JS Map, not a plain object — a Map
    # doesn't support the `.bigness` dot-access the JS side wants.
    indices, clusters, bignesses, twistinesses = [], [], [], []
    for i, (rec, cluster) in enumerate(zip(_datasets['mystery'], _mystery_assignments)):
        bigness, twistiness = summary_features(rec['trace'])
        indices.append(i + 1)
        clusters.append(int(cluster))
        bignesses.append(bigness)
        twistinesses.append(twistiness)
    window.renderClusterScatter('l5-scatter-canvas', indices, clusters, bignesses, twistinesses)
    window.setResultsSummary('l5-summary', f"Found {k} group(s) across {len(traces)} motions. (spread score: {inertia:.1f}, lower = tighter groups)")
    document.getElementById('btn-l6-load-clusters').disabled = False
    log(f"Clustered {len(traces)} mystery motions into {k} groups.")

def l5_clear(e=None):
    global km_mystery, _mystery_assignments
    _datasets['mystery'] = []
    km_mystery = None
    _mystery_assignments = []
    _refresh_counts()
    document.getElementById('btn-l5-find-patterns').disabled = True
    window.resetTraceCanvas('l5-scatter-canvas')
    window.setResultsSummary('l5-summary', 'No patterns found yet.')
    log("Cleared mystery motions.")


# Optional mystery-motion crop tool — same dead-time trimming as Lesson 5's
# Fireball crop tool, reusing the same JS renderer (see renderCropExamples
# in index.html) with 'l5-mystery-crop-container'/'mystery' instead of
# 'l4-crop-container'/'fireball' so the two stay independent. Optional
# because, unlike Lessons 1-4's labeled recognition, k-means just compares
# shapes — dead time is less likely to break it outright, so this is framed
# as a "try this if patterns look messy" extra rather than a required step.
_crop_originals_mystery = {}   # example index (into _datasets['mystery']) -> its
                                # original, uncropped trace, captured at Load time.

def l5_load_mystery_examples(e=None):
    global _crop_originals_mystery
    examples = _datasets['mystery']
    if not examples:
        _crop_originals_mystery = {}
        window.renderCropExamples('l5-mystery-crop-container', [], [], [], 'mystery')
        document.getElementById('btn-l5-apply-mystery-crops').disabled = True
        log("No mystery motions yet — record some above first.")
        return
    _crop_originals_mystery = {i: ex['trace'] for i, ex in enumerate(examples)}

    indices, traces, suggested = [], [], []
    for i, ex in enumerate(examples):
        start_idx = detect_motion_start(ex['trace'])
        indices.append(i)
        traces.append(accel_magnitude(ex['trace']))
        suggested.append(round(start_idx * 0.05, 3))
    window.renderCropExamples('l5-mystery-crop-container', indices, traces, suggested, 'mystery')
    document.getElementById('btn-l5-apply-mystery-crops').disabled = False
    log(f"Loaded {len(examples)} mystery motion(s) with a suggested crop point each. Drag any marker to adjust, then Apply Crops.")

def l5_delete_mystery_example(idx):
    """Removes one mystery motion outright — same reasoning as Lesson 5's
    Fireball crop tool's delete button. Reloads the crop UI afterward so
    remaining examples get fresh, correctly-shifted indices."""
    examples = _datasets['mystery']
    if not (0 <= idx < len(examples)):
        log("Could not remove that mystery motion (already removed?).")
        return
    examples.pop(idx)
    _refresh_counts()
    log(f"Removed mystery motion #{idx + 1}.")
    l5_load_mystery_examples()

def l5_apply_mystery_crops(e=None):
    if not _crop_originals_mystery:
        log("Load your mystery motions first.")
        return
    examples = _datasets['mystery']
    changed = 0
    for i, original_trace in _crop_originals_mystery.items():
        if i >= len(examples):
            continue
        el = document.getElementById(f'l5-mystery-crop-value-{i}')
        if el is None:
            continue
        try:
            crop_seconds = float(el.value)
        except Exception:
            crop_seconds = 0.0
        start_idx = max(0, min(len(original_trace) - 4, round(crop_seconds / 0.05)))
        examples[i]['trace'] = original_trace[start_idx:]
        if start_idx > 0:
            changed += 1
    log(f"Applied crops: trimmed dead time from {changed} of {len(_crop_originals_mystery)} mystery motion(s). "
        "Click Find Patterns again to see the effect.")


# ══════════════════════════════════════════════════════════════
# CLASS 2, LESSON 2 — Make New Spells (name the discovered clusters, recognize them)
# ══════════════════════════════════════════════════════════════

_cluster_names = {}   # {cluster_id: name}
knn_spells = None
SPELL_LIGHT = [le.LEGO_COLOR_RED, le.LEGO_COLOR_BLUE, le.LEGO_COLOR_GREEN,
               le.LEGO_COLOR_PURPLE, le.LEGO_COLOR_ORANGE, le.LEGO_COLOR_AZURE]

def l6_load_clusters(e=None):
    if km_mystery is None or not _mystery_assignments:
        log("Run 'Find Patterns' in Lesson 5 first.")
        return
    cluster_ids = sorted(set(_mystery_assignments))
    counts = [_mystery_assignments.count(cid) for cid in cluster_ids]
    window.renderClusterNaming('l6-naming-container', cluster_ids, counts)
    document.getElementById('btn-l6-save-names').disabled = False
    log(f"Loaded {len(cluster_ids)} cluster(s) from Lesson 5 — name them below.")

def l6_save_names(e=None):
    global _cluster_names, knn_spells
    cluster_ids = sorted(set(_mystery_assignments))
    _cluster_names = {}
    for cid in cluster_ids:
        el = document.getElementById(f'l6-name-{cid}')
        name = (el.value or f'Spell {cid + 1}').strip() if el else f'Spell {cid + 1}'
        _cluster_names[cid] = name

    traces = [rec['trace'] for rec in _datasets['mystery']]
    labels = [_cluster_names[c] for c in _mystery_assignments]
    knn_spells = KNNClassifier(k=3)
    knn_spells.fit(traces, labels)
    log(f"Saved spells: {', '.join(_cluster_names.values())}. Try casting them below!")
    check_ready()

def l6_cast_start(e=None):
    if knn_spells is None:
        log("Name and save your spells first.")
        return
    _begin_record('l6-cast')

def l6_cast_stop(e=None):
    trace = _end_record()
    if trace is None:
        return
    label, confidence, _ = knn_spells.predict(trace)
    window.setBadge('l6-cast-result', 'success', f"✨ {label}! ({confidence*100:.0f}% match)")

    ordered_names = sorted(_cluster_names.values())
    try:
        idx = ordered_names.index(label)
    except ValueError:
        idx = 0
    color = SPELL_LIGHT[idx % len(SPELL_LIGHT)]
    try:
        dm_device.light_color(color, pattern=le.LIGHT_PATTERN_PULSE, intensity=100, blocking=False)
        dm_device.beep(le.SOUND_PATTERN_BEEP_SINGLE, frequency=440 + idx * 120, blocking=False)
    except Exception as ex:
        log(f"(hardware feedback skipped: {ex})")


# ── Press-and-hold wiring ──
# Record/Cast buttons use plain onmousedown/onmouseup/ontouch* JS attributes
# (see index.html) that dispatch CustomEvents, rather than py-mousedown/
# py-mouseup attributes — PyScript's py-* bindings are only confirmed
# reliable here for py-click (see the sibling QLearn apps' note on
# dynamically-injected buttons); this event-dispatch path is the
# already-proven pattern for anything else in this codebase.
_START_HANDLERS = {
    'l1-record': l1_record_start,
    'l1-cast': l1_cast_start,
    'l2-cast': l2_cast_start,
    'l3-cast': l3_cast_start,
    'lwho-cast': lwho_cast_start,
    'l4-cast': l4_cast_start,
    'l5-record': l5_record_start,
    'l6-cast': l6_cast_start,
}
_STOP_HANDLERS = {
    'l1-record': l1_record_stop,
    'l1-cast': l1_cast_stop,
    'l2-cast': l2_cast_stop,
    'l3-cast': l3_cast_stop,
    'lwho-cast': lwho_cast_stop,
    'l4-cast': l4_cast_stop,
    'l5-record': l5_record_stop,
    'l6-cast': l6_cast_stop,
}

def _on_record_start_event(event):
    try:
        target = str(event.detail.target)
    except Exception:
        return
    handler = _START_HANDLERS.get(target)
    if handler:
        handler()

def _on_record_stop_event(event):
    try:
        target = str(event.detail.target)
    except Exception:
        return
    handler = _STOP_HANDLERS.get(target)
    if handler:
        handler()

_record_start_proxy = create_proxy(_on_record_start_event)
_record_stop_proxy = create_proxy(_on_record_stop_event)
document.addEventListener('wand-record-start', _record_start_proxy)
document.addEventListener('wand-record-stop', _record_stop_proxy)

# Delete button on each crop card (Lesson 5's Fireball crops, or Class 2
# Lesson 1's optional mystery-motion crops) is dynamically injected by
# renderCropExamples(), so — same reasoning as the record/cast buttons —
# it dispatches a CustomEvent rather than relying on py-click. event.detail
# .dataset says which crop tool it came from, since both can be loaded at
# once (just one hidden).
def _on_crop_delete_event(event):
    try:
        idx = int(event.detail.index)
    except Exception:
        return
    try:
        kind = str(event.detail.dataset)
    except Exception:
        kind = 'fireball'
    if kind == 'mystery':
        l5_delete_mystery_example(idx)
    else:
        l4_delete_example(idx)

_crop_delete_proxy = create_proxy(_on_crop_delete_event)
document.addEventListener('crop-delete-example', _crop_delete_proxy)


# ── Browser support check ──────────────────────────────────────────────
# Web Bluetooth isn't implemented in Safari or Firefox. Ported from
# ~/Documents/GitHub/NeuralNetworkEducation's network-trainer app's
# check_browser_support(), including its hasattr() feature-detection
# approach (Safari/Firefox don't define navigator.bluetooth at all, not
# just leave it undefined, so a plain attribute access raises
# AttributeError there). Unlike that app, this banner is never dismissed
# and nothing here falls back to a virtual controller — every lesson needs
# the wand's real IMU, so there's no usable fallback path to offer.
def check_browser_support():
    banner = document.getElementById('ble-warning-banner')
    if banner and not hasattr(window.navigator, 'bluetooth'):
        banner.classList.remove('hidden')
        btn = document.getElementById('btn-connect-dm')
        if btn:
            btn.disabled = True

check_browser_support()
check_ready()
_refresh_l1_overlay()
