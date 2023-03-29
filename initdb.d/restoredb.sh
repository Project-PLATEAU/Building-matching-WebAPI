#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE EXTENSION postgis;
	-- CREATE EXTENSION postgis_raster;
	-- CREATE EXTENSION postgis_sfcgal;
EOSQL

for i in /docker-entrypoint-initdb.d/*.dump.gz
do
	gzip -dc $i | psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
done

