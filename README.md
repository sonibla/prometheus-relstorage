# prometheus-relstorage

Prometheus collector for [RelStorage](https://relstorage.readthedocs.io/) metrics.

Exposes local object cache statistics (hits, misses, hit ratio, byte usage) and
storage-level statistics (object count, database size) for RelStorage backends
(PostgreSQL, MySQL, SQLite, Oracle).

## Metrics exposed

| Metric | Type | Description |
|---|---|---|
| `relstorage_cache_hits` | Gauge | Local cache hits since last reset |
| `relstorage_cache_misses` | Gauge | Local cache misses since last reset |
| `relstorage_cache_sets` | Gauge | Local cache sets since last reset |
| `relstorage_cache_hit_ratio` | Gauge | Local cache hit ratio (0–1) |
| `relstorage_cache_bytes` | Gauge | Bytes currently used by the local cache |
| `relstorage_cache_bytes_limit` | Gauge | Configured byte limit of the local cache |
| `relstorage_db_object_count` | Gauge | Approximate number of objects in the database |
| `relstorage_db_size_bytes` | Gauge | Approximate physical size of the database in bytes |

## Usage

```python
from relstorage.storage import RelStorage
from prometheus_relstorage import RelStorageCollector
from prometheus_client import REGISTRY

storage = RelStorage(adapter)

# Register on the default global registry
RelStorageCollector(storage)

# Or use a custom registry
from prometheus_client import CollectorRegistry
registry = CollectorRegistry()
RelStorageCollector(storage, registry=registry)

# Or manage registration manually
collector = RelStorageCollector(storage, registry=None)
registry.register(collector)
```

## Notes

- `relstorage_db_object_count` and `relstorage_db_size_bytes` require a live
  database connection and execute a lightweight query on each scrape. They are
  relatively cheap but not free, consider scraping at a lower frequency if needed.
- Cache stats are in-process counters and are extremely cheap to read.
