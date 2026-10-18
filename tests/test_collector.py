"""Tests for prometheus_relstorage collectors.

RelStorageCollector
-------------------
Uses a real ``LocalClient`` (RelStorage's in-process LRU cache).  ``LocalClient``
requires no database connection — it only needs an ``Options`` object with a few
numeric attributes.  We attach the real client to a minimal mock storage so that
the collector finds it via ``storage._cache``.

RelStorageDBStatsCollector
--------------------------
The only interesting behaviour is forwarding ``storage.adapter.stats.get_db_size()``
to a Prometheus gauge and swallowing exceptions.  A ``unittest.mock.Mock`` is the
right tool here: the collector is a thin wrapper, and the DB call is unavoidable.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily

from relstorage.cache.local_client import LocalClient
from relstorage.options import Options

from prometheus_relstorage import RelStorageCollector, RelStorageDBStatsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_options(**kwargs) -> Options:
    """Return an Options instance with selected attributes overridden."""
    opts = Options()
    for k, v in kwargs.items():
        setattr(opts, k, v)
    return opts


def _make_cache(cache_local_mb: float = 10.0) -> LocalClient:
    """Return a real LocalClient with a small byte limit for fast tests."""
    return LocalClient(_make_options(cache_local_mb=cache_local_mb))


def _make_storage(cache: LocalClient | None) -> object:
    """Return a minimal mock storage with ``_cache`` set."""
    storage = SimpleNamespace(_cache=cache)
    return storage


def _metrics(collector) -> dict:
    """Collect and return a dict of metric_name → list[Sample]."""
    result = {}
    for m in collector.collect():
        result[m.name] = list(m.samples)
    return result


def _value(metrics: dict, name: str) -> float:
    samples = metrics[name]
    assert len(samples) == 1, f"Expected 1 sample for {name}, got {
        len(samples)}"
    return samples[0].value


# OID / TID helpers — RelStorage uses plain integers for both.
_OID = 1
_TID = 1


def _store(cache: LocalClient, oid: int = _OID, tid: int = _TID,
           state: bytes = b"data") -> None:
    """Store one object in the cache."""
    cache[oid, tid] = (state, tid)


def _hit(cache: LocalClient, oid: int = _OID, tid: int = _TID):
    """Access a stored object, producing a cache hit."""
    return cache.get((oid, tid))


def _miss(cache: LocalClient, oid: int = _OID + 999, tid: int = _TID):
    """Access an absent object, producing a cache miss."""
    return cache.get((oid, tid))


# ---------------------------------------------------------------------------
# RelStorageCollector — metric presence and naming
# ---------------------------------------------------------------------------

class TestRelStorageCollectorMetricNames(unittest.TestCase):

    def setUp(self):
        self.cache = _make_cache()
        self.storage = _make_storage(self.cache)
        self.registry = CollectorRegistry()
        self.collector = RelStorageCollector(
            self.storage, registry=self.registry)

    def test_all_expected_metric_names_present(self):
        metrics = _metrics(self.collector)
        for name in (
            "relstorage_cache_hits",
            "relstorage_cache_misses",
            "relstorage_cache_sets",
            "relstorage_cache_hit_ratio",
            "relstorage_cache_bytes",
            "relstorage_cache_bytes_limit",
            "relstorage_cache_oid_count",
        ):
            self.assertIn(
                name,
                metrics,
                f"{name} missing from collected metrics")

    def test_all_metrics_are_gauge_type(self):
        for m in self.collector.collect():
            self.assertIsInstance(
                m, GaugeMetricFamily, f"{
                    m.name} should be GaugeMetricFamily, got {
                    type(m).__name__}", )

    def test_all_metrics_have_no_labels(self):
        metrics = _metrics(self.collector)
        for name, samples in metrics.items():
            for s in samples:
                self.assertEqual(
                    s.labels, {},
                    f"{name} sample has unexpected labels: {s.labels}",
                )

    def test_each_metric_has_exactly_one_sample(self):
        metrics = _metrics(self.collector)
        for name, samples in metrics.items():
            self.assertEqual(
                len(samples), 1,
                f"{name} has {len(samples)} samples, expected 1",
            )


# ---------------------------------------------------------------------------
# RelStorageCollector — cache activity
# ---------------------------------------------------------------------------

class TestRelStorageCollectorCacheActivity(unittest.TestCase):

    def setUp(self):
        self.cache = _make_cache()
        self.storage = _make_storage(self.cache)
        self.registry = CollectorRegistry()
        self.collector = RelStorageCollector(
            self.storage, registry=self.registry)

    def test_hits_zero_initially(self):
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_hits"), 0.0)

    def test_misses_zero_initially(self):
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_misses"), 0.0)

    def test_sets_zero_initially(self):
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_sets"), 0.0)

    def test_sets_incremented_after_store(self):
        _store(self.cache)
        metrics = _metrics(self.collector)
        self.assertGreater(_value(metrics, "relstorage_cache_sets"), 0.0)

    def test_hits_incremented_after_cache_hit(self):
        _store(self.cache)
        _hit(self.cache)
        metrics = _metrics(self.collector)
        self.assertGreater(_value(metrics, "relstorage_cache_hits"), 0.0)

    def test_misses_incremented_after_cache_miss(self):
        _miss(self.cache)
        metrics = _metrics(self.collector)
        self.assertGreater(_value(metrics, "relstorage_cache_misses"), 0.0)

    def test_hit_ratio_between_zero_and_one(self):
        _store(self.cache)
        _hit(self.cache)
        _miss(self.cache)
        metrics = _metrics(self.collector)
        ratio = _value(metrics, "relstorage_cache_hit_ratio")
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)

    def test_hit_ratio_is_one_when_all_hits(self):
        _store(self.cache)
        _hit(self.cache)
        _hit(self.cache)
        metrics = _metrics(self.collector)
        self.assertAlmostEqual(
            _value(metrics, "relstorage_cache_hit_ratio"), 1.0
        )

    def test_hit_ratio_is_zero_when_all_misses(self):
        _miss(self.cache)
        _miss(self.cache)
        metrics = _metrics(self.collector)
        self.assertAlmostEqual(
            _value(metrics, "relstorage_cache_hit_ratio"), 0.0
        )

    def test_hit_ratio_zero_when_no_accesses(self):
        # ratio = hits/(hits+misses) when total == 0 → 0
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_hit_ratio"), 0.0)

    def test_cache_bytes_non_negative(self):
        metrics = _metrics(self.collector)
        self.assertGreaterEqual(_value(metrics, "relstorage_cache_bytes"), 0.0)

    def test_cache_bytes_increases_after_store(self):
        before = _value(_metrics(self.collector), "relstorage_cache_bytes")
        _store(self.cache, state=b"x" * 512)
        after = _value(_metrics(self.collector), "relstorage_cache_bytes")
        self.assertGreaterEqual(after, before)

    def test_oid_count_zero_initially(self):
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_oid_count"), 0.0)

    def test_oid_count_increases_after_store(self):
        _store(self.cache, oid=1)
        _store(self.cache, oid=2, tid=2)
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_oid_count"), 2.0)

    def test_oid_count_is_oid_not_entry_count(self):
        # Two stores for the same OID at different TIDs still count as one OID.
        _store(self.cache, oid=1, tid=1)
        _store(self.cache, oid=1, tid=2)
        metrics = _metrics(self.collector)
        self.assertEqual(_value(metrics, "relstorage_cache_oid_count"), 1.0)


# ---------------------------------------------------------------------------
# RelStorageCollector — MVCC active viewers
# ---------------------------------------------------------------------------

class TestRelStorageCollectorMVCC(unittest.TestCase):
    """relstorage_mvcc_active_viewers is only emitted when the cache object
    exposes a ``polling_state`` attribute (i.e. when storage._cache is a
    real StorageCache, not a bare LocalClient)."""

    def _make_storage_with_viewers(self, count: int) -> object:
        """Fake a StorageCache-like object with a polling_state."""
        cache = SimpleNamespace(
            stats=lambda: {"hits": 0, "misses": 0, "sets": 0,
                           "ratio": 0.0, "len": 0, "bytes": 0},
            limit=10_000_000,
            options=_make_options(),
            polling_state=SimpleNamespace(
                stats=lambda: {"registered_viewers": count},
                max_allowed_index_depth=1000,
                max_allowed_index_size=200_000,
            ),
        )
        return SimpleNamespace(_cache=cache)

    def test_active_viewers_present_when_polling_state_available(self):
        storage = self._make_storage_with_viewers(3)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertIn("relstorage_mvcc_active_viewers", metrics)

    def test_active_viewers_value(self):
        storage = self._make_storage_with_viewers(5)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertEqual(
            _value(
                metrics,
                "relstorage_mvcc_active_viewers"),
            5.0)

    def test_active_viewers_absent_without_polling_state(self):
        # When _cache is a bare LocalClient (no polling_state), the metric
        # is silently skipped rather than raising.
        cache = _make_cache()
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertNotIn("relstorage_mvcc_active_viewers", metrics)


# ---------------------------------------------------------------------------
# RelStorageCollector — configuration metrics
# ---------------------------------------------------------------------------

class TestRelStorageCollectorConfig(unittest.TestCase):
    """Per-object size limit and MVCC coordinator limits."""

    def test_cache_object_max_bytes_present(self):
        cache = _make_cache()
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertIn("relstorage_cache_object_max_bytes", metrics)

    def test_cache_object_max_bytes_matches_options(self):
        object_max = 8192
        cache = LocalClient(_make_options(cache_local_object_max=object_max))
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertEqual(
            _value(metrics, "relstorage_cache_object_max_bytes"),
            float(object_max),
        )

    def _make_storage_with_polling_state(self, max_depth: int = 1000,
                                         max_size: int = 200_000) -> object:
        cache = SimpleNamespace(
            stats=lambda: {"hits": 0, "misses": 0, "sets": 0,
                           "ratio": 0.0, "len": 0, "bytes": 0},
            limit=10_000_000,
            options=_make_options(),
            polling_state=SimpleNamespace(
                stats=lambda: {"registered_viewers": 0},
                max_allowed_index_depth=max_depth,
                max_allowed_index_size=max_size,
            ),
        )
        return SimpleNamespace(_cache=cache)

    def test_mvcc_max_index_depth_present(self):
        storage = self._make_storage_with_polling_state()
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertIn("relstorage_mvcc_max_index_depth", metrics)

    def test_mvcc_max_index_depth_value(self):
        storage = self._make_storage_with_polling_state(max_depth=500)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertEqual(
            _value(
                metrics,
                "relstorage_mvcc_max_index_depth"),
            500.0)

    def test_mvcc_max_index_size_present(self):
        storage = self._make_storage_with_polling_state()
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertIn("relstorage_mvcc_max_index_size", metrics)

    def test_mvcc_max_index_size_value(self):
        storage = self._make_storage_with_polling_state(max_size=50_000)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertEqual(
            _value(
                metrics,
                "relstorage_mvcc_max_index_size"),
            50_000.0)

    def test_mvcc_config_absent_without_polling_state(self):
        # When there is no polling_state, MVCC config metrics are silently
        # skipped.
        cache = _make_cache()
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertNotIn("relstorage_mvcc_max_index_depth", metrics)
        self.assertNotIn("relstorage_mvcc_max_index_size", metrics)


# ---------------------------------------------------------------------------
# RelStorageCollector — byte limit
# ---------------------------------------------------------------------------

class TestRelStorageCollectorByteLimit(unittest.TestCase):

    def test_bytes_limit_matches_options(self):
        mb = 5.0
        cache = _make_cache(cache_local_mb=mb)
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        metrics = _metrics(collector)
        expected = int(1_000_000 * mb)
        self.assertEqual(
            _value(
                metrics,
                "relstorage_cache_bytes_limit"),
            float(expected))

    def test_bytes_limit_reflects_custom_mb(self):
        for mb in (1.0, 10.0, 100.0):
            with self.subTest(mb=mb):
                cache = _make_cache(cache_local_mb=mb)
                storage = _make_storage(cache)
                registry = CollectorRegistry()
                collector = RelStorageCollector(storage, registry=registry)
                metrics = _metrics(collector)
                expected = int(1_000_000 * mb)
                self.assertEqual(
                    _value(metrics, "relstorage_cache_bytes_limit"),
                    float(expected),
                )


# ---------------------------------------------------------------------------
# RelStorageCollector — edge cases
# ---------------------------------------------------------------------------

class TestRelStorageCollectorEdgeCases(unittest.TestCase):

    def test_no_metrics_when_cache_is_none(self):
        storage = _make_storage(cache=None)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        self.assertEqual(_metrics(collector), {})

    def test_no_metrics_when_cache_attribute_absent(self):
        storage = SimpleNamespace()  # no _cache at all
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=registry)
        self.assertEqual(_metrics(collector), {})

    def test_registry_none_does_not_register(self):
        cache = _make_cache()
        storage = _make_storage(cache)
        registry = CollectorRegistry()
        collector = RelStorageCollector(storage, registry=None)
        # Manually collect — should work fine
        self.assertIsInstance(_metrics(collector), dict)
        # But it should NOT appear in the separate registry
        self.assertEqual(list(registry.collect()), [])


# ---------------------------------------------------------------------------
# RelStorageDBStatsCollector — DB-level metrics via mock
# ---------------------------------------------------------------------------

class TestRelStorageDBStatsCollector(unittest.TestCase):

    def _make_mock_storage(self, db_size: int = 42_000_000):
        storage = Mock()
        storage.adapter.stats.get_db_size.return_value = db_size
        return storage

    def test_emits_db_size_bytes_metric(self):
        storage = self._make_mock_storage(db_size=99_000_000)
        registry = CollectorRegistry()
        collector = RelStorageDBStatsCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertIn("relstorage_db_size_bytes", metrics)

    def test_db_size_bytes_has_correct_value(self):
        storage = self._make_mock_storage(db_size=12_345_678)
        registry = CollectorRegistry()
        collector = RelStorageDBStatsCollector(storage, registry=registry)
        metrics = _metrics(collector)
        self.assertEqual(
            _value(
                metrics,
                "relstorage_db_size_bytes"),
            12_345_678.0)

    def test_db_size_bytes_is_gauge(self):
        storage = self._make_mock_storage()
        registry = CollectorRegistry()
        collector = RelStorageDBStatsCollector(storage, registry=registry)
        for m in collector.collect():
            self.assertIsInstance(m, GaugeMetricFamily)

    def test_no_metric_emitted_when_get_db_size_raises(self):
        storage = Mock()
        storage.adapter.stats.get_db_size.side_effect = Exception(
            "DB unavailable")
        registry = CollectorRegistry()
        collector = RelStorageDBStatsCollector(storage, registry=registry)
        # Should not raise; metric is silently skipped.
        metrics = _metrics(collector)
        self.assertNotIn("relstorage_db_size_bytes", metrics)

    def test_registry_none_does_not_register(self):
        storage = self._make_mock_storage()
        registry = CollectorRegistry()
        collector = RelStorageDBStatsCollector(storage, registry=None)
        # Collecting directly still works
        metrics = _metrics(collector)
        self.assertIn("relstorage_db_size_bytes", metrics)
        # But the separate registry is empty
        self.assertEqual(list(registry.collect()), [])

    def test_get_db_size_called_exactly_once_per_collect(self):
        storage = self._make_mock_storage()
        collector = RelStorageDBStatsCollector(storage, registry=None)
        _metrics(collector)
        storage.adapter.stats.get_db_size.assert_called_once()

    def test_db_size_bytes_has_no_labels(self):
        storage = self._make_mock_storage()
        collector = RelStorageDBStatsCollector(storage, registry=None)
        metrics = _metrics(collector)
        for s in metrics["relstorage_db_size_bytes"]:
            self.assertEqual(s.labels, {})


if __name__ == "__main__":
    unittest.main()
