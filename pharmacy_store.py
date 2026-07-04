"""Nöbetçi eczane verisi için SQLite depo.

Arka plandaki scrape işi buraya yazar; web/bot buradan okur. Böylece
kullanıcı trafiği e-Devlet'e dokunmaz (rate-limit güvenliği).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Bir il "tamamlandı" (yeniden çekilmez) sayılır ancak son başarılı çekimi
# bu süreden yeniyse. Daha eskiyse tekrar çekilir -> gün içi/gece tazeleme
# (bazı illerde gece nöbetçisi değişimi yakalanır). Dakika.
FRESH_MINUTES = int(os.environ.get("NOBETCIM_FRESH_MIN", "120") or "120")

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
    source     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (duty_date, plate_code)
);

-- Adres→koordinat önbelleği (koordinatsız oda siteleri için Nominatim
-- sonuçları; her scrape turunda yeniden geocode etmemek için kalıcı).
CREATE TABLE IF NOT EXISTS geocode_cache (
    query_key TEXT PRIMARY KEY,
    lat       REAL,
    lng       REAL
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
        # Mevcut DB migrasyonu: scrape_log.source kolonu yoksa ekle (kaynak
        # bazlı tazelik için; oda kendi doldurduğunu atlar, TİTCK'i devralır).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scrape_log)")}
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE scrape_log ADD COLUMN source TEXT NOT NULL DEFAULT ''"
            )
            logger.info("scrape_log.source kolonu eklendi (migrasyon)")
    logger.info("SQLite depo hazır: %s", DB_PATH)


# Dejenere / nöbet-geçişi koruması
# --------------------------------
# Oda siteleri 08:30 nöbet geçişinde (ve bazen akşam güncellemesinde) kısa süre
# YARIM/BOZUK liste döndürebiliyor (ör. 17 eczanelik il aniden 1 eczane). Tam o
# anda çekim yapılırsa sağlam liste bu bozuk listeyle EZİLİR; bir sonraki taramaya
# kadar (akşam taramaları kapalıysa gece boyu) kullanıcı eksik liste görür.
# Koruma: aynı nöbet günü için elde SAĞLAM bir liste (>= GUARD_MIN_EXISTING) varken
# gelen yeni liste anormal küçükse (<=2 ya da mevcudun 1/3'ünden az) YAZMA, mevcudu
# koru. Küçük illerde (mevcut < GUARD_MIN_EXISTING) koruma devreye girmez → normal
# güncellenir. (2 Tem 2026 Kahramanmaraş: 22:00 taraması odayı geçişte yakalayıp
# 17→1 yazmış, gece boyu tek eczane görünmüştü.)
GUARD_MIN_EXISTING = 4

# Operasyonel kaçış kapısı: meşru bir gün-içi küçülme (ör. odanın 6→2
# düzeltmesi) guard'a takılırsa `scrape_all.py --plates X --force --no-guard`
# ile bu bayrak kapatılıp yeniden çekilir. Varsayılan HEP açık.
GUARD_ENABLED = True


def _looks_degenerate(new_count: int, existing_count: int) -> bool:
    """Yeni listenin, mevcut sağlam listeye göre 'geçiş bozuğu' olup olmadığı.

    Aynı (duty_date, plaka) için mevcut liste sağlamsa (>= GUARD_MIN_EXISTING) ve
    yeni liste ya <=2 ya da mevcudun 1/3'ünden azsa dejenere sayılır. Farklı
    nöbet günü ilk kez yazılırken mevcut=0 olduğundan koruma devreye girmez.
    """
    if existing_count < GUARD_MIN_EXISTING:
        return False
    return new_count <= 2 or new_count * 3 < existing_count


def save_province(
    duty_date: str,
    plate_code: int | str,
    city: str,
    pharmacies: list[dict],
    guard: bool = True,
    source: str = "",
) -> None:
    """Bir il için (duty_date) eczaneleri kaydeder; eskisini değiştirir.

    pharmacies öğeleri: district, district_key, name, phone, address, lat, lng
    """
    plate = int(plate_code)
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    # Mükerrer temizleme: bazı kaynaklar (özellikle TİTCK) aynı eczaneyi birden
    # çok döndürebiliyor (ör. Yozgat'ta "SAĞLIK Boğazlıyan" 9 kez aynı koordinat).
    # Ad+ilçe+koordinat aynıysa tek kayıt tut (2 Tem 2026).
    seen = set()
    rows = []
    for p in pharmacies:
        name = p.get("name")
        if not name:
            continue
        key = (name, p.get("district", ""), p.get("lat"), p.get("lng"))
        if key in seen:
            continue
        seen.add(key)
        rows.append((
            duty_date, plate, city,
            p.get("district", ""), p.get("district_key", ""),
            name, p.get("phone"), p.get("address", ""),
            p.get("lat"), p.get("lng"),
        ))

    with _write_lock, _connect() as conn:
        # Dejenere/geçiş koruması: sağlam liste varken bozuk küçük listeyle ezme.
        prev = conn.execute(
            "SELECT count FROM scrape_log WHERE duty_date = ? AND plate_code = ?",
            (duty_date, plate),
        ).fetchone()
        existing = int(prev["count"]) if prev else 0
        if guard and GUARD_ENABLED and _looks_degenerate(len(rows), existing):
            logger.warning(
                "save_province: %s (plaka %s, %s) dejenere/geçiş bozuğu sayıldı "
                "(%d → %d); mevcut liste KORUNDU, üzerine yazılmadı "
                "(meşru küçülmeyse: --plates %s --force --no-guard)",
                city, plate, duty_date, existing, len(rows), plate,
            )
            return
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
            "INSERT INTO scrape_log (duty_date, plate_code, count, scraped_at, source) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(duty_date, plate_code) DO UPDATE SET "
            "count=excluded.count, scraped_at=excluded.scraped_at, source=excluded.source",
            (duty_date, plate, len(rows), now_iso, source),
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


def completed_plates(
    duty_date: str,
    fresh_minutes: int = FRESH_MINUTES,
    source: str | None = None,
) -> set[int]:
    """Verilen nöbet günü için 'tamamlandı' (bu turda atlanacak) plaka kodları.

    Bir il atlanır ancak: count>0 (veri var) VE son başarılı çekimi
    fresh_minutes'ten yeni. Daha eski çekimler tekrar denenir -> gün içi/gece
    tazeleme (gece nöbetçisi değişimini yakalamak için).

    source verilirse YALNIZCA o kaynaktan (ör. 'oda') taze doldurulmuş iller
    döner. Oda taraması `source='oda'` ile çağırır → böylece TİTCK'in "yarın"
    diye doldurup 08:30'da aktifleşen günü ATLAMAZ, geçişte hemen devralır
    (oda = Google koordinatı). Oda kendi doldurduğunu ise atlar (nezaket).

    count=0 olan iller 'tamamlandı' sayılmaz: e-Devlet hızlı isteklerde bazen
    boş forma döndürüyor (geçici 0); böylece sonraki tur tekrar dener.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=fresh_minutes)).isoformat()
    sql = ("SELECT plate_code FROM scrape_log "
           "WHERE duty_date = ? AND count > 0 AND scraped_at >= ?")
    params: list = [duty_date, cutoff]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    with _connect() as conn:
        cur = conn.execute(sql, params)
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


def geocode_get(query_key: str) -> tuple[float, float] | None:
    """Önbellekten adres→koordinat döner (yoksa None)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT lat, lng FROM geocode_cache WHERE query_key = ?", (query_key,)
        )
        row = cur.fetchone()
        if row and row["lat"] is not None:
            return float(row["lat"]), float(row["lng"])
        return None


def geocode_put(query_key: str, lat: float | None, lng: float | None) -> None:
    """Adres→koordinat önbelleğe yazar (başarısız geocode'lar None olarak da
    kaydedilir; böylece her turda boşuna tekrar denenmez)."""
    with _write_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO geocode_cache (query_key, lat, lng) VALUES (?,?,?) "
            "ON CONFLICT(query_key) DO UPDATE SET lat=excluded.lat, lng=excluded.lng",
            (query_key, lat, lng),
        )


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
