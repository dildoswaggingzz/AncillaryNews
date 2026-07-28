ancillary-news/
├── docker-compose.yml
├── pyproject.toml
├── CLAUDE.md
├── README.md
├── DEPLOYMENT.md
├── .pre-commit-config.yaml
├── init-db/                         # numbered SQL schema, applied in order on DB init
│   ├── 01-init.sql
│   ├── 02-event-reports.sql
│   ├── 03-triggers.sql
│   ├── 04-bess-simulations.sql
│   ├── 05-morning-briefs.sql
│   ├── 06-bess-afrr-activation.sql
│   ├── 07-bess-capacity-currency.sql
│   ├── 08-market-events.sql
│   └── 09-bess-coverage-counts.sql
├── shared/                          # library imported by every service (not a service)
│   ├── __init__.py
│   ├── base_ingestor.py             # data/infra
│   ├── db_manager.py
│   ├── datasets.py                  # dataset registry
│   ├── backfill.py
│   ├── baselines.py
│   ├── units.py                     # currency (DKK/EUR) handling — never sum currencies
│   ├── logging_config.py
│   ├── metrics.py
│   ├── dataset_validation.py
│   ├── forecast_model.py            # forecasting (LightGBM)
│   ├── feature_store.py
│   ├── bess_simulator.py            # BESS stack: run_backtest, BessConfig, BacktestResult
│   ├── bess_dispatch_milp.py        #   perfect/forecast co-optimizer LP (revenue only)
│   ├── bess_estimator.py            #   Morning Brief configs (zone capacity stacks)
│   ├── economic_eval.py             #   forecast-vs-trailing allocation layer
│   ├── bess_economics.py            #   retailer cost/P&L layer (revenue -> net profit)
│   ├── rule_engine.py               # reasoning / triggers / synthesis
│   ├── event_extractor.py
│   ├── event_synthesizer.py
│   ├── claim_extractor.py
│   ├── morning_brief_editor.py
│   ├── price_recap_synthesizer.py
│   ├── forecast_synthesizer.py
│   ├── article_extractor.py         # soft data (news/RSS)
│   ├── rss_feeds.py
│   ├── rss_reader.py
│   ├── vector_store.py
│   ├── llm_json.py                  # LLM + notifications
│   ├── email_notifier.py
│   ├── slack_notifier.py
│   └── linkedin_embed.py
├── services/                        # long-running services, each with a Dockerfile
│   ├── ingestor/                    # hard market data -> TimescaleDB
│   ├── crawler/                     # soft data (RSS/news) -> Qdrant
│   ├── orchestrator/                # APScheduler jobs (rule engine, Morning Brief)
│   └── api/                         # FastAPI dashboard + endpoints
├── scripts/                         # one-off reports, migrations, backfills
├── tests/                           # pytest suite (offline by default; -m live for network)
├── docs/                            # *-design.md and *-results.md per subsystem
├── grafana/                         # monitoring
└── prometheus/
