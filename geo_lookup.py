"""Çevrimdışı il tespiti: koordinat -> plaka, nokta-içinde-poligon ile.

Türkiye il sınırları GeoJSON'undan (data/tr_cities.json, 81 il, 'number'
alanı = plaka kodu) yararlanır. Ağ gerektirmez, sınırsız ve anlık. Konum→il
çevirimini Nominatim'e gitmeden yapar; bulunamayan (sınır/kıyı/GPS hatası)
noktalar çağırana None döner ve orada Nominatim'e yedek düşülür.

Veri kaynağı: alpers/Turkey-Maps-GeoJSON (kamuya açık).
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_DATA = os.path.join(os.path.dirname(__file__), "data", "tr_cities.json")

# Her il için: (plaka, (min_lng,min_lat,max_lng,max_lat), [polygon, ...])
# polygon = [ring, ...]; ring = [(x,y), ...]  (x=lng, y=lat)
_PROVINCES: list[tuple[int, tuple[float, float, float, float], list]] = []


def _rings_to_polys(geom: dict) -> list:
    """GeoJSON Polygon/MultiPolygon -> [polygon,...]; polygon=[ring,...]."""
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        return [[[(float(p[0]), float(p[1])) for p in ring] for ring in coords]]
    if t == "MultiPolygon":
        return [[[(float(p[0]), float(p[1])) for p in ring] for ring in poly]
                for poly in coords]
    return []


def _bbox(polys: list) -> tuple[float, float, float, float]:
    xs_min = ys_min = float("inf")
    xs_max = ys_max = float("-inf")
    for poly in polys:
        for ring in poly:
            for x, y in ring:
                if x < xs_min: xs_min = x
                if x > xs_max: xs_max = x
                if y < ys_min: ys_min = y
                if y > ys_max: ys_max = y
    return (xs_min, ys_min, xs_max, ys_max)


def _load() -> None:
    if _PROVINCES:
        return
    try:
        with open(_DATA, encoding="utf-8") as f:
            gj = json.load(f)
    except Exception:
        logger.exception("İl sınırı verisi yüklenemedi: %s", _DATA)
        return
    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        num = props.get("number")
        polys = _rings_to_polys(feat.get("geometry") or {})
        if num is None or not polys:
            continue
        _PROVINCES.append((int(num), _bbox(polys), polys))
    logger.info("Çevrimdışı il sınırı yüklendi: %d il", len(_PROVINCES))


def _ring_crosses(x: float, y: float, ring: list) -> bool:
    """Ray-casting: noktanın ring'i tek (True) / çift (False) kez kestiği."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_polygon(x: float, y: float, polygon: list) -> bool:
    """polygon = [dış halka, delik1, ...]; even-odd ile delikleri de hesaba katar."""
    inside = False
    for ring in polygon:
        if _ring_crosses(x, y, ring):
            inside = not inside
    return inside


def province_for(lat: float, lng: float) -> int | None:
    """Koordinatın düştüğü ilin plaka kodunu döner; bulunamazsa None."""
    _load()
    x, y = lng, lat
    for plate, (minx, miny, maxx, maxy), polys in _PROVINCES:
        if x < minx or x > maxx or y < miny or y > maxy:
            continue
        for poly in polys:
            if _in_polygon(x, y, poly):
                return plate
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tests = [
        ("İstanbul", 41.0082, 28.9784, 34),
        ("Ankara", 39.9334, 32.8597, 6),
        ("İzmir", 38.4237, 27.1428, 35),
        ("Kahramanmaraş", 37.5753, 36.9228, 46),
        ("Van", 38.4942, 43.3800, 65),
        ("Antalya", 36.8969, 30.7133, 7),
        ("Gaziantep", 37.0662, 37.3833, 27),
    ]
    for name, la, lo, exp in tests:
        got = province_for(la, lo)
        print(f"{name}: beklenen {exp}, çıkan {got}  {'OK' if got == exp else 'HATA'}")
