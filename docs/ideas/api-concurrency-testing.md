# API Concurrency & Race-Condition Testing Strategy

## Problem Statement
How might we deterministically prove that `netmgmt-client-py`'s FastAPI presentation tier stays non-blocking, lease-safe, and race-free under concurrent HTTP/WebSocket traffic—across both fast CI unit runs and live multi-vendor devices—without `time.sleep` flakiness or heavy test-suite bloat?

## Recommended Direction
Adopt a **split deterministic + live-testbed concurrency architecture** backed by barrier-synchronized ASGI unit tests ([tests/unit/test_api_concurrency.py](../..//tests/unit/test_api_concurrency.py)) and targeted multi-device composite tests ([tests/composite/test_api_testbed.py](../../tests/composite/test_api_testbed.py)).

Instead of relying on sleep-based timing races, deterministic unit scenarios use `threading.Barrier` and `threading.Event` latches (`AsyncConcurrencyHarness`, `_LatchedOpService`, `_LatchedOpNetconfClient`) to freeze in-flight RPCs or transport teardowns at exact points and verify invariants such as `CapacityLimiter` isolation, per-session `op_lock` serialization, `409 SESSION_CONFLICT` delete protection, TTL sweep lease preservation, multi-consumer WebSocket `FanoutQueue` broadcasting, and real Uvicorn TCP socket shutdown on abrupt client disconnects. Complementary live testbed scenarios fan out concurrently across Nokia SRLinux, Arista cEOS, Netopeer2, and SQLite `SchemaDB`.

## Key Assumptions & Concurrency Invariants Validated
- [x] **Event-loop responsiveness under saturated RPCs:** Non-RPC async endpoints (`/healthz`, `/readyz`, `/api/v1/devices`) and WebSocket consumer reads (`anyio.to_thread.run_sync(..., abandon_on_cancel=True)` without `rpc_limiter`) are never starved when `rpc_concurrency_limit` is saturated (`test_capacity_limiter_latched_allows_non_rpc_probes`, `test_websocket_streaming_not_starved_when_rpc_limiter_saturated`).
- [x] **Session sweep thread offloading:** `GET /api/v1/sessions` and `GET /api/v1/sessions/{id}` offload `sweep_expired_sessions()` onto AnyIO worker threads so slow `<unlock>` / `__exit__` teardowns do not freeze the ASGI event loop (`test_session_list_and_get_offload_expired_cleanup_from_event_loop`).
- [x] **Per-consumer WebSocket broadcast isolation:** `FanoutQueue` + `open_consumer` / `get_consumer_events` / `close_consumer` delivers events to all concurrent WebSocket consumers with independent `DROP_OLDEST` backpressure, early consumer detach without stopping streams, and `_STOP_SENTINEL` wakeups on explicit subscription deletion.
- [x] **Logical session lease & shutdown ordering:** Concurrent operations on the same session serialize cleanly (`max_concurrent_calls == 1`), `DELETE` during an active lease returns `409 SESSION_CONFLICT`, TTL sweeps evict only idle siblings while extending active leases, and `close_all()` drains active leases before unlocking and closing transports.
- [x] **Shared SQLite `SchemaDB` concurrency:** Configuring `timeout=30.0`, `PRAGMA busy_timeout = 5000`, `PRAGMA journal_mode = WAL`, and sharing `SchemaManager.db` in `SchemaCatalogService` avoids SQLite locking failures during concurrent multi-vendor capability discovery and catalog reads.
- [x] **Real TCP HTTP/WebSocket boundary:** `uvicorn.Server` bound to an ephemeral loopback socket cleanly handles concurrent `httpx` requests, abrupt `ws.transport.abort()` drops mid-stream, and bounded `lifespan` shutdown (`test_live_uvicorn_tcp_concurrency_abrupt_ws_drop_and_clean_shutdown`).

## MVP Scope (Implemented)
- **Unit & Socket Concurrency Suite (`tests/unit/test_api_concurrency.py`):** 10 deterministic tests covering Scenarios 1–4, 6, and 7 (210 total unit tests passing in ~3.5s).
- **Live Testbed Concurrency Suite (`tests/composite/test_api_testbed.py`):** 3 multi-device concurrency tests (12 total live API tests passing against SRLinux `172.20.20.2`, cEOS `172.20.20.3`, and Netopeer2 `172.20.20.4`).
- **Targeted Hardening Fixes:** Applied in [api/routes/subscriptions.py](../../api/routes/subscriptions.py), [api/routes/sessions.py](../../api/routes/sessions.py), [application/subscriptions.py](../../application/subscriptions.py), [application/schemas.py](../../application/schemas.py), and [core/schema_db.py](../../core/schema_db.py).

## Not Doing (and Why)
- **High-volume soak / latency benchmark harness (e.g., 500+ RPS Locust/k6 loops):** Kept out of pytest CI because container-hosted lab NOS instances (`cEOS`, `SRLinux`, `Netopeer2`) throttle control-plane SSH/gRPC handshakes and would introduce environment-dependent flakiness rather than catching code races.
- **Cross-file shared test fixture refactoring:** Kept `AsyncConcurrencyHarness` local to [tests/unit/test_api_concurrency.py](../../tests/unit/test_api_concurrency.py) to avoid coupling unrelated unit test modules to shared harness internals.
- **Multi-process worker coordination (Redis/external lock backend):** Out of scope while FastAPI runs single-process with in-memory `DeviceSessionManager` and `SubscriptionService` state.