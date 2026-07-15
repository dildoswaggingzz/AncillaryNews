ancillary-news/
├── docker-compose.yml
├── pyproject.toml
├── init-db/
│   └── 01-init.sql
├── shared/
│   ├── __init__.py
│   ├── base_ingestor.py
│   └── db_manager.py
└── services/
    └── ingestor/
        ├── Dockerfile
        └── main.py