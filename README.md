# AncillaryNews
The goal is to build a newsagent that can explain fluctuations in the danish ancillary markets

**Scope "EnergySignals Agent"**

1. Formål & Målsætning
At skabe en autonom agens, der monitorerer det danske elmarked, korrelerer systemdata (Energinet) med eksterne markedsanalyser (nyheder/teorier), og leverer handlingsorienterede indsigter i tilfælde af markedsudsving.

2. Funktionelle Moduler (Docker-microservices)
A. Ingestion Engine (The "Hard Data" Layer)
KPI: 100% oppetid på polling af Energinet API.

Logik: Asynkrone GET-requests mod Energinet/ENTSO-E.

Storage: TimescaleDB til tidsserier.

Funktion: Skal implementere Exponential Backoff og Circuit Breaking (f.eks. ved brug af tenacity-biblioteket).

B. Insight Crawler (The "Soft Data" Layer)
KPI: Konvertering af ustrukturerede kilder til struktureret Markdown.

Logik: En dedikeret scraper-container (Playwright) der trigger på faste intervaller eller via RSS.

Storage: Qdrant (Vektor-database) til semantisk søgning.

Funktion: Skal kunne "summarize" artikler og udtrække "Markeds-teser" (f.eks. "Analytiker X mener, at vindmangel vil presse prisen i aften").

C. Intelligence Orchestrator (The Reasoning Layer)
Logik: En RAG-pipeline (LangChain/LlamaIndex).

Beslutningsmatrix:

Tjekker for anomaly i data (Systemydelser/Pris-spikes).

Henter relevant kontekst fra vektor-DB'en.

LLM-syntese: "Givet data fra Energinet og teorierne i Vektor-DB, hvorfor ser vi dette udsving?"

Alerting: Output til JSON (til videre integration i Slack/Dashboard).

3. Teknisk Infrastruktur (Claude Code Implementation Plan)
For at få Claude Code til at bygge dette, skal du give den følgende ordrer:

Project Init: "Setup en Python-baseret monorepo struktur med poetry til dependency management. Opsæt Docker-compose fil med TimescaleDB, Qdrant og tre separate services (ingestor, crawler, orchestrator)."

Schema Design: "Definer et SQL-schema til TimescaleDB, der kan lagre historiske priser og ubalancer med høj performance."

Resilience Layer: "Implementer en base-service klasse, der bruger tenacity til at håndtere netværksfejl ved kald til Energinet API'et."

RAG Implementation: "Skab en service, der kan tage en tekst-input (scrape) og transformere den til embeddings i Qdrant, så de kan korreleres med tidsstempler."

4. Strategiske succeskriterier
Latency: Agenten skal kunne give en "Early Warning" inden for 15 minutter efter at et udsving er registreret i data.

Kontekst: Agenten skal altid returnere kilden for sin analyse (f.eks. "Baseret på prisspikes fra Energinet og en analyse fra EnergiWatch vedrørende flaskehalse...").

Vedligeholdelse: Hele systemet skal kunne genstartes fra bunden via docker-compose up --build.

**Brainstorming**
