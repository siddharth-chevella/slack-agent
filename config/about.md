OLake: Comprehensive Technical Research Report
Open-Source Database Replication to Apache Iceberg
March 2026 | Based on olake.io & github.com/datazip-inc/olake

================================================================================
1. INTRODUCTION & OVERVIEW
================================================================================

OLake is an open-source, high-performance ELT (Extract, Load, Transform) engine built by Datazip, focused exclusively on one mission: replicating data from operational databases into open lakehouse table formats — primarily Apache Iceberg and plain Parquet — as fast and reliably as possible. The project is written entirely in Go (with a specialized Java component for Iceberg I/O), and is available at github.com/datazip-inc/olake.

Unlike generic ETL platforms such as Airbyte or Fivetran that attempt to cover hundreds of sources and destinations, OLake is deliberately narrow in scope. It does not try to be a universal connector hub. Instead, it is hyper-optimised for the specific, increasingly common use case of database-to-lakehouse replication — moving OLTP data into a queryable, engine-agnostic lakehouse so that analytics, ML, and BI workloads can run without touching production systems.

The tool was born from our experience building Datazip, a broader data analytics platform, where we kept hitting the same wall: existing ingestion solutions were too slow, too complex, or too expensive for the Iceberg-native path. OLake is our answer to that problem, now shared with the open-source community.

Key Claim: OLake delivers up to 7x faster bulk-load performance than Fivetran for PostgreSQL-to-Iceberg replication, and outpaces open-source alternatives like Airbyte and Debezium by 21x to over 600x in benchmark tests (NYC Taxi dataset, 4 billion rows, Azure infrastructure).

How OLake connects to sources: OLake connects directly to the upstream database (or log source). It reads the source's change stream (PostgreSQL WAL, MySQL binlog, MongoDB oplog, etc.) as a consumer or client. OLake does not sit behind middleware or act as a replica, primary, or broker in the source's topology; it is always a direct client to the database it is reading from.

================================================================================
2. SUPPORTED SOURCES & DESTINATIONS
================================================================================

2.1 Source Connectors

PostgreSQL supports Full Refresh, Incremental, and CDC via pgoutput (WAL logical replication). It works with standard community Postgres, AWS RDS, Aurora, and Supabase. MySQL supports Full Refresh, Incremental, and CDC via Binlog (go-mysql library), including MySQL RDS and Aurora. MongoDB supports Full Refresh, Incremental, and CDC via the Oplog / Change Streams API, including sharded and replica-set clusters. Oracle supports Full Refresh and Incremental Sync via DBMS Parallel Execute, but CDC is not yet available. Apache Kafka supports consumer group-based streaming in append-only mode. MSSQL, DB2, and S3 are planned and currently in development.

2.2 Destination Connectors

Apache Iceberg is supported via multiple catalog integrations: AWS Glue, REST catalogs (Nessie, Polaris, Unity Catalog, Lakekeeper, S3 Tables), Hive Metastore, and JDBC Catalog. Plain Parquet is written directly to S3, GCS, MinIO, or local filesystem.

OLake's Iceberg output is immediately queryable by any Iceberg v2-compatible engine, including AWS Athena, Trino, Spark, Flink, Presto, Hive, Snowflake, DuckDB, and Dremio.

================================================================================
3. ARCHITECTURE: A DEEP TECHNICAL DIVE
================================================================================

OLake follows a modular, plugin-based architecture with a clean separation of concerns across five primary layers: the Core Framework, the Protocol Layer, Drivers (Sources), Writers (Destinations), and the Type System.

3.1 The Core Framework

The core framework is built with Cobra for CLI command management and exposes four primary commands: spec, check, discover, and sync.

State Management tracks sync progress in a state.json file, enabling resumable snapshots and CDC cursor tracking. Configuration Management handles source credentials, destination configs, concurrency settings, and stream selections via JSON files. Monitoring is provided by an embedded HTTP server that exposes live stats.

3.2 The Protocol Layer

The Protocol Layer defines three critical Go interfaces: Connector (base), Driver (extends Connector with Setup, Discover, Read, ChangeStreamSupported), and Writer (extends Connector with Setup, Write, Normalization, EvolveSchema, Close).

3.3 Drivers (Source Connectors)

PostgreSQL Driver: connects to WAL using pglogrepl and native pgoutput protocol. Supports CTID-based, primary key, and user-defined column chunking strategies for full refresh.

MySQL Driver: uses go-mysql library, single-reader multi-writer architecture reading binlog events. Supports auto-increment PK chunking, indexed column chunking, and partition-aware chunking.

MongoDB Driver: uses Change Streams API with SetFullDocument(UpdateLookup). Supports ObjectID-based, SplitVector, and adaptive sampling chunking strategies.

3.4 Writers (Destination Connectors)

OLake uses a hybrid Go/Java architecture for Iceberg. The Go side handles data ingestion, concurrency, schema detection, batching (10,000 records per batch), and serialization to Arrow/Parquet. The Java side runs as a gRPC server and handles Iceberg table creation, committing Parquet files, and catalog interactions.

OLake recently migrated to Apache Arrow as its in-memory data format, achieving roughly 1.75x additional speedup.

For CDC operations, OLake writes Iceberg Equality Delete Files bounded at 64 MB each.

3.5 Parallel Chunking & Concurrency Model

OLake operates on three levels of concurrency: Global (GlobalCxGroup caps total concurrent stream operations), Stream (pool of worker threads per stream), and Writer Pool (each writer thread has its own in-memory buffer channel).

3.6 State Management & Resumability

Every sync operation produces a state.json file recording the current position of each stream. For CDC streams this is the database log cursor (WAL LSN, binlog position, resume token). For full refresh, it tracks completed chunks enabling crash recovery.

3.7 Schema Evolution

OLake automatically detects and adapts to schema changes. Uses a thread-safe schema evolution mechanism with a Dead Letter Queue (DLQ) for problematic records.

================================================================================
4. PERFORMANCE BENCHMARKS
================================================================================

Full Load: 235,000 records per second for PostgreSQL. Fivetran is 7x slower. Airbyte crashed. Debezium is 15.9x slower.

CDC: 50 million PostgreSQL CDC changes in 20.1 minutes — 53% faster than Fivetran, 10x–70x faster than other open-source CDC tools. MySQL CDC is 85.9% faster than Fivetran.

================================================================================
5. DEPLOYMENT
================================================================================

Production: Kubernetes via Helm chart with OLake UI, OLake Worker (Temporal), PostgreSQL, NFS Server, Temporal UI.

Development: Single Docker Compose file launching the full stack.

Airflow: Can be orchestrated via Airflow DAGs invoking OLake CLI commands on a schedule.

================================================================================
6. KNOWN LIMITATIONS
================================================================================

OLake is an ELT tool — transformations happen downstream. Oracle CDC (LogMiner) is not yet supported. Delta Lake and Apache Hudi are planned destinations.

================================================================================
7. COMMUNITY
================================================================================

OLake is maintained by Datazip. GitHub: github.com/datazip-inc/olake. Slack community for real-time support. Documentation: olake.io/docs.

================================================================================
Sources: olake.io/docs | olake.io/blog | github.com/datazip-inc/olake
================================================================================
