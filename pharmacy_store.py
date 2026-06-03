"""Nöbetçi eczane verisi için SQLite depo.

Arka plandaki scrape işi buraya yazar; web/bot buradan okur. Böylece
kullanıcı trafiği e-Devlet'e dokunmaz (rate-limit güvenliği).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# DB yolu: env > /opt/nobetcim (üretim) > proje dizini (lokal)
_DEFAULT_DB = Path(__file__).parent / "pharmacies.db"
DB_PATH = os.environ.get("NOBETCIM_DB", "").strip() or str(_DEFAULT_DB)

_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pharmacies (
    duty_date    TEXT    NOT NULL,
    plate_code   INTEGER NOT NULL,
    city         TEXT    NOT NULL,
    district     TEXT    DEFAULT '',
    district_key TEXT    DEFAULT '',
    name         TEXT    NOT NULL,
    phone        TEXT,
    address      TEXT    DEFAULT '',
    lat          REAL,
    lng          REAL
);
CREATE INDEX IF NOT EXISTS idx_pharm_date_plate
    ON pharmacies (duty_date, plate_code);

CREATE TABLE IF NOT EXISTS scrape_log (
    duty_date  TEXT    NOT NULL,
    plate_code INTEGER NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    scraped_at TEXT    NOT NULL,
    PRIMARY KEY (duty_date, plate_code)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    logger.info("SQLite depo hazır: %s", DB_PATH)


def save_province(
    duty_date: str,
    plate_code: int | str,
    city: str,
    pharmacies: list[dict],
) -> None:
    """Bir il için (duty_date) eczaneleri kaydeder; eskisini değiştirir.

    pharmacies öğeleri: district, district_key, name, phone, address, lat, lng
    """
    plate = int(plate_code)
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            duty_date, plate, city,
            p.get("district", ""), p.get("district_key", ""),
            p.get("name", ""), p.get("phone"), p.get("address", ""),
            p.get("lat"), p.get("lng"),
        )
        for p in pharmacies
        if p.get("name")
    ]

    with _write_lock, _connect() as conn:
        conn.execute(
            "DELETE FROM pharmacies WHERE duty_date = ? AND plate_code = ?",
            (duty_date, plate),
        )
        if rows:
            conn.executemany(
                "INSERT INTO pharmacies "
                "(duty_date, plate_code, city, district, district_key, name, phone, address, lat, lng) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        conn.execute(
            "INSERT INTO scrape_log (duty_date, plate_code, count, scraped_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(duty_date, plate_code) DO UPDATE SET "
            "count=excluded.count, scraped_at=excluded.scraped_at",
            (duty_date, plate, len(rows), now_iso),
        )


def get_province(duty_date: str, plate_code: int | str) -> list[dict]:
    """Bir il + nöbet günü için eczane satırlarını döner."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT city, district, district_key, name, phone, address, lat, lng "
            "FROM pharmacies WHERE duty_date = ? AND plate_code = ? "
            "ORDER BY district, name",
            (duty_date, int(plate_code)),
        )
        return [dict(r) for r in cur.fetchall()]


def completed_plates(duty_date: str) -> set[int]:
    """Verilen nöbet günü için scrape'i tamamlanmış plaka kodları."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT plate_code FROM scrape_log WHERE duty_date = ?",
            (duty_date,),
        )
        return {int(r["plate_code"]) for r in cur.fetchall()}


def province_count(duty_date: str, plate_code: int | str) -> int:
    """Depoda kayıtlı eczane sayısı (yoksa -1 = hiç scrape edilmemiş)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT count FROM scrape_log WHERE duty_date = ? AND plate_code = ?",
            (duty_date, int(plate_code)),
        )
        row = cur.fetchone()
        return int(row["count"]) if row else -1


def prune_old(keep_dates: list[str]) -> int:
    """keep_dates dışındaki tüm nöbet günlerini siler. Silinen satır sayısını döner."""
    if not keep_dates:
        return 0
    placeholders = ",".join("?" * len(keep_dates))
    with _write_lock, _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM pharmacies WHERE duty_date NOT IN ({placeholders})",
            keep_dates,
        )
        conn.execute(
            f"DELETE FROM scrape_log WHERE duty_date NOT IN ({placeholders})",
            keep_dates,
        )
        return cur.rowcount


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("DB_PATH:", DB_PATH)
