Below are the repositories that make up OLake:

=== olake (Core Engine) ===
Language: Go
The central runtime that does all actual data movement. Contains:
- Source connectors/drivers: PostgreSQL (pgoutput CDC), MySQL (binlog CDC), MongoDB (oplog CDC),
  Oracle, MSSQL, DB2, Kafka, S3
- Destination writers: Apache Iceberg (REST/Glue/Hive catalogs), Parquet files
- Core sync logic: full load, incremental, and CDC (change data capture) modes
- Schema discovery, schema evolution handling, type mapping
- CLI entrypoint for running sync jobs (used directly or invoked by olake-ui's BFF)
- Internal libraries/interfaces shared across connectors (state management, record batching,
  checkpointing, parallelism)
- CI/CD pipelines, integration tests, Docker image builds

=== olake-ui (Frontend + BFF) ===
Languages: TypeScript (React frontend), Go (BFF/API backend)
The user-facing control plane for OLake. Contains:
- React frontend: dashboard for managing sources, destinations, jobs, sync runs, logs
- BFF (Backend for Frontend): REST API that stores job/source/destination configurations in
  PostgreSQL, triggers and monitors sync jobs via Temporal workflow engine
- Temporal worker code that invokes olake (core engine) Docker image to actually run syncs
- Docker Compose setup to run the full stack (UI + BFF + Temporal + PostgreSQL)
- Auth layer (user login, session management)
Connection to others: Directly orchestrates olake (core) by spawning its Docker container per
sync job. olake-helm deploys this service to Kubernetes. olake-docs documents how to set it up.

=== olake-docs (Documentation & Website) ===
Language: MDX/JavaScript (Docusaurus)
The public-facing website at olake.io. Contains:
- Full user documentation: quickstart guides, connector-specific setup (Postgres, MySQL, MongoDB,
  Iceberg, S3, etc.), configuration references, CLI flag references
- Architecture explanations, CDC concepts, benchmarks, performance comparisons
- Blog posts and announcements
- Changelog and migration guides
- No runtime code; purely static content
Connection to others: Documents all features of olake (core) and olake-ui. References olake-helm
for Kubernetes deployment instructions.

=== olake-helm (Kubernetes Deployment) ===
Language: YAML (Helm)
Kubernetes deployment manifests for the entire OLake stack. Contains:
- Helm chart(s) for deploying olake-ui (frontend + BFF), Temporal, PostgreSQL, and the sync
  worker infrastructure on a Kubernetes cluster
- values.yaml with configurable resource limits, replica counts, image tags, secrets
- Templates for Deployments, Services, Ingress, ConfigMaps, PersistentVolumeClaims
Connection to others: Packages and deploys olake-ui and the olake core worker image.

=== olake-fusion (Lakehouse Management Layer) ===
Language: Java
A lakehouse management system built on open table formats (Iceberg, etc.). Contains:
- Table optimization services: compaction, expiry, orphan file cleanup for Iceberg tables
- Catalog management: unified interface over multiple Iceberg catalogs (REST, Hive, Glue, etc.)
- Iceberg table health metrics, partitioning strategies, self-optimizing table features
Connection to others: Sits downstream of olake (core) — after olake writes data into Iceberg
tables, olake-fusion manages and optimizes those tables.

=== Inter-repo Relationships Summary ===
olake (core) ← invoked by → olake-ui (BFF orchestrates core's Docker image per sync job)
olake (core) → writes Iceberg tables → olake-fusion (manages/optimizes those tables)
olake + olake-ui → packaged for K8s by → olake-helm
olake + olake-ui + olake-helm → documented in → olake-docs
