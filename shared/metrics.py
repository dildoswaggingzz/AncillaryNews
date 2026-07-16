"""
Shared Prometheus exposition helper (Phase 6 production readiness, README
§7: "Prometheus + Grafana (poller health, trigger rates, LLM latency/cost)").

Each service defines its own metric objects (`Counter`/`Histogram`) at
module import time -- registered once, in the default `prometheus_client`
registry, right next to the code they instrument (see
`services/ingestor/main.py`, `services/crawler/main.py`,
`services/orchestrator/main.py`, `shared/event_synthesizer.py`,
`shared/claim_extractor.py`, `shared/rule_engine.py`).

`services/api/main.py` is a FastAPI app already serving HTTP, so it exposes
`/metrics` directly via `prometheus_client.generate_latest()`. The other
three services are long-running APScheduler loops with no existing HTTP
server; `start_metrics_server` below gives each one a minimal, independently
scrapeable exposition endpoint via `prometheus_client`'s own
`start_http_server` (a background thread, stdlib `http.server` under the
hood -- no extra dependency beyond `prometheus_client` itself).

Called only from each service's `main()` entrypoint (i.e. under
`if __name__ == "__main__":`), never at module import time -- importing a
service's `main` module (as the test suite does) must never bind a real
socket.
"""

import logging

from prometheus_client import start_http_server

logger = logging.getLogger(__name__)


def start_metrics_server(port: int) -> None:
    """Starts a background HTTP server exposing Prometheus text format on `port`."""
    start_http_server(port)
    logger.info("Metrics server listening on port %d", port, extra={"metrics_port": port})
