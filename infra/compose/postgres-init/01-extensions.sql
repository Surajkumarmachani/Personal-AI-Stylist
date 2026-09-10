-- Runs once, on first cluster init, as the superuser.
--
-- The `vector` extension is created here rather than in a migration so the
-- Phase 3 migration that adds the embedding column is a schema change only,
-- not also an infrastructure change requiring superuser. Harmless in Phase 1;
-- nothing references it yet.
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_stat_statements: you cannot tune what you cannot see, and the first time
-- you need it is always during an incident.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
