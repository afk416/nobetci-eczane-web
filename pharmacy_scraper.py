"""Türkiye geneli nöbetçi eczane verisini CollectAPI üzerinden çeker."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TURKEY_TZ = timezone(timedelta(hours=3))
DAILY_REFRESH_HOUR = 9  # Eczaneler TR saatiyle 09:00'da değişiyor

# CollectAPI ayarları (token önce env, sonra lokal dosya)
COLLECTAPI_URL = "https://api.collectapi.com/health/dutyPharmacy"
COLLECTAPI_TOKEN = os.environ.get("COLLECTAPI_TOKEN", "").strip()
if not COLLECTAPI_TOKEN:
    try:
        # Lokal geliştirme için: proje kökündeki collectapi_token.txt
        for candidate in (
            Path(__file__).parent / "collectapi_token.txt",
            Path(__file__).parent.parent / "collectapi_token.txt",
        ):
            if candidate.exists():
                COLLECTAPI_TOKEN = candidate.read_text(encoding="utf-8").strip()
                break
    except Exception:
        pass

# Cache: il_key -> {"ts": float, "data": list}
_cache: dict = {}
_CACHE_TTL = 1800  # 30 dk

# Geriye dönük uyumluluk: bot.py bu sabiti import ediyor
DISTRICT_NORMALIZE = {
    "merkez": "merkez",
    "onikisubat": "merkez",
    "dulkadiroglu": "merkez",
}


# ---------------------------------------------------------------------------
# Türkçe karakter normalize
# ---------------------------------------------------------------------------

def normalize_text(s: str) -> str:
    """'Onikişubat' → 'onikisubat'.

    Türkçe büyük 'İ' karakteri Python .lower() ile sorun çıkardığı için
    önce Türkçe karakterleri ASCII'ye çevirip sonra lower yapıyoruz.
    """
    if not s:
        return ""
    # Önce Türkçe → ASCII (lower'dan önce, "İ" sorunu için kritik)
    repl = (
        ("İ", "I"), ("ı", "i"),
        ("Ş", "S"), ("ş", "s"),
        ("Ğ", "G"), ("ğ", "g"),
        ("Ü", "U"), ("ü", "u"),
        ("Ö", "O"), ("ö", "o"),
        ("Ç", "C"), ("ç", "c"),
    )
    for a, b in repl:
        s = s.replace(a, b)
    return s.lower().strip()


def district_key(name: str) -> str:
    """İlçe adından normalize anahtar. Onikişubat/Dulkadiroğlu → 'merkez'."""
    norm = normalize_text(name)
    return DISTRICT_NORMALIZE.get(norm, norm)


# Geriye dönük uyumluluk: önceki kodda kullanıldı
_normalize = normalize_text


# ---------------------------------------------------------------------------
# CollectAPI çağrısı
# ---------------------------------------------------------------------------

def _fetch_from_api(il: str) -> list:
    if not COLLECTAPI_TOKEN:
        raise RuntimeError(
            "COLLECTAPI_TOKEN ayarlı değil (env var veya collectapi_token.txt)"
        )
    headers = {
        "Authorization": f"apikey {COLLECTAPI_TOKEN}",
        "content-type": "application/json",
    }
    params = {"il": il, "ilce": ""}
    r = requests.get(COLLECTAPI_URL, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"CollectAPI hata: {body.get('message', 'bilinmeyen')}")
    return body.get("result", []) or []


def _parse_phone(raw: str) -> str | None:
    """'0(344)511-66-46' → '+903445116646'."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return None
    if digits.startswith("90") and len(digits) >= 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) >= 11:
        return "+9" + digits
    if len(digits) == 10:
        return "+90" + digits
    return digits


def _transform(item: dict) -> dict:
    """CollectAPI cevabını bizim standart formata çevir."""
    loc = item.get("loc") or ""
    lat = lng = None
    if "," in loc:
        try:
            lat_s, lng_s = loc.split(",", 1)
            lat = float(lat_s.strip())
            lng = float(lng_s.strip())
        except (ValueError, AttributeError):
            pass

    harita = None
    if lat is not None and lng is not None:
        harita = f"https://www.google.com/maps?q={lat},{lng}"

    dist = (item.get("dist") or "").strip()
    return {
        "ad": (item.get("name") or "").strip(),
        "ilce": dist,
        "ilce_key": district_key(dist),
        "adres": (item.get("address") or "").strip(),
        "tel": _parse_phone(item.get("phone")),
        "lat": lat,
        "lng": lng,
        "harita": harita,
    }


def _last_refresh_boundary_ts() -> float:
    """En son geçilen 09:00 (TR) anının unix timestamp'i."""
    now_tr = datetime.now(TURKEY_TZ)
    boundary = now_tr.replace(
        hour=DAILY_REFRESH_HOUR, minute=0, second=0, microsecond=0
    )
    if now_tr < boundary:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


def fetch_pharmacies(il: str = "kahramanmaras", force_refresh: bool = False) -> list:
    """Belirtilen ildeki nöbetçi eczaneleri döndürür.

    Cache: 30 dk + her gün 09:00 TR sınırında otomatik yenileme.
    Her il için ayrı cache tutulur.
    """
    il_key = normalize_text(il) or "kahramanmaras"
    now = time.time()
    boundary = _last_refresh_boundary_ts()

    cached = _cache.get(il_key)
    if not force_refresh and cached:
        cache_fresh = (
            cached.get("data")
            and (now - cached["ts"] < _CACHE_TTL)
            and cached["ts"] >= boundary
        )
        if cache_fresh:
            return cached["data"]

    try:
        raw = _fetch_from_api(il_key)
    except Exception:
        logger.exception("CollectAPI çağrısı başarısız (il=%s)", il_key)
        if cached and cached.get("data"):
            logger.warning("Eski cache verisi döndürülüyor")
            return cached["data"]
        raise

    data = [_transform(item) for item in raw]
    data = [d for d in data if d["ad"]]
    _cache[il_key] = {"ts": now, "data": data}
    logger.info("CollectAPI: %s için %d eczane çekildi", il_key, len(data))
    return data


# ---------------------------------------------------------------------------
# Reverse geocode — koordinat → il + ilçe
# ---------------------------------------------------------------------------

# Türkiye'deki 81 il, normalize anahtarlar
TURKEY_PROVINCES = {
    "adana", "adiyaman", "afyonkarahisar", "afyon", "agri", "aksaray", "amasya",
    "ankara", "antalya", "ardahan", "artvin", "aydin", "balikesir", "bartin",
    "batman", "bayburt", "bilecik", "bingol", "bitlis", "bolu", "burdur", "bursa",
    "canakkale", "cankiri", "corum", "denizli", "diyarbakir", "duzce", "edirne",
    "elazig", "erzincan", "erzurum", "eskisehir", "gaziantep", "giresun",
    "gumushane", "hakkari", "hatay", "igdir", "isparta", "istanbul", "izmir",
    "kahramanmaras", "karabuk", "karaman", "kars", "kastamonu", "kayseri",
    "kilis", "kirikkale", "kirklareli", "kirsehir", "kocaeli", "konya", "kutahya",
    "malatya", "manisa", "mardin", "mersin", "mugla", "mus", "nevsehir", "nigde",
    "ordu", "osmaniye", "rize", "sakarya", "samsun", "sanliurfa", "siirt", "sinop",
    "sivas", "sirnak", "tekirdag", "tokat", "trabzon", "tunceli", "usak", "van",
    "yalova", "yozgat", "zonguldak",
}

# Eski/alternatif isimler
PROVINCE_ALIASES = {
    "icel": "mersin",
    "afyon": "afyonkarahisar",
    "maras": "kahramanmaras",
    "kmaras": "kahramanmaras",
    "k.maras": "kahramanmaras",
    "k maras": "kahramanmaras",
    "urfa": "sanliurfa",
    "antep": "gaziantep",
}


def _detect_province(addr: dict) -> str | None:
    """Nominatim address dict'inden Türk il anahtarını bulur."""
    candidates = []
    for field in ("province", "state", "region", "state_district"):
        v = addr.get(field)
        if v:
            candidates.append(v)

    for raw in candidates:
        norm = normalize_text(raw)
        # 1) Doğrudan eşleşme
        if norm in TURKEY_PROVINCES:
            return norm
        if norm in PROVINCE_ALIASES:
            return PROVINCE_ALIASES[norm]
        # 2) Kelime kelime tara ("Kahramanmaras Province" gibi)
        for word in norm.split():
            if word in TURKEY_PROVINCES:
                return word
            if word in PROVINCE_ALIASES:
                return PROVINCE_ALIASES[word]
    return None


def get_location_info(lat: float, lng: float) -> dict | None:
    """Koordinattan il + ilçe bilgisini Nominatim üzerinden döner.

    Returns:
        {"il": "kahramanmaras", "ilce_key": "merkez", "ilce_display": "Onikişubat"}
        veya None
    """
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
    """ESKI API — sadece ilçe anahtarını döner."""
    info = get_location_info(lat, lng)
    return info.get("ilce_key") if info else None


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(f"Toplam: {len(fetch_pharmacies('kahramanmaras'))} (Maraş)")
    for p in fetch_pharmacies("kahramanmaras")[:3]:
        print(json.dumps(p, ensure_ascii=False))
