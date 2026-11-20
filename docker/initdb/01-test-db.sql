-- Separate database for the test suite, so tests never touch real data.
CREATE DATABASE prodata_test OWNER prodata;
\connect prodata_test
CREATE EXTENSION IF NOT EXISTS postgis;
\connect prodata
CREATE EXTENSION IF NOT EXISTS postgis;
