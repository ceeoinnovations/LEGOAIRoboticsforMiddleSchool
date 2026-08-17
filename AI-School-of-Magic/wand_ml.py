"""wand_ml.py — gesture-recognition algorithms for the Magic Wand lessons.

Independent of hardware/UI (same role qlearn.py plays in the sibling
QLearn-* apps): everything here operates on plain Python/numpy data —
lists of raw [ax, ay, az, gx, gy, gz] IMU samples and string labels —
and never touches PyScript, the DOM, or BLE. main.py is the only file
that knows about the robot or the page.

Algorithms are implemented from scratch (no scikit-learn) so the whole
decision process stays inspectable by students, matching QTable in
qlearn.py.
"""
import numpy as np

# One raw IMU sample = accelerometer XYZ + gyroscope XYZ. Units are whatever
# the hub reports raw (not calibrated to g / deg-per-s here) — that's fine
# because every distance computed below is relative to a training set that
# was recorded on the same hub, never compared to an absolute scale.
CHANNELS = ['ax', 'ay', 'az', 'gx', 'gy', 'gz']
N_CHANNELS = len(CHANNELS)


def resample_trace(raw, n_steps=40):
    """raw: list of N_CHANNELS-length samples (any length >= 1), captured at
    whatever rate notifications arrived. Returns an (n_steps, N_CHANNELS)
    array, linearly interpolated onto a fixed number of timesteps so two
    gestures of different durations become directly comparable — a quick
    student holding the wand for 0.8s and a slow one taking 2s can still be
    recognized as "the same shape of motion."
    """
    raw = np.asarray(raw, dtype=float)
    if raw.ndim == 1:
        raw = raw.reshape(1, -1)
    if raw.shape[0] < 2:
        raw = np.vstack([raw, raw])
    src_t = np.linspace(0.0, 1.0, raw.shape[0])
    dst_t = np.linspace(0.0, 1.0, n_steps)
    out = np.empty((n_steps, N_CHANNELS))
    for c in range(N_CHANNELS):
        out[:, c] = np.interp(dst_t, src_t, raw[:, c])
    return out


def _prep(raw_trace, n_steps=40):
    """raw trace -> flattened fixed-length feature vector."""
    return resample_trace(raw_trace, n_steps).reshape(-1)


class Normalizer:
    """Per-channel/per-timestep z-score normalization, fit once on a
    training set and reused for every trace compared against it. Without
    this, the axis with the largest raw numbers (often gyroscope, during a
    fast spin) would dominate every distance calculation regardless of
    which axes actually distinguish the gestures."""

    def __init__(self):
        self.mean = None
        self.std = None

    def fit(self, X):
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std < 1e-6] = 1e-6

    def transform(self, X):
        return (X - self.mean) / self.std


class KNNClassifier:
    """k-Nearest-Neighbors over flattened, normalized, resampled gesture
    traces — "compare the shape of this motion to the shapes we've seen
    before." Works for both:
      - one-class recognition (Lessons 1-3: only 'Fireball' examples exist,
        so `predict` reports a confidence to compare against a threshold —
        "close enough to what we've seen" vs. "not recognized")
      - multi-class recognition (Lesson 5: several named spells)
    """

    def __init__(self, k=3, n_steps=40):
        self.k = k
        self.n_steps = n_steps
        self.X = None
        self.y = None
        self.normalizer = Normalizer()
        self.scale = 6.0  # distance at which confidence decays to ~0; refit in fit()

    @property
    def n_examples(self):
        return 0 if self.X is None else len(self.X)

    @property
    def labels(self):
        return [] if self.y is None else sorted(set(self.y.tolist()))

    def fit(self, traces, labels):
        X = np.array([_prep(t, self.n_steps) for t in traces])
        self.normalizer.fit(X)
        self.X = self.normalizer.transform(X)
        self.y = np.array(labels)

        # Adapt the confidence scale to this dataset's own spread, instead of
        # a hardcoded constant: the average distance between training
        # examples and their own nearest neighbor is a natural "this still
        # counts as the same gesture" yardstick.
        if len(self.X) >= 2:
            d = np.linalg.norm(self.X[:, None, :] - self.X[None, :, :], axis=2)
            np.fill_diagonal(d, np.inf)
            nearest = d.min(axis=1)
            self.scale = float(max(np.mean(nearest) * 2.5, 1e-3))
        else:
            self.scale = 6.0

    def _distances(self, raw_trace):
        x = self.normalizer.transform(_prep(raw_trace, self.n_steps)[None, :])[0]
        return np.linalg.norm(self.X - x, axis=1)

    def predict(self, raw_trace):
        """Returns (label, confidence in [0, 1], sorted nearest distances)."""
        if self.X is None or len(self.X) == 0:
            return None, 0.0, []
        d = self._distances(raw_trace)
        k = min(self.k, len(d))
        nearest_idx = np.argsort(d)[:k]
        nearest_d = d[nearest_idx]
        nearest_labels = self.y[nearest_idx]

        labels, counts = np.unique(nearest_labels, return_counts=True)
        best_label = str(labels[np.argmax(counts)])
        avg_dist = float(np.mean(nearest_d))
        confidence = float(np.clip(1.0 - avg_dist / self.scale, 0.0, 1.0))
        return best_label, confidence, [float(v) for v in np.sort(nearest_d)]


class KMeansCluster:
    """From-scratch k-means over flattened, normalized, resampled gesture
    traces — Lesson 4's "find the pattern with no labels" tool. Deliberately
    not scikit-learn, so the algorithm (pick k centers, assign, re-average,
    repeat) stays something students can have explained to them line by
    line, matching QTable's Bellman update in qlearn.py.
    """

    def __init__(self, k=3, n_iter=50, seed=0, n_steps=40):
        self.k = k
        self.n_iter = n_iter
        self.n_steps = n_steps
        self.rng = np.random.default_rng(seed)
        self.centroids = None
        self.normalizer = Normalizer()

    def fit(self, traces):
        """traces: list of raw variable-length recordings (unlabeled).
        Returns (assignments: list[int] one per trace, inertia: float)."""
        X = np.array([_prep(t, self.n_steps) for t in traces])
        self.normalizer.fit(X)
        Xn = self.normalizer.transform(X)
        n = len(Xn)
        k = max(1, min(self.k, n))

        init_idx = self.rng.choice(n, size=k, replace=False)
        centroids = Xn[init_idx].copy()

        for _ in range(self.n_iter):
            d = np.linalg.norm(Xn[:, None, :] - centroids[None, :, :], axis=2)
            assignments = np.argmin(d, axis=1)
            new_centroids = centroids.copy()
            for c in range(k):
                members = Xn[assignments == c]
                if len(members) > 0:
                    new_centroids[c] = members.mean(axis=0)
            if np.allclose(new_centroids, centroids):
                centroids = new_centroids
                break
            centroids = new_centroids

        self.centroids = centroids
        d = np.linalg.norm(Xn[:, None, :] - centroids[None, :, :], axis=2)
        assignments = np.argmin(d, axis=1)
        inertia = float(np.sum(np.min(d, axis=1) ** 2))
        return assignments.tolist(), inertia

    def predict(self, raw_trace):
        if self.centroids is None:
            return None
        x = self.normalizer.transform(_prep(raw_trace, self.n_steps)[None, :])[0]
        d = np.linalg.norm(self.centroids - x[None, :], axis=1)
        return int(np.argmin(d))


def summary_features(raw_trace):
    """Two simple, human-describable numbers for a raw trace, used ONLY for
    the 2D scatter plot in Lesson 4 (never for clustering/classification
    itself, which use the full resampled trace via KNN/KMeans above):
      - "bigness": total accelerometer range across the gesture
      - "twistiness": total gyroscope range across the gesture
    Gives students an intuitive (if simplified) picture of why the AI might
    be grouping certain motions together.
    """
    trace = resample_trace(raw_trace)
    accel = trace[:, 0:3]
    gyro = trace[:, 3:6]
    bigness = float(np.ptp(accel, axis=0).sum())
    twistiness = float(np.ptp(gyro, axis=0).sum())
    return bigness, twistiness


def accel_magnitude(raw_trace):
    """Per-sample accelerometer magnitude sqrt(ax^2+ay^2+az^2), on the RAW
    (not resampled/time-warped) trace — used only for the "compare examples
    side by side" overlay chart, where keeping real elapsed time is the
    point: it's what makes uneven leading dead-time (button held before the
    motion actually starts) visible as different-length flat runs at the
    start of each line, rather than something resampling would hide."""
    return [float((s[0] ** 2 + s[1] ** 2 + s[2] ** 2) ** 0.5) for s in raw_trace]


def detect_motion_start(raw_trace, baseline_fraction=0.2, threshold_factor=1.5):
    """Guess which sample index a recording's "real" motion starts at, for
    the Lesson 4 (Data Cleaning) crop tool's default marker position.

    Treats the first `baseline_fraction` of the recording as "holding
    still" and measures how noisy/spread-out that baseline is; returns the
    first index whose accelerometer magnitude exceeds
    baseline_mean + threshold_factor * baseline_spread. This is a deliberately
    simple heuristic (not a separate ML model) — a plain mean+spread
    threshold is something a middle schooler can have explained to them in
    one sentence, matching the rest of this module's "keep it inspectable"
    approach. Falls back to 0 (no trim) if the trace is too short or nothing
    ever clearly exceeds the baseline, so it never suggests trimming away
    the whole recording.
    """
    mag = accel_magnitude(raw_trace)
    n = len(mag)
    if n < 6:
        return 0
    baseline_n = max(3, int(n * baseline_fraction))
    baseline = mag[:baseline_n]
    mean_b = sum(baseline) / len(baseline)
    spread_b = (sum((v - mean_b) ** 2 for v in baseline) / len(baseline)) ** 0.5
    threshold = mean_b + threshold_factor * max(spread_b, 1e-6)
    for i, v in enumerate(mag):
        if v > threshold:
            return i
    return 0
