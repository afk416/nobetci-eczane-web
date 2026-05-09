"""Kahramanmaraş Belediyesi nöbetçi eczane sayfasından veri çeker."""
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

TURKEY_TZ = timezone(timedelta(hours=3))
DAILY_REFRESH_HOUR = 9  # Eczaneler Türkiye saatiyle 09:00'da değişiyor

# Sadece bu ilçelerdeki eczaneleri gösteriyoruz (Kahramanmaraş şehir merkezi)
TARGET_DISTRICTS = ("onikisubat", "dulkadiroglu", "merkez")


def _normalize(s: str) -> str:
    """Türkçe karakter ve büyük/küçük harf fark etmeden karşılaştırma için."""
    return (
        s.lower()
        .replace("ı", "i")
        .replace("ş", "s")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ö", "o")
        .replace("ç", "c")
    )


def _section_matches_target(h1_text: str) -> bool:
    norm = _normalize(h1_text)
    if "nobetci" in norm and "eczane" in norm and len(norm) < 25:
        return False  # genel sayfa başlığı, atla
    return any(d in norm for d in TARGET_DISTRICTS)

URL = "https://kahramanmaras.bel.tr/nobetci-eczaneler"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_COORD_RE = re.compile(r"q=(-?\d+\.\d+),\s*(-?\d+\.\d+)")

_cache = {"ts": 0.0, "data": []}
_CACHE_TTL = 1800  # 30 dk


def _parse_row(row):
    ad_el = row.find(class_="eczane-ad")
    adres_el = row.find(class_="eczane-adres")

    ad = ad_el.get_text(" ", strip=True) if ad_el else ""
    adres = adres_el.get_text(" ", strip=True) if adres_el else ""

    lat = lng = None
    harita = None
    tel = None

    map_a = row.find("a", class_="eczane-link-map")
    if map_a and map_a.get("href"):
        harita = map_a["href"]
        m = _COORD_RE.search(harita)
        if m:
            lat = float(m.group(1))
            lng = float(m.group(2))

    for a in row.find_all("a"):
        href = a.get("href", "")
        if href.startswith("tel:"):
            tel = href.replace("tel:", "").strip()
            break

    ilce = None
    if " - " in ad:
        parts = ad.rsplit(" - ", 1)
        ad_temiz = parts[0].strip()
        ilce = parts[1].strip()
    else:
        ad_temiz = ad

    return {
        "ad": ad_temiz,
        "ilce": ilce,
        "adres": adres,
        "tel": tel,
        "lat": lat,
        "lng": lng,
        "harita": harita,
    }


def _last_refresh_boundary_ts() -> float:
    """En son geçilen 09:00 (TR) anının unix zamanı."""
    now_tr = datetime.now(TURKEY_TZ)
    boundary = now_tr.replace(
        hour=DAILY_REFRESH_HOUR, minute=0, second=0, microsecond=0
    )
    if now_tr < boundary:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


def fetch_pharmacies(force_refresh: bool = False):
    """Nöbetçi eczane listesini döndürür. 30 dk cache'li, 09:00'da otomatik yenilenir."""
    now = time.time()
    boundary = _last_refresh_boundary_ts()
    cache_fresh = (
        _cache["data"]
        and (now - _cache["ts"] < _CACHE_TTL)
        and _cache["ts"] >= boundary
    )
    if not force_refresh and cache_fresh:
        return _cache["data"]

    r = requests.get(URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")

    # Sayfa ilcelere gore gruplu (h1 + sonraki .eczaneler-wrapper).
    # Sadece TARGET_DISTRICTS'taki bolumleri aliyoruz (Onikişubat + Dulkadiroğlu).
    seen = set()
    data = []
    for wrapper in soup.find_all(class_="eczaneler-wrapper"):
        h1 = wrapper.find_previous("h1")
        if not h1 or not _section_matches_target(h1.get_text(strip=True)):
            continue
        for row in wrapper.find_all(class_="eczane-row"):
            p = _parse_row(row)
            if not p["ad"]:
                continue
            key = (p["ad"], p.get("lat"), p.get("lng"))
            if key in seen:
                continue
            seen.add(key)
            data.append(p)

    _cache["ts"] = now
    _cache["data"] = data
    return data


if __name__ == "__main__":
    import json
    for p in fetch_pharmacies():
        print(json.dumps(p, ensure_ascii=False))
