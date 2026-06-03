"""Tüm 81 ili e-Devlet'ten nazikçe scrape edip SQLite depoya yazan toplu iş.

systemd timer ile günde birkaç kez çalıştırılır. e-Devlet rate-limit
uyguladığı için iller SIRALI işlenir, aralarda gecikme ve retry vardır.
Bugün ve yarının nöbet günleri toplanır (08:30 geçişi için).

Kullanım:
    python scrape_all.py            # bugün + yarın, eksik illeri tamamla
    python scrape_all.py --force    # tümünü yeniden çek (scrape_log'u yok say)
    python scrape_all.py --plates 46,34   # sadece belirli plakalar
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import pharmacy_store as store
from provinces import ALL_PLATE_CODES, PLATE_CITY, district_key
from titck_scraper import TitckSession

logger = logging.getLogger("scrape_all")

TURKEY_TZ = timezone(timedelta(hours=3))
PROVINCE_DELAY = 1.5   # iller arası nazik gecikme (saniye)


def active_duty_dates() -> list[str]:
    """e-Devlet formatında (dd/mm/YYYY) bugün ve yarın."""
    now = datetime.now(TURKEY_TZ)
    return [now.strftime("%d/%m/%Y"), (now + timedelta(days=1)).strftime("%d/%m/%Y")]


def iso_date(ddmmyyyy: str) -> str:
    """'03/06/2026' → '2026-06-03' (depo anahtarı)."""
    return datetime.strptime(ddmmyyyy, "%d/%m/%Y").strftime("%Y-%m-%d")


def scrape_for_date(
    session: TitckSession,
    date_ddmmyyyy: str,
    plates: list[int],
    force: bool,
) -> tuple[int, int]:
    duty_iso = iso_date(date_ddmmyyyy)
    done = set() if force else store.completed_plates(duty_iso)
    pending = [p for p in plates if p not in done]

    logger.info(
        "Nöbet günü %s (%s): %d il bekliyor, %d atlandı",
        date_ddmmyyyy, duty_iso, len(pending), len(plates) - len(pending),
    )

    ok = fail = 0
    for i, plate in enumerate(pending, 1):
        city = PLATE_CITY[plate]
        try:
            res = session.scrape_province(str(plate), prefer_date=date_ddmmyyyy)
        except Exception:
            logger.exception("[%d/%d] %s (%d) HATA", i, len(pending), city, plate)
            fail += 1
            time.sleep(PROVINCE_DELAY)
            continue

        if not res.success:
            logger.warning("[%d/%d] %s (%d) başarısız", i, len(pending), city, plate)
            fail += 1
            time.sleep(PROVINCE_DELAY)
            continue

        for ph in res.pharmacies:
            ph["district_key"] = district_key(ph.get("district", ""))

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info(
            "[%d/%d] %s (%d): %d eczane (%d koordinatlı) %.1fs",
            i, len(pending), city, plate, len(res.pharmacies), with_coords, res.took,
        )
        ok += 1
        time.sleep(PROVINCE_DELAY)

    return ok, fail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="tümünü yeniden çek")
    parser.add_argument("--plates", type=str, default="", help="virgüllü plaka listesi")
    parser.add_argument("--today-only", action="store_true", help="sadece bugün")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )

    store.init_db()

    if args.plates:
        plates = [int(x) for x in args.plates.split(",") if x.strip().isdigit()]
    else:
        plates = ALL_PLATE_CODES

    dates = active_duty_dates()
    if args.today_only:
        dates = dates[:1]

    started = time.time()
    session = TitckSession()
    total_ok = total_fail = 0
    for date_str in dates:
        ok, fail = scrape_for_date(session, date_str, plates, args.force)
        total_ok += ok
        total_fail += fail

    # Eski nöbet günlerini temizle (aktif günleri tut)
    keep = [iso_date(d) for d in dates]
    pruned = store.prune_old(keep)

    logger.info(
        "BİTTİ: %d başarılı, %d başarısız, %d eski satır silindi, toplam %.0fs",
        total_ok, total_fail, pruned, time.time() - started,
    )
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
