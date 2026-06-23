"""Nöbetçi eczane verisini SQLite depodan (TİTCK kaynaklı) okur.

Veri, arka plandaki scrape_all.py işi tarafından e-Devlet/TİTCK'ten çekilip
SQLite'a yazılır. Bu modül kullanıcı isteğinde SADECE yerel depodan okur;
e-Devlet'e dokunmaz (rate-limit güvenliği). Konum→il/ilçe tespiti Nominatim
ile yapılır.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

import geo_lookup
import pharmacy_store as store
from provinces import (  # noqa: F401 (geriye dönük uyumluluk için re-export)
    DISTRICT_NORMALIZE,
    PLATE_CITY,
    PROVINCE_ALIASES,
    TURKEY_PROVINCES,
    district_key,
    normalize_text,
    province_to_plate,
)

logger = logging.getLogger(__name__)

TURKEY_TZ = timezone(timedelta(hours=3))
# Eczaneler TR saatiyle 08:30'da değişiyor
DAILY_REFRESH_HOUR = 8
DAILY_REFRESH_MINUTE = 30

_normalize = normalize_text  # geriye dönük uyumluluk


# ---------------------------------------------------------------------------
# Güncel nöbet günü
# ---------------------------------------------------------------------------

def _current_duty_dates() -> list[str]:
    """Denenecek nöbet günleri (ISO), öncelik sırasıyla.

    08:30'dan önce aktif nöbet dünden gelir; sonra bugün. Depoda hangisi
    varsa o kullanılır.
    """
    now = datetime.now(TURKEY_TZ)
    boundary = now.replace(
        hour=DAILY_REFRESH_HOUR, minute=DAILY_REFRESH_MINUTE, second=0, microsecond=0
    )
    today = now.date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    if now < boundary:
        # 00:00–08:30: aktif nöbet dünden; ardından bugün/yarın yedek
        order = [yesterday, today, tomorrow]
    else:
        # 08:30 sonrası "dün" SÜRESİ DOLMUŞ nöbet günü — sessizce göstermek
        # kapalı eczaneye yönlendirir; yedeğe koymuyoruz (max doğruluk). Bugün
        # yoksa boş dönülür ("veri yok"), yanlış günün listesi gösterilmez.
        order = [today, tomorrow]
    return [d.strftime("%Y-%m-%d") for d in order]


# ---------------------------------------------------------------------------
# Depodan okuma
# ---------------------------------------------------------------------------

def _to_output(row: dict) -> dict:
    """Depo satırını web/bot'un beklediği standart şekle çevirir."""
    lat = row.get("lat")
    lng = row.get("lng")
    harita = None
    if lat is not None and lng is not None:
        harita = f"https://www.google.com/maps?q={lat},{lng}"
    return {
        "ad": (row.get("name") or "").strip(),
        "ilce": (row.get("district") or "").strip(),
        "ilce_key": row.get("district_key") or district_key(row.get("district") or ""),
        "adres": (row.get("address") or "").strip(),
        "tel": row.get("phone"),
        "lat": lat,
        "lng": lng,
        "harita": harita,
    }


def fetch_pharmacies(il: str = "kahramanmaras", force_refresh: bool = False) -> list:
    """Belirtilen ildeki güncel nöbetçi eczaneleri SQLite depodan döner.

    force_refresh artık kullanılmıyor (veri arka plan işiyle güncelleniyor);
    imza geriye dönük uyumluluk için korundu.
    """
    plate = province_to_plate(il)
    if plate is None:
        logger.warning("Bilinmeyen il: %s", il)
        return []

    for duty_iso in _current_duty_dates():
        rows = store.get_province(duty_iso, plate)
        if rows:
            data = [_to_output(r) for r in rows]
            data = [d for d in data if d["ad"]]
            logger.info("Depo: %s (plaka %s, %s) için %d eczane", il, plate, duty_iso, len(data))
            return data

    logger.warning("Depoda veri yok: %s (plaka %s)", il, plate)
    return []


# ---------------------------------------------------------------------------
# Reverse geocode — koordinat → il + ilçe
# ---------------------------------------------------------------------------

def _detect_province(addr: dict) -> str | None:
    """Nominatim address dict'inden Türk il anahtarını bulur."""
    candidates = []
    for field in ("province", "state", "region", "state_district"):
        v = addr.get(field)
        if v:
            candidates.append(v)

    for raw in candidates:
        norm = normalize_text(raw)
        if norm in TURKEY_PROVINCES:
            return PROVINCE_ALIASES.get(norm, norm)
        if norm in PROVINCE_ALIASES:
            return PROVINCE_ALIASES[norm]
        for word in norm.split():
            if word in TURKEY_PROVINCES:
                return PROVINCE_ALIASES.get(word, word)
            if word in PROVINCE_ALIASES:
                return PROVINCE_ALIASES[word]
    return None


def get_location_info(lat: float, lng: float) -> dict | None:
    """Koordinattan il (+ varsa ilçe) bilgisi — HİBRİT.

    1) Önce ÇEVRİMDIŞI il sınırı poligonu (ağ yok, sınırsız, anlık).
    2) Çevrimdışı bulamazsa (sınır/kıyı/GPS hatası) Nominatim'e yedek düşer.

    Çevrimdışı yolda ilçe etiketi dönmez (ilce_display=None); ilçe yalnız
    yedek Nominatim devreye girince gelir. İlçe sadece ekran etiketidir,
    eczane süzmeyi etkilemez.
    """
    plate = geo_lookup.province_for(lat, lng)
    if plate is not None:
        city = PLATE_CITY.get(plate)
        if city:
            return {"il": normalize_text(city), "ilce_key": None, "ilce_display": None}
    # Çevrimdışı bulunamadı -> Nominatim yedek
    return _nominatim_location_info(lat, lng)


def _nominatim_location_info(lat: float, lng: float) -> dict | None:
    """Yedek: koordinattan il + ilçe bilgisini Nominatim üzerinden döner."""
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?lat={lat}&lon={lng}&format=jsonv2&addressdetails=1&accept-language=tr"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "nobetcim-bot/1.0 (afk416@gmail.com)"},
            timeout=8,
        )
        r.raise_for_status()
        addr = r.json().get("address", {})

        il = _detect_province(addr)
        if not il:
            return None

        ilce_display = (
            addr.get("town")
            or addr.get("city_district")
            or addr.get("district")
            or addr.get("county")
            or addr.get("municipality")
            or addr.get("village")
            or ""
        ).strip()

        return {
            "il": il,
            "ilce_key": district_key(ilce_display) if ilce_display else None,
            "ilce_display": ilce_display,
        }
    except Exception:
        logger.exception("Nominatim reverse geocoding hatası")
        return None


# Geriye dönük uyumluluk
def get_district_for_location(lat: float, lng: float):
    info = get_location_info(lat, lng)
    return info.get("ilce_key") if info else None


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    data = fetch_pharmacies("kahramanmaras")
    print(f"Toplam: {len(data)} (Maraş)")
    for p in data[:3]:
        print(json.dumps(p, ensure_ascii=False))
