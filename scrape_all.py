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

import chamber_scraper as chamber
import pharmacy_store as store
from provinces import ALL_PLATE_CODES, PLATE_CITY, district_key
from titck_scraper import TitckSession

logger = logging.getLogger("scrape_all")

TURKEY_TZ = timezone(timedelta(hours=3))
PROVINCE_DELAY = 2.5   # iller arası nazik gecikme (saniye) — throttle'ı tetiklememek için
CHAMBER_DELAY = 2.0    # oda siteleri arası nazik gecikme

# TİTCK'e veri beslemeyen 20 il: eczacı odası sitelerinden çekilir.
# 27 (Gaziantep) ve 79 (Kilis) Eflatunweb (gaziantepeo.org.tr), kalanı OBEN.
CHAMBER_PLATES = set(chamber.CHAMBER_SOURCES) | {27, 79}


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

        # Katman 1: TİTCK "başarılı ama boş" dönerse, bugün elimizdeki dolu
        # veriyi EZME (sabahki iyi veri kalsın). Geçici 0'lara karşı koruma.
        if not res.pharmacies and store.province_count(duty_iso, plate) > 0:
            logger.warning(
                "[%d/%d] %s (%d) TİTCK boş döndü; mevcut %d kayıt KORUNDU",
                i, len(pending), city, plate, store.province_count(duty_iso, plate),
            )
            fail += 1
            time.sleep(PROVINCE_DELAY)
            continue

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info(
            "[%d/%d] %s (%d): %d eczane (%d koordinatlı) %.1fs",
            i, len(pending), city, plate, len(res.pharmacies), with_coords, res.took,
        )
        ok += 1
        time.sleep(PROVINCE_DELAY)

    return ok, fail


def scrape_chambers(duty_iso: str, plates: list[int], force: bool) -> tuple[int, int]:
    """Eczacı odası kaynaklı 20 ili çekip depoya yazar (TODAY-only).

    Koordinatsız sitelerin (Malatya, Gaziantep, Kilis) eczaneleri Nominatim
    ile geocode edilir (sonuç önbelleğe alınır).
    """
    done = set() if force else store.completed_plates(duty_iso)
    targets = [p for p in plates if p in CHAMBER_PLATES and p not in done]
    logger.info("Oda kaynakları (%s): %d il bekliyor, %d atlandı",
                duty_iso, len(targets), len(CHAMBER_PLATES & set(plates)) - len(targets))

    ok = fail = 0
    for i, plate in enumerate(targets, 1):
        city = PLATE_CITY[plate]
        try:
            if plate in (27, 79):
                res = chamber.scrape_gaziantep_eo(str(plate), want_kilis=(plate == 79))
            else:
                info = chamber.CHAMBER_SOURCES[plate]
                res = chamber.scrape_chamber(
                    info["url"], city, str(plate), multi=info.get("multi", False)
                )
        except Exception:
            logger.exception("[%d/%d] oda %s (%d) HATA", i, len(targets), city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if not res.success:
            logger.warning("[%d/%d] oda %s (%d) başarısız", i, len(targets), city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        # Katman 1: oda boş döndüyse bugünkü dolu veriyi ezme
        if not res.pharmacies and store.province_count(duty_iso, plate) > 0:
            logger.warning("[%d/%d] oda %s (%d) boş döndü; mevcut kayıt KORUNDU",
                           i, len(targets), city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        # koordinatsız eczaneleri geocode et (önbellekli)
        if any(p.get("lat") is None for p in res.pharmacies):
            chamber.geocode_pharmacies(res.pharmacies, city)
        for ph in res.pharmacies:
            ph["district_key"] = district_key(ph.get("district", ""))

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info("[%d/%d] oda %s (%d): %d eczane (%d koordinatlı) %.1fs",
                    i, len(targets), city, plate, len(res.pharmacies), with_coords, res.took)
        ok += 1
        time.sleep(CHAMBER_DELAY)

    return ok, fail


def scrape_fallbacks(duty_iso: str, plates: list[int]) -> tuple[int, int]:
    """TİTCK'ten bugün veri ALINAMAYAN illeri (count<=0) oda yedeğinden doldurur.

    Yalnız chamber.FALLBACK_SOURCES'ta kayıtlı (OBEN, parser tutan) iller için
    çalışır. Dolu veri zaten varsa o ile dokunmaz.
    """
    targets = [
        p for p in plates
        if p in chamber.FALLBACK_SOURCES and store.province_count(duty_iso, p) <= 0
    ]
    if not targets:
        return 0, 0

    logger.info("Fallback (oda yedeği): %d il deneniyor: %s",
                len(targets), ", ".join(f"{p} {PLATE_CITY[p]}" for p in targets))
    ok = fail = 0
    for plate in targets:
        city = PLATE_CITY[plate]
        info = chamber.FALLBACK_SOURCES[plate]
        try:
            res = chamber.scrape_chamber(
                info["url"], city, str(plate), multi=info.get("multi", False)
            )
        except Exception:
            logger.exception("fallback %s (%d) HATA", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if not res.success or not res.pharmacies:
            logger.warning("fallback %s (%d) veri yok", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if any(p.get("lat") is None for p in res.pharmacies):
            chamber.geocode_pharmacies(res.pharmacies, city)
        for ph in res.pharmacies:
            ph["district_key"] = district_key(ph.get("district", ""))

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info("fallback ODA %s (%d): %d eczane (%d koordinatlı) — TİTCK boştu",
                    city, plate, len(res.pharmacies), with_coords)
        ok += 1
        time.sleep(CHAMBER_DELAY)

    return ok, fail


def report_empty(duty_iso: str, plates: list[int]) -> None:
    """Tüm kaynaklar denendikten sonra hâlâ verisiz kalan illeri raporlar."""
    empty = [p for p in plates if store.province_count(duty_iso, p) <= 0]
    if empty:
        logger.warning(
            "⚠ BUGÜN VERİSİZ İLLER (%d/%d): %s",
            len(empty), len(plates),
            ", ".join(f"{p} {PLATE_CITY[p]}" for p in empty),
        )
    else:
        logger.info("Tüm iller (%d) için veri mevcut.", len(plates))


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
    total_ok = total_fail = 0

    # TİTCK illeri (oda kaynaklı 20 il hariç) — bugün + yarın
    titck_plates = [p for p in plates if p not in CHAMBER_PLATES]
    if titck_plates:
        session = TitckSession()
        for date_str in dates:
            ok, fail = scrape_for_date(session, date_str, titck_plates, args.force)
            total_ok += ok
            total_fail += fail

    # Eczacı odası illeri — sadece bugün (siteler TODAY-only)
    chamber_ok, chamber_fail = scrape_chambers(iso_date(dates[0]), plates, args.force)
    total_ok += chamber_ok
    total_fail += chamber_fail

    # Fallback: TİTCK'ten bugün veri alınamayan illeri oda yedeğinden doldur
    fb_ok, fb_fail = scrape_fallbacks(iso_date(dates[0]), plates)
    total_ok += fb_ok

    # Hâlâ verisiz kalan illeri raporla (hangi ile fallback gerektiğini görmek için)
    report_empty(iso_date(dates[0]), plates)

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
