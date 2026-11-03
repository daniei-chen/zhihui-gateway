"""Process counters and bounded windows; no request bodies or database scrape scans."""
from __future__ import annotations

from collections import Counter, deque
import math
import threading
import time

BOUNDS = (.1, .5, 1, 2, 5, 10, 30, 60, 180, 600, math.inf)
PROTOCOLS = frozenset(('openai', 'responses', 'anthropic', 'http'))
TRAFFIC = frozenset(('inference', 'admin', 'probe', 'other'))


def _escape(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


class Metrics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.counts = Counter()
        self.outcomes = Counter()
        self.rules = Counter()
        self.minutes = {}
        self.models = set()
        self.histograms = {}
        self.samples = deque(maxlen=20000)

    def _prune(self, minute):
        for old in tuple(self.minutes):
            if old < minute - 1439:
                del self.minutes[old]

    def _histogram(self, name, protocol, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            return
        item = self.histograms.setdefault((name, protocol), {'count': 0, 'sum': 0.0, 'buckets': [0] * len(BOUNDS)})
        item['count'] += 1
        item['sum'] += seconds
        for i, bound in enumerate(BOUNDS):
            item['buckets'][i] += int(seconds <= bound)

    def observe(self, *, protocol, traffic, http_status, terminal, latency_ms,
                first_token_ms=None, model=None, rules=()):
        protocol = protocol if protocol in PROTOCOLS else 'http'
        traffic = traffic if traffic in TRAFFIC else 'other'
        status = http_status if type(http_status) is int else 500
        if terminal == 'client_cancelled':
            status = 499
        elif 200 <= status < 300 and terminal in ('upstream_error', 'transport_error', 'protocol_incomplete', 'application_error', 'interrupted'):
            status = 502
        group = str(status // 100) + 'xx' if 100 <= status < 600 else 'other'
        now = self.clock()
        minute = int(now // 60)
        with self.lock:
            self._prune(minute)
            bucket = self.minutes.setdefault(minute, Counter())
            for target in (self.counts, bucket):
                target['requests'] += 1
                target['errors'] += int(status >= 400)
                target['429'] += int(status == 429)
                target['503'] += int(status == 503)
                target['5xx'] += int(500 <= status < 600)
            self.outcomes[protocol, traffic, group] += 1
            if model:
                model = str(model)[:96]
                if model not in self.models and len(self.models) >= 64:
                    model = 'other'
                else:
                    self.models.add(model)
                bucket['model:' + model] += 1
            for name in rules:
                # A mutable custom configuration cannot grow metric cardinality forever.
                if name not in self.rules and len(self.rules) >= 96:
                    name = 'other'
                self.rules[str(name)[:64]] += 1
            if type(latency_ms) in (int, float) and math.isfinite(latency_ms) and latency_ms >= 0:
                self._histogram('request_duration', protocol, latency_ms / 1000)
                self.samples.append((now, latency_ms))
            if type(first_token_ms) in (int, float) and math.isfinite(first_token_ms) and first_token_ms >= 0:
                self._histogram('first_token', protocol, first_token_ms / 1000)

    def render(self):
        now = self.clock()
        minute = int(now // 60)
        with self.lock:
            self._prune(minute)
            hour, day = Counter(), Counter()
            for at, values in self.minutes.items():
                day.update(values)
                if at >= minute - 59:
                    hour.update(values)
            counts, outcomes, rules = self.counts.copy(), self.outcomes.copy(), self.rules.copy()
            histograms = {key: dict(value, buckets=list(value['buckets'])) for key, value in self.histograms.items()}
            latency = sorted(value for at, value in self.samples if at >= now - 3600)
        lines, defined = [], set()

        def emit(name, kind, help_text, value, labels=None):
            if name not in defined:
                lines.extend(('# HELP ' + name + ' ' + help_text, '# TYPE ' + name + ' ' + kind))
                defined.add(name)
            suffix = '{' + ','.join(k + '="' + _escape(v) + '"' for k, v in (labels or {}).items()) + '}' if labels else ''
            lines.append(name + suffix + ' ' + str(value))

        emit('workbuddy_requests_total', 'counter', 'Tracked requests since this process started.', counts['requests'])
        emit('workbuddy_errors_total', 'counter', 'Tracked logical failures since this process started; inspect traffic/status groups.', counts['errors'])
        emit('workbuddy_metrics_window_coverage_seconds', 'gauge', 'Window coverage since process start, capped at 24h.', min(86400, max(0, now - self.started)))
        emit('workbuddy_requests_last_hour', 'gauge', 'Observed requests in the last 60 minute buckets.', hour['requests'])
        emit('workbuddy_errors_last_hour', 'gauge', 'Observed logical failures in the last 60 minute buckets.', hour['errors'])
        for key, name in [('429', 'workbuddy_rejections_429_24h'), ('503', 'workbuddy_rejections_503_24h'), ('5xx', 'workbuddy_errors_5xx_24h')]:
            emit(name, 'gauge', 'Observed window count, 60s buckets, up to 24h of this process.', day[key])
        for (protocol, traffic, group), value in sorted(outcomes.items()):
            emit('workbuddy_request_outcomes_total', 'counter', 'Tracked outcomes by bounded protocol, traffic and logical status class.', value,
                 {'protocol': protocol, 'traffic': traffic, 'status_class': group})
        for key, value in sorted(day.items()):
            if key.startswith('model:'):
                emit('workbuddy_requests_24h_by_model', 'gauge', 'Observed model window count; at most 64 models plus other.', value, {'model': key[6:]})
        for rule, value in sorted(rules.items()):
            emit('workbuddy_compatibility_hits_total', 'counter', 'Requests matching a rule; no prompt content is recorded.', value, {'rule': rule})
        for (stem, protocol), item in sorted(histograms.items()):
            name = 'workbuddy_' + stem + '_seconds'
            if name not in defined:
                lines.extend(('# HELP ' + name + ' Observed timing since this process started.', '# TYPE ' + name + ' histogram'))
            defined.add(name)
            for bound, value in zip(BOUNDS, item['buckets']):
                edge = '+Inf' if math.isinf(bound) else str(bound)
                lines.append(name + '_bucket{protocol="' + protocol + '",le="' + edge + '"} ' + str(value))
            lines.append(name + '_sum{protocol="' + protocol + '"} ' + str(item['sum']))
            lines.append(name + '_count{protocol="' + protocol + '"} ' + str(item['count']))
        if latency:
            for p in (50, 95, 99):
                emit('workbuddy_latency_ms_p' + str(p) + '_1h', 'gauge', 'Percentile of at most 20000 observed samples, up to 1h.', latency[min(len(latency)-1, int(len(latency) * p / 100))])
            emit('workbuddy_latency_samples_1h', 'gauge', 'Retained observed samples in the percentile window.', len(latency))
        return lines


METRICS = Metrics()
