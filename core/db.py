from __future__ import annotations

import os

import duckdb

from core.config import settings


def get_connection() -> duckdb.DuckDBPyConnection:
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    return duckdb.connect(settings.db_path)


def initialize_db() -> None:
    con = get_connection()

    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS trades_id_seq START 1
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id         BIGINT  DEFAULT nextval('trades_id_seq') PRIMARY KEY,
            symbol     VARCHAR NOT NULL,
            ts         BIGINT  NOT NULL,
            price      DOUBLE  NOT NULL,
            qty        DOUBLE  NOT NULL,
            side       VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT now()
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            symbol        VARCHAR NOT NULL,
            ts            BIGINT  NOT NULL,
            last_price    DOUBLE,
            funding_rate  DOUBLE,
            open_interest DOUBLE,
            volume_24h    DOUBLE,
            created_at    TIMESTAMP DEFAULT now()
        )
    """)

    # Sesiones de trading — se usará en Fase 7 (journal)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id         BIGINT PRIMARY KEY,
            start_time TIMESTAMP DEFAULT now(),
            end_time   TIMESTAMP,
            notes      TEXT
        )
    """)

    con.close()
