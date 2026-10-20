import threading
import time
import unittest

from support import T0, StoreTestCase, entry, make_config, make_source
from newsradar.scheduler import INTERNAL_ERROR_RETRY, Poller, due_sources, host_of
from newsradar.store import INTERNAL_ERROR, Store


def rows(*pairs):
    return {sid: dict(next_due=due) for sid, due in pairs}


class DueSourcesTests(unittest.TestCase):
    A = make_source(id="a", url="https://a.test/1", priority=10)
    B = make_source(id="b", url="https://b.test/1", priority=0)
    A2 = make_source(id="a2", url="https://a.test/2", priority=0)

    def due(self, sources, stored, now=T0, busy=(), ready=None, startup=T0 - 1000, step=10):
        return [d[1]["id"] for d in due_sources(sources, stored, now, busy_hosts=set(busy), host_ready_at=ready or {},
                                                startup_due=startup, priority_step=step)]

    def test_only_due_sources(self):
        self.assertEqual(self.due([self.A, self.B], rows(("a", T0), ("b", T0 + 1))), ["a"])

    def test_busy_and_resting_hosts_are_skipped(self):
        stored = rows(("a", T0 - 5), ("b", T0 - 5))
        self.assertEqual(self.due([self.A, self.B], stored, busy=["a.test"]), ["b"])
        self.assertEqual(self.due([self.A, self.B], stored, ready={"b.test": T0 + 1}), ["a"])
        self.assertEqual(self.due([self.A, self.B], stored, ready={"b.test": T0}), ["b", "a"])

    def test_priority_is_a_bounded_head_start(self):
        # a (priority 10) is 50 s older than b (priority 0); with 10 s/level b goes first...
        self.assertEqual(self.due([self.A, self.B], rows(("a", T0 - 150), ("b", T0 - 100))), ["b", "a"])
        # ...but once a has waited more than the 100 s head start, a goes first.
        self.assertEqual(self.due([self.A, self.B], rows(("a", T0 - 250), ("b", T0 - 100))), ["a", "b"])

    def test_priority_is_clamped(self):
        low = make_source(id="low", url="https://c.test/", priority=99)
        neg = make_source(id="neg", url="https://d.test/", priority=-50)
        # Clamped to 12 and 0: keys are T0-10 (low), T0-5 (neg), T0-2 (b). Unclamped, low would sort last
        # (99 levels of delay) and neg first (a negative head start).
        order = self.due([low, neg, self.B], rows(("low", T0 - 130), ("neg", T0 - 5), ("b", T0 - 2)))
        self.assertEqual(order, ["low", "neg", "b"])

    def test_ties_on_head_start_go_to_the_longest_waiting(self):
        low = make_source(id="low", url="https://c.test/", priority=12)
        high = make_source(id="high", url="https://d.test/", priority=0)
        self.assertEqual(self.due([high, low], rows(("low", T0 - 121), ("high", T0 - 1))), ["low", "high"])

    def test_never_polled_sources_count_as_due_at_startup(self):
        order = self.due([self.A, self.B], rows(("a", 0), ("b", T0 - 2000)), startup=T0 - 1000, step=0)
        self.assertEqual(order, ["b", "a"])

    def test_ties_keep_configuration_order_and_unknown_rows_are_ignored(self):
        same = make_source(id="c", url="https://c.test/", priority=0)
        self.assertEqual(self.due([self.B, same], rows(("b", T0), ("c", T0))), ["b", "c"])
        self.assertEqual(self.due([self.B, same], rows(("b", T0))), ["b"])

    def test_host_of(self):
        self.assertEqual(host_of(self.A), "a.test")
        self.assertEqual(host_of(make_source(url="https://A.test:8443/x")), "A.test:8443")


class Recorder:
    """A fake fetch: records timing per call and returns a canned result."""

    def __init__(self, delay=0.0, result=None, raise_for=(), results=None):
        self.delay, self.result, self.raise_for = delay, result or {}, set(raise_for)
        self.results = results or {}
        self.calls, self.lock = [], threading.Lock()
        self.active, self.max_active, self.active_hosts, self.host_overlap = 0, 0, {}, False

    def __call__(self, source, previous):
        host = host_of(source)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.active_hosts[host] = self.active_hosts.get(host, 0) + 1
            self.host_overlap |= self.active_hosts[host] > 1
        started = time.time()
        try:
            if source["id"] in self.raise_for:
                raise RuntimeError("boom")
            time.sleep(self.delay)
            result = dict(started=started, finished=time.time(), status=200, headers={},
                          entries=[entry(1, host=host)], fetch_ms=1)
            result.update(self.results.get(source["id"], self.result))
            return result
        finally:
            with self.lock:
                self.active -= 1
                self.active_hosts[host] -= 1
                self.calls.append((source["id"], started, time.time()))


class PollerTests(StoreTestCase):
    def run_poller(self, sources, fetch, seconds, **config):
        cfg = make_config(*sources, **config)
        store = Store(self.tmp / ("p%d.sqlite" % len(self.id())), cfg)
        stop, beats = threading.Event(), []
        poller = Poller(cfg, store, fetch, stop, beats.append)
        thread = threading.Thread(target=poller.run)
        thread.start()
        time.sleep(seconds)
        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.addCleanup(store.db.close)
        return store, beats

    def test_worker_cap_and_one_request_per_host(self):
        sources = [make_source(id="s%d" % i, url="https://h%d.test/%d" % (i % 3, i), interval=0.05) for i in range(9)]
        fetch = Recorder(delay=0.15)
        self.run_poller(sources, fetch, 1.2, workers=2, host_gap_seconds=0)
        self.assertLessEqual(fetch.max_active, 2)
        self.assertEqual(fetch.max_active, 2)
        self.assertFalse(fetch.host_overlap)

    def test_host_gap_between_requests(self):
        sources = [make_source(id="x", url="https://same.test/1", interval=0.01),
                   make_source(id="y", url="https://same.test/2", interval=0.01)]
        fetch = Recorder()
        self.run_poller(sources, fetch, 1.6, workers=4, host_gap_seconds=0.6)
        starts = sorted(start for _, start, _ in fetch.calls)
        self.assertGreaterEqual(len(starts), 2)
        self.assertTrue(all(b - a >= 0.55 for a, b in zip(starts, starts[1:])), starts)

    def test_rate_limited_host_pauses_all_its_sources(self):
        sources = [make_source(id="x", url="https://same.test/1", interval=0.01),
                   make_source(id="y", url="https://same.test/2", interval=0.01),
                   make_source(id="z", url="https://other.test/1", interval=0.01)]
        limited = dict(status=429, entries=[], error="HTTP 429", headers={})
        fetch = Recorder(results={"x": limited, "y": limited})
        self.run_poller(sources, fetch, 1.2, workers=4, host_gap_seconds=0)
        same_host = [sid for sid, _, _ in fetch.calls if sid in ("x", "y")]
        self.assertEqual(len(same_host), 1)  # paused for at least 60 s after the 429
        self.assertGreater(sum(sid == "z" for sid, _, _ in fetch.calls), 1)

    def test_exceptions_mark_the_source_and_the_loop_survives(self):
        sources = [make_source(id="bad", url="https://bad.test/", interval=0.01),
                   make_source(id="good", url="https://good.test/", interval=0.01)]
        fetch = Recorder(raise_for=["bad"])
        before = time.time()
        with self.assertLogs("radar", "ERROR") as logs:
            store, beats = self.run_poller(sources, fetch, 1.0, workers=2, host_gap_seconds=0)
        bad = store.source("bad")
        self.assertEqual((bad["status"], bad["error"]), ("error", INTERNAL_ERROR))
        self.assertGreaterEqual(bad["next_due"], before + INTERNAL_ERROR_RETRY - 1)
        self.assertGreater(sum(sid == "good" for sid, _, _ in fetch.calls), 1)
        self.assertTrue(any("poll processing failed: bad" in line for line in logs.output))
        self.assertGreater(len(beats), 1)

    def test_results_are_stored(self):
        fetch = Recorder()
        store, _ = self.run_poller([make_source(id="a", interval=60)], fetch, 0.8, workers=1)
        self.assertEqual(len(fetch.calls), 1)  # not due again for 60 s
        self.assertEqual(store.source("a")["status"], "ok")
        self.assertEqual(store.db.execute("SELECT count(*) FROM checks").fetchone()[0], 1)

    def test_stop_is_prompt(self):
        cfg = make_config(make_source())
        store = Store(self.tmp / "stop.sqlite", cfg)
        self.addCleanup(store.db.close)
        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        Poller(cfg, store, Recorder(), stop, lambda now: None).run()
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
