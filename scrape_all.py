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
    """e-Devlet formatında (dd/mm/YYYY) aktif nöbet günü + ertesi gün.

    Nöbet TR saatiyle 08:30'da değişir. 08:30'dan ÖNCE (gece 00:00–08:30)
    aktif nöbet hâlâ DÜNden gelir; bu yüzden taban gün dün olur. Böylece
    gece çekimleri (00/01/02) hâlâ aktif olan nöbet gününü tazeler.
    """
    now = datetime.now(TURKEY_TZ)
    boundary = now.replace(hour=8, minute=30, second=0, microsecond=0)
    base = now if now >= boundary else now - timedelta(days=1)
    return [base.strftime("%d/%m/%Y"), (base + timedelta(days=1)).strftime("%d/%m/%Y")]


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

        # TİTCK istenen tarihi listede bulamazsa SESSİZCE başka günü (genelde
        # bugünü) döndürür; bunu yanlış güne yazmayalım — yoksa 08:30 sonrası
        # kullanıcıya yanlış günün listesi gösterilir (max doğruluk).
        if res.duty_date and iso_date(res.duty_date) != duty_iso:
            logger.warning(
                "[%d/%d] %s (%d) TİTCK %s yerine %s döndü; YAZILMADI (yanlış gün)",
                i, len(pending), city, plate, duty_iso, res.duty_date,
            )
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


def scrape_fallbacks(duty_iso: str, plates: list[int], force: bool = False) -> tuple[int, int]:
    """ODA-ÖNCELİK: FALLBACK_SOURCES illerini (bugün) DOĞRUDAN odadan çeker.

    Eczacı odası siteleri nöbet değişimini anında yansıttığı için bu iller önce
    odadan alınır; oda boş/erişilemezse TİTCK ikinci sırada (sonraki geçişte)
    doldurur. Taze-tamamlanmış iller atlanır (freshness). Oda boş dönerse
    mevcut dolu veriyi EZMEZ (kayıt yapılmaz).
    """
    done = set() if force else store.completed_plates(duty_iso)
    targets = [p for p in plates if p in chamber.FALLBACK_SOURCES and p not in done]
    if not targets:
        return 0, 0

    logger.info("Oda-öncelik: %d il odadan çekiliyor: %s",
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
            logger.exception("oda %s (%d) HATA", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if not res.success or not res.pharmacies:
            logger.warning("oda %s (%d) veri yok -> TİTCK devralacak", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if any(p.get("lat") is None for p in res.pharmacies):
            chamber.geocode_pharmacies(res.pharmacies, city)
        for ph in res.pharmacies:
            ph["district_key"] = district_key(ph.get("district", ""))

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info("ODA %s (%d): %d eczane (%d koordinatlı) %.1fs",
                    city, plate, len(res.pharmacies), with_coords, res.took)
        ok += 1
        time.sleep(CHAMBER_DELAY)

    return ok, fail


def scrape_api_sources(duty_iso: str, plates: list[int], force: bool = False) -> tuple[int, int]:
    """ODA-ÖNCELİK: JSON/API tabanlı özel oda kaynakları (Ankara, Giresun...).

    OBEN olmayan, kendi veri ucu olan odalar. chamber.scrape_oda_api tipe göre
    doğru parser'a yönlendirir. Boş dönerse mevcut veriyi ezmez; taze-tamamlanmış
    iller atlanır.
    """
    done = set() if force else store.completed_plates(duty_iso)
    targets = [p for p in plates if p in chamber.ODA_API_SOURCES and p not in done]
    if not targets:
        return 0, 0

    logger.info("Oda-öncelik (API): %d il çekiliyor: %s",
                len(targets), ", ".join(f"{p} {PLATE_CITY[p]}" for p in targets))
    ok = fail = 0
    for plate in targets:
        city = PLATE_CITY[plate]
        info = chamber.ODA_API_SOURCES[plate]
        try:
            res = chamber.scrape_oda_api(plate, info, duty_iso)
        except Exception:
            logger.exception("oda-api %s (%d) HATA", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if not res.success or not res.pharmacies:
            logger.warning("oda-api %s (%d) veri yok -> TİTCK devralacak", city, plate)
            fail += 1
            time.sleep(CHAMBER_DELAY)
            continue

        if any(p.get("lat") is None for p in res.pharmacies):
            chamber.geocode_pharmacies(res.pharmacies, city)
        for ph in res.pharmacies:
            ph["district_key"] = district_key(ph.get("district", ""))

        with_coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        store.save_province(duty_iso, plate, city, res.pharmacies)
        logger.info("ODA-API %s (%d): %d eczane (%d koordinatlı) %.1fs",
                    city, plate, len(res.pharmacies), with_coords, res.took)
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

    duty_today = iso_date(dates[0])

    # 1) ODA-ÖNCELİK (bugün): eczacı odası siteleri değişimi ANINDA yansıtır.
    #    Oda kaynaklı tüm iller önce odadan çekilir (TİTCK'siz 20 il + fallback 26).
    chamber_ok, chamber_fail = scrape_chambers(duty_today, plates, args.force)
    total_ok += chamber_ok
    total_fail += chamber_fail

    fb_ok, fb_fail = scrape_fallbacks(duty_today, plates, args.force)
    total_ok += fb_ok
    total_fail += fb_fail

    api_ok, api_fail = scrape_api_sources(duty_today, plates, args.force)
    total_ok += api_ok
    total_fail += api_fail

    # 2) TİTCK İKİNCİ SIRADA: oda siteleri TODAY-only olduğu için yarını TİTCK
    #    verir; bugün ise yalnız oda'nın DOLDURAMADIĞI illeri çeker.
    titck_plates = [p for p in plates if p not in CHAMBER_PLATES]
    if titck_plates:
        session = TitckSession()
        oda_plates = CHAMBER_PLATES | set(chamber.FALLBACK_SOURCES) | set(chamber.ODA_API_SOURCES)
        # BUGÜN: oda'nın bugün veri sağladığı illeri TİTCK EZMESİN (oda-öncelik,
        # --force olsa bile). Oda kaynağı olmayan iller normal akışta çekilir.
        # Oda verisi YALNIZCA taze ise (count>0 ve son FRESH_MINUTES içinde) "dolu"
        # sayılır. Oda sitesi bayatlamış/erişilemez olduğunda eski kayıt korunsa
        # bile TİTCK devralabilsin diye tazelik kontrolü şart (max doğruluk).
        oda_fresh = set(store.completed_plates(duty_today))
        oda_filled = {p for p in oda_plates if p in oda_fresh}
        today_titck = [p for p in titck_plates if p not in oda_filled]
        ok, fail = scrape_for_date(session, dates[0], today_titck, args.force)
        total_ok += ok
        total_fail += fail
        # YARIN (ve sonrası): tüm TİTCK illeri (oda yarını vermez)
        for date_str in dates[1:]:
            ok, fail = scrape_for_date(session, date_str, titck_plates, args.force)
            total_ok += ok
            total_fail += fail

    # Hâlâ verisiz kalan illeri raporla
    report_empty(duty_today, plates)

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
