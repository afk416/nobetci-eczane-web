"""TİTCK'li 61 il için eczacı odası nöbetçi sayfalarını otomatik keşfeder.

Her il için aday domain/yol kombinasyonlarını dener, mevcut OBEN parser'ımız
(chamber_scraper.scrape_chamber) ile veri çıkıp çıkmadığına bakar. Veri çıkan
ilk URL'yi "bulundu" olarak kaydeder. Hiçbir aday tutmayan iller "bulunamadı"
listesine düşer (elle bakılacaklar).

Çalıştırma:
    python discover_chambers.py            # tüm 61 il
    python discover_chambers.py 34,6,42    # sadece bu plakalar
"""
from __future__ import annotations

import logging
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import chamber_scraper as chamber
from provinces import ALL_PLATE_CODES, PLATE_CITY, normalize_text

# Keşifte hızlı ol: başarısız adaylarda retry/backoff bekleme + log gürültüsünü kıs
chamber.MAX_RETRIES = 1
chamber.REQUEST_TIMEOUT = 8
logging.getLogger("chamber_scraper").setLevel(logging.CRITICAL)

_SESSION = requests.Session()
_SESSION.headers.update(chamber.HEADERS)


def fast_soup(url: str) -> BeautifulSoup | None:
    """Tek denemeli, hızlı GET -> BeautifulSoup (hata olursa None)."""
    try:
        r = _SESSION.get(url, timeout=8)
        if r.status_code != 200:
            return None
        html = r.content.decode(r.apparent_encoding or "utf-8", "replace")
        return BeautifulSoup(html, "lxml")
    except Exception:
        return None


def find_nobet_links(host: str) -> list[str]:
    """Oda ana sayfasından 'nöbet' içeren linkleri bulur (gerçek sayfa yolu)."""
    for base in (f"https://www.{host}", f"https://{host}"):
        soup = fast_soup(base + "/")
        if soup is None:
            continue
        links: list[str] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            txt = a.get_text(" ", strip=True)
            if "nobet" in href.lower() or "nobet" in normalize_text(txt):
                full = urljoin(base + "/", href)
                if full not in seen:
                    seen.add(full)
                    links.append(full)
        if links:
            return links[:6]
    return []

# TİTCK'e veri beslemeyen, zaten oda'dan çekilen 20 il (bunları atla)
CHAMBER_PLATES = set(chamber.CHAMBER_SOURCES) | {27, 79}

# İl adının domain'de geçen kısa biçimi (gözlemlenen istisnalar)
SLUG_ALIASES = {
    "afyonkarahisar": "afyon",
    "kahramanmaras": "maras",
    "sanliurfa": "urfa",
    "gaziantep": "antep",
}

# Yol şablonları (oda sitelerinde gözlemlenen)
PATHS = ["/nobetci-eczaneler", "/nobetci-eczaneler/{plate}", "/nobetciler", "/"]
# Domain ekleri: {izmir}eczaciodasi, {burdur}eo, {bitlis}ecza ...
DOMAIN_SUFFIXES = ["eczaciodasi", "eo", "ecza", "eczaodasi"]


def slugs_for(city: str) -> list[str]:
    base = normalize_text(city).replace(" ", "").replace(".", "")
    out = [base]
    if base in SLUG_ALIASES:
        out.insert(0, SLUG_ALIASES[base])
    return out


def candidate_hosts(city: str) -> list[str]:
    hosts: list[str] = []
    for slug in slugs_for(city):
        for suf in DOMAIN_SUFFIXES:
            hosts.append(f"{slug}{suf}.org.tr")
    seen, uniq = set(), []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def try_url(url: str, city: str, plate: int) -> dict | None:
    try:
        res = chamber.scrape_chamber(url, city, str(plate), multi=False)
    except Exception:
        return None
    if res.success and res.pharmacies:
        coords = sum(1 for p in res.pharmacies if p.get("lat") and p.get("lng"))
        return {"plate": plate, "city": city, "url": url,
                "count": len(res.pharmacies), "coords": coords}
    return None


def probe_province(plate: int) -> dict | None:
    city = PLATE_CITY[plate]
    for host in candidate_hosts(city):
        # 1) ana sayfadan gerçek "nöbet" linklerini bul ve dene
        for url in find_nobet_links(host):
            hit = try_url(url, city, plate)
            if hit:
                return hit
        # 2) doğrudan bilinen yol şablonlarını dene
        for path in PATHS:
            url = f"https://www.{host}{path.format(plate=plate)}"
            hit = try_url(url, city, plate)
            if hit:
                return hit
    return None


def main() -> int:
    if len(sys.argv) > 1:
        plates = [int(x) for x in sys.argv[1].split(",") if x.strip().isdigit()]
    else:
        plates = [p for p in ALL_PLATE_CODES if p not in CHAMBER_PLATES]

    print(f"== Keşif: {len(plates)} il ==", flush=True)
    found: list[dict] = []
    missing: list[int] = []

    for i, plate in enumerate(plates, 1):
        city = PLATE_CITY[plate]
        print(f"[{i}/{len(plates)}] {plate:>2} {city} ...", end=" ", flush=True)
        hit = probe_province(plate)
        if hit:
            found.append(hit)
            print(f"BULUNDU -> {hit['url']}  ({hit['count']} ecz, {hit['coords']} koord)", flush=True)
        else:
            missing.append(plate)
            print("bulunamadi", flush=True)
        time.sleep(0.5)

    print("\n================ ÖZET ================")
    print(f"BULUNAN: {len(found)} / {len(plates)}")
    print("\n# Registry'e eklenebilecek (plate: url):")
    for h in sorted(found, key=lambda x: x["plate"]):
        cflag = "True" if h["coords"] else "False"
        print(f'    {h["plate"]}: {{"url": "{h["url"]}", "coords": {cflag}}},  # {h["city"]} ({h["count"]} ecz)')

    print(f"\nBULUNAMAYAN: {len(missing)}")
    print("  " + ", ".join(f"{p} {PLATE_CITY[p]}" for p in missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
