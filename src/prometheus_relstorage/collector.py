from typing import Generator, TypedDict

from prometheus_client import CollectorRegistry, REGISTRY
from prometheus_client.core import GaugeMetricFamily
from relstorage.storage import RelStorage
from relstorage.cache.local_client import LocalClient


class _CacheStats(TypedDict):
    hits: int
    misses: int
    sets: int
    ratio: float
    bytes: int
    len: int


class RelStorageCollector:
    """Prometheus collector for RelStorage per-process cache metrics.

    Collects local object cache statistics (hits, misses, hit ratio, byte
    usage). These are in-process values that differ per API container.

    For database-level metrics (DB size), use a separate collector registered
    on your DB-metrics endpoint.

    The *storage* argument must be a ``relstorage.storage.RelStorage``
    instance. Obtaining it from a ``ZODB.DB``::

        storage = db.storage

    Usage::

        from prometheus_relstorage import RelStorageCollector

        # Register on the default global registry
        RelStorageCollector(storage)

        # Use a custom registry
        RelStorageCollector(storage, registry=my_registry)

        # Skip auto-registration
        collector = RelStorageCollector(storage, registry=None)
        my_registry.register(collector)
    """

    def __init__(
            self,
            storage: RelStorage,
            registry: CollectorRegistry | None = REGISTRY):
        self._storage = storage
        if registry is not None:
            registry.register(self)

    def collect(self) -> Generator:
        # _cache is a private attribute; guard in case RelStorage internals
        # change
        cache: LocalClient | None = getattr(self._storage, "_cache", None)
        if cache is None:
            return

        stats: _CacheStats = cache.stats()

        cache_hits = GaugeMetricFamily(
            "relstorage_cache_hits",
            "RelStorage local cache hits since last reset",
        )
        cache_misses = GaugeMetricFamily(
            "relstorage_cache_misses",
            "RelStorage local cache misses since last reset",
        )
        cache_sets = GaugeMetricFamily(
            "relstorage_cache_sets",
            "RelStorage local cache sets since last reset",
        )
        cache_hit_ratio = GaugeMetricFamily(
            "relstorage_cache_hit_ratio",
            "RelStorage local cache hit ratio (0–1)",
        )
        cache_bytes = GaugeMetricFamily(
            "relstorage_cache_bytes",
            "Bytes currently used by the RelStorage local cache",
        )
        cache_bytes_limit = GaugeMetricFamily(
            "relstorage_cache_bytes_limit",
            "Configured byte limit of the RelStorage local cache",
        )
        cache_oid_count = GaugeMetricFamily(
            "relstorage_cache_oid_count",
            "Number of distinct object IDs currently stored in the RelStorage local cache",
        )

        cache_hits.add_metric([], float(stats["hits"]))
        cache_misses.add_metric([], float(stats["misses"]))
        cache_sets.add_metric([], float(stats["sets"]))
        cache_hit_ratio.add_metric([], float(stats["ratio"]))
        cache_bytes.add_metric([], float(stats["bytes"]))
        cache_bytes_limit.add_metric([], float(cache.limit))
        cache_oid_count.add_metric([], float(stats["len"]))

        cache_object_max_bytes = GaugeMetricFamily(
            "relstorage_cache_object_max_bytes",
            "Maximum bytes for a single object to be stored in the RelStorage local cache",
        )
        options = getattr(cache, "options", None)
        if options is not None:
            cache_object_max_bytes.add_metric(
                [], float(options.cache_local_object_max)
            )
            yield cache_object_max_bytes

        yield cache_hits
        yield cache_misses
        yield cache_sets
        yield cache_hit_ratio
        yield cache_bytes
        yield cache_bytes_limit
        yield cache_oid_count

        # relstorage_mvcc_* metrics are only available when the cache
        # object is a StorageCache (has a polling_state attribute).
        polling_state = getattr(cache, "polling_state", None)
        if polling_state is not None:
            ps_stats = polling_state.stats()
            viewers = ps_stats.get("registered_viewers")
            if viewers is not None:
                active_viewers = GaugeMetricFamily(
                    "relstorage_mvcc_active_viewers",
                    "Number of active ZODB connections sharing this RelStorage in this process",
                )
                active_viewers.add_metric([], float(viewers))
                yield active_viewers

            mvcc_max_depth = GaugeMetricFamily(
                "relstorage_mvcc_max_index_depth",
                "Maximum transaction lag (depth) allowed before a ZODB connection is detached",
            )
            mvcc_max_size = GaugeMetricFamily(
                "relstorage_mvcc_max_index_size",
                "Maximum total MVCC object index entries allowed before old viewers are cut off",
            )
            mvcc_max_depth.add_metric(
                [], float(polling_state.max_allowed_index_depth)
            )
            mvcc_max_size.add_metric(
                [], float(polling_state.max_allowed_index_size)
            )
            yield mvcc_max_depth
            yield mvcc_max_size


class RelStorageDBStatsCollector:
    """Prometheus collector for RelStorage database-level metrics.

    Collects metrics that query the database directly (DB size). These return
    the same value from any process, so register this on a dedicated
    DB-metrics endpoint rather than the default per-process ``/metrics``.

    ``get_object_count()`` (``SELECT COUNT(*) FROM object_state``) is
    intentionally excluded — it is O(n) in PostgreSQL due to MVCC and
    prohibitively slow on large databases.

    Usage::

        from prometheus_relstorage import RelStorageDBStatsCollector

        RelStorageDBStatsCollector(storage, registry=my_db_registry)
    """

    def __init__(
            self,
            storage: RelStorage,
            registry: CollectorRegistry | None = REGISTRY):
        self._storage = storage
        if registry is not None:
            registry.register(self)

    def collect(self) -> Generator:
        db_size_bytes = GaugeMetricFamily(
            "relstorage_db_size_bytes",
            "Physical size of the RelStorage database in bytes (pg_database_size)",
        )
        try:
            db_size_bytes.add_metric(
                [], float(
                    self._storage.adapter.stats.get_db_size()))
            yield db_size_bytes
        except Exception:  # noqa: BLE001 — DB may be unavailable at scrape time
            pass
