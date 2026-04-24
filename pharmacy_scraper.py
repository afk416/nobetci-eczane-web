"""Kahramanmaraş Belediyesi nöbetçi eczane sayfasından veri çeker."""
import re
import time
import requests
from bs4 import BeautifulSoup

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


def fetch_pharmacies(force_refresh: bool = False):
    """Nöbetçi eczane listesini döndürür. 30 dk cache'li."""
    now = time.time()
    if not force_refresh and _cache["data"] and (now - _cache["ts"] < _CACHE_TTL):
        return _cache["data"]

    r = requests.get(URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")

    wrapper = soup.find(class_="eczaneler-wrapper")
    if not wrapper:
        return _cache["data"] or []

    rows = wrapper.find_all(class_="eczane-row")
    data = [_parse_row(row) for row in rows]
    data = [d for d in data if d["ad"]]

    _cache["ts"] = now
    _cache["data"] = data
    return data


if __name__ == "__main__":
    import json
    for p in fetch_pharmacies():
        print(json.dumps(p, ensure_ascii=False))
