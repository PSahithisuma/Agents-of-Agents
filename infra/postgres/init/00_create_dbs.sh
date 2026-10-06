#!/bin/bash
# Creates all required databases for VibeForge services
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE keycloak;
    CREATE DATABASE langfuse;
    CREATE DATABASE gitea;
    GRANT ALL PRIVILEGES ON DATABASE keycloak TO vibeforge;
    GRANT ALL PRIVILEGES ON DATABASE langfuse TO vibeforge;
    GRANT ALL PRIVILEGES ON DATABASE gitea TO vibeforge;
EOSQL

echo "All databases created successfully"
