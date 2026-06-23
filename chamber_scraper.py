"""Eczacı Odası (chamber) nöbetçi eczane scraper'ı.

TİTCK'e veri beslemeyen 20 il için, ilgili eczacı odasının resmi
sitesinden nöbetçi eczaneleri çeker. Odaların çoğu OBEN Teknoloji
platformunu kullanıyor; sayfa sunucu tarafında render edilen statik
HTML'dir ve koordinatlar `maps?q=LAT,LNG` bağlantılarında gömülüdür.

Üç farklı şablon görülüyor ama hepsinde ORTAK çapa şu ikisidir:
  - Google Maps linki:  <a href="...maps?q=LAT,LNG">
  - Telefon linki:      <a href="tel:...">
İl ataması, en yakın önceki bölüm başlığından ("... Bugün Nöbetçi
Eczaneler") yapılır; başlıkta il adı yoksa çağırana verilen beklenen il
kullanılır (tek illi eski şablon sayfaları için).

Kullanım:
    python chamber_scraper.py 15           # Burdur (plaka)
    python chamber_scraper.py 65           # Van
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

MAPS_RE = re.compile(r"maps\?q=(-?[\d.]+),(-?[\d.]+)")
# "03 Haziran 2026 Van'da Bugün Nöbetçi Eczaneler" / "Bartın Bugün Nöbetçi..."
SECTION_RE = re.compile(r"Bug.n\s+N.bet.i\s+Eczaneler", re.IGNORECASE)
DATE_PREFIX_RE = re.compile(r"^\d{1,2}\s+\S+\s+\d{4}\s+")

# TİTCK'e veri beslemeyen 20 ilin eczacı odası kaynakları.
# Çoğu OBEN Teknoloji platformu (server-rendered HTML, maps?q= koordinat).
# Bazı sayfalar birden çok il içerir; il başlıklarına göre süzülür.
#   coords=False  -> sayfada koordinat yok, Nominatim ile geocode gerekir
CHAMBER_SOURCES: dict[int, dict] = {
    3:  {"url": "https://www.afyoneczaciodasi.org.tr/nobetci-eczaneler",        "coords": True},
    8:  {"url": "https://www.trabzoneczaciodasi.org.tr/nobetci-eczaneler/8",    "coords": True},
    9:  {"url": "https://www.aydineczaciodasi.org.tr/nobetci-eczaneler",        "coords": True},
    13: {"url": "https://www.bitlisecza.org.tr/nobetci-eczaneler",              "coords": True},
    15: {"url": "https://www.burdureo.org.tr/nobetci-eczaneler",               "coords": True},
    21: {"url": "https://www.diyarbakireo.org.tr/nobetci-eczaneler",           "coords": True},
    22: {"url": "https://www.edirneeo.org.tr/nobetci-eczaneler",               "coords": True},
    23: {"url": "https://www.elazigeczaciodasi.org.tr/nobetci-eczaneler/elazig", "coords": True},
    26: {"url": "https://www.eskisehireo.org.tr/eskisehir-nobetci-eczaneler/", "coords": True},
    30: {"url": "https://www.vaneczaciodasi.org.tr/nobetci-eczaneler",          "coords": True, "multi": True},  # Hakkari (Van sayfası)
    35: {"url": "https://www.izmireczaciodasi.org.tr/nobetci-eczaneler",        "coords": True},
    43: {"url": "https://www.kutahyaeo.org.tr/nobetci-eczaneler",              "coords": True},
    44: {"url": "https://www.malatyaeczaciodasi.org.tr/nobetci-eczaneler",      "coords": False},
    49: {"url": "https://www.batmaneczaciodasi.org.tr/nobetci-eczaneler/49",    "coords": True},  # Muş
    65: {"url": "https://www.vaneczaciodasi.org.tr/nobetci-eczaneler",          "coords": True, "multi": True},  # Van
    72: {"url": "https://www.batmaneczaciodasi.org.tr/nobetci-eczaneler/72",    "coords": True},  # Batman
    73: {"url": "https://sirnakeo.org.tr/nobetci-eczaneler",                    "coords": True},
    74: {"url": "https://www.zeo.org.tr/nobetci-eczaneler",                     "coords": True, "multi": True},  # Bartın (Zonguldak sayfası)
    # 79 Kilis -> Eflatunweb/Netosfer, ayrı parser (kilis_scraper)
}

# TİTCK birincil; bu iller TİTCK'tan bugün veri ALINAMAZSA (boş/hata) oda
# sitesinden YEDEK olarak çekilir. discover_chambers.py ile keşfedildi.
# Yalnız OBEN platformlu, parser'ımızın tuttuğu odalar buradadır.
FALLBACK_SOURCES: dict[int, dict] = {
    1:  {"url": "https://www.adanaeo.org.tr/nobetci-eczaneler",            "coords": True},  # Adana
    2:  {"url": "https://www.adiyamaneo.org.tr/nobetci-eczaneler",         "coords": True},  # Adıyaman
    4:  {"url": "https://www.agrieo.org.tr/nobetci-eczaneler",             "coords": True},  # Ağrı
    10: {"url": "https://www.balikesireczaciodasi.org.tr/nobetci-eczaneler", "coords": True},  # Balıkesir
    17: {"url": "https://www.canakkaleeo.org.tr/nobetci-eczaneler",        "coords": True},  # Çanakkale
    19: {"url": "https://www.corumeo.org.tr/nobetci-eczaneler",            "coords": True},  # Çorum
    20: {"url": "https://www.denizlieczaciodasi.org.tr/nobetci-eczaneler", "coords": True},  # Denizli
    24: {"url": "https://www.erzincaneo.org.tr/nobetci-eczaneler",         "coords": True},  # Erzincan
    25: {"url": "https://www.erzurumeo.org.tr/nobetci-eczaneler/25",       "coords": True},  # Erzurum
    31: {"url": "https://www.hatayeo.org.tr/nobetci-eczaneler",            "coords": True},  # Hatay
    32: {"url": "https://www.ispartaeo.org.tr/nobetci-eczaneler",          "coords": True},  # Isparta
    37: {"url": "https://www.kastamonueo.org.tr/nobetci-eczaneler/37",     "coords": True},  # Kastamonu
    39: {"url": "https://www.kirklarelieo.org.tr/nobetci-eczaneler",       "coords": True},  # Kırklareli
    45: {"url": "https://www.manisaeczaciodasi.org.tr/nobetci-eczaneler",  "coords": True},  # Manisa
    46: {"url": "https://www.kahramanmaraseo.org.tr/nobetci-eczaneler",    "coords": True},  # Kahramanmaraş
    47: {"url": "https://www.mardineczaciodasi.org.tr/nobetci-eczaneler",  "coords": True},  # Mardin
    48: {"url": "https://www.muglaeczaciodasi.org.tr/nobetci-eczaneler",   "coords": True},  # Muğla
    50: {"url": "https://www.nevsehireo.org.tr/nobetci-eczaneler",         "coords": True},  # Nevşehir
    55: {"url": "https://www.samsuneczaciodasi.org.tr/nobetci-eczaneler",  "coords": True},  # Samsun
    56: {"url": "https://www.siirteo.org.tr/nobetci-eczaneler",            "coords": True},  # Siirt
    58: {"url": "https://www.sivaseo.org.tr/nobetci-eczaneler",            "coords": True},  # Sivas
    61: {"url": "https://www.trabzoneczaciodasi.org.tr/nobetci-eczaneler/61", "coords": True},  # Trabzon
    66: {"url": "https://www.yozgateo.org.tr/nobetci-eczaneler",           "coords": True},  # Yozgat
    67: {"url": "https://www.zonguldakeczaciodasi.org.tr/nobetci-eczaneler", "coords": True},  # Zonguldak
    68: {"url": "https://www.aksarayeo.org.tr/nobetci-eczaneler",          "coords": True},  # Aksaray
    80: {"url": "https://www.osmaniyeeczaciodasi.org.tr/nobetci-eczaneler", "coords": True},  # Osmaniye
    # 2026-06 keşfi (discover-oda-sites workflow): OBEN parser'ın tuttuğu, il-bazlı
    # filtreli URL'lerle doğrulanmış 21 il daha. Çoğu komşu illeri tek odadan
    # plaka/slug yoluyla servis ediyor (erzurumeo, trabzon, kastamonueo, seo...).
    11: {"url": "https://www.eskisehireo.org.tr/bilecik-nobetci-eczaneler",      "coords": True},  # Bilecik
    12: {"url": "https://www.elazigeczaciodasi.org.tr/nobetci-eczaneler/bingol", "coords": True},  # Bingöl
    14: {"url": "https://www.seo.org.tr/nobetci-eczaneler/14",                   "coords": True},  # Bolu
    16: {"url": "https://www.beo.org.tr/nobetci-eczaneler",                      "coords": True},  # Bursa
    18: {"url": "https://www.kastamonueo.org.tr/nobetci-eczaneler/18",           "coords": True},  # Çankırı
    29: {"url": "https://www.trabzoneczaciodasi.org.tr/nobetci-eczaneler/29",    "coords": True},  # Gümüşhane
    33: {"url": "https://www.mersineczaciodasi.org.tr/nobetci-eczaneler",        "coords": True},  # Mersin
    36: {"url": "https://www.erzurumeo.org.tr/nobetci-eczaneler/36",             "coords": True},  # Kars
    38: {"url": "https://www.kayserieo.org.tr/nobetci-eczaneler",                "coords": True},  # Kayseri
    41: {"url": "https://www.kocaelieo.org.tr/nobetci-eczaneler",                "coords": True},  # Kocaeli
    51: {"url": "https://www.neo.org.tr/nobetci-eczaneler",                      "coords": True},  # Niğde
    53: {"url": "https://www.trabzoneczaciodasi.org.tr/nobetci-eczaneler/53",    "coords": True},  # Rize
    54: {"url": "https://www.seo.org.tr/nobetci-eczaneler",                      "coords": True},  # Sakarya
    59: {"url": "https://www.teo.org.tr/nobetci-eczaneler",                      "coords": True},  # Tekirdağ
    62: {"url": "https://www.elazigeczaciodasi.org.tr/nobetci-eczaneler/tunceli","coords": True},  # Tunceli
    63: {"url": "https://www.sanliurfaeo.org.tr/nobetci-eczaneler",             "coords": False},  # Şanlıurfa (geocode)
    69: {"url": "https://www.erzurumeo.org.tr/nobetci-eczaneler/69",             "coords": True},  # Bayburt
    75: {"url": "https://www.erzurumeo.org.tr/nobetci-eczaneler/75",             "coords": True},  # Ardahan
    76: {"url": "https://www.erzurumeo.org.tr/nobetci-eczaneler/76",             "coords": True},  # Iğdır
    78: {"url": "https://www.kastamonueo.org.tr/nobetci-eczaneler/78",           "coords": True},  # Karabük
    81: {"url": "https://www.duzceeo.org/nobetci-eczaneler",                     "coords": True},  # Düzce
}


def _norm(s: str) -> str:
    """Türkçe il/ilçe adını karşılaştırma için sadeleştir (büyük ASCII)."""
    if not s:
        return ""
    table = str.maketrans("çğıİiöşüÇĞıÖŞÜ", "cgiIiosuCGiOSU")
    s = s.translate(table)
    s = (s.replace("ı", "i").replace("İ", "I"))
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if digits.startswith("90") and len(digits) >= 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) >= 11:
        return "+9" + digits
    if len(digits) == 10:
        return "+90" + digits
    return digits


def _province_from_header(text: str) -> str | None:
    """'03 Haziran 2026 Van'da Bugün Nöbetçi Eczaneler' -> 'Van'."""
    text = " ".join(text.split())
    m = SECTION_RE.search(text)
    if not m:
        return None
    before = text[: m.start()].strip()
    before = DATE_PREFIX_RE.sub("", before).strip()
    if not before:
        return None
    word = before.split()[-1]
    # locative ekini at: Van'da -> Van, Hakkari'de -> Hakkari
    word = re.split(r"['’]", word)[0]
    return word or None


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "nobetcim-bot/1.0 (afk416@gmail.com)"
GEOCODE_DELAY = 1.1  # Nominatim kullanım politikası: ≤1 istek/sn
MAHALLE_RE = re.compile(r"([0-9A-Za-zÇĞİÖŞÜçğıöşü.\s]+?MAH(?:ALLES[İI])?)\.?\b", re.IGNORECASE)


def _geocode_queries(address: str, district: str, city: str) -> list[str]:
    """Kabadan inceye doğru denenecek geocode sorguları.

    Tam adres genelde gürültülü (parantezli tarifler vs.) ve Nominatim'de
    bulunamıyor; mahalle/ilçe seviyesi daha güvenilir sonuç verir.
    """
    qs: list[str] = []
    m = MAHALLE_RE.search(address or "")
    if m:
        mahalle = " ".join(m.group(1).split())
        qs.append(", ".join(x for x in [mahalle, district, city, "Türkiye"] if x))
    if district and _norm(district) not in ("MERKEZ",):
        qs.append(f"{district}, {city}, Türkiye")
    qs.append(f"{city} merkez, Türkiye")
    # tekrarları sırayı bozmadan ayıkla
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def geocode_pharmacies(pharmacies: list[dict], city: str) -> int:
    """Koordinatsız eczaneleri (lat/lng None) adresinden Nominatim ile geocode
    eder. Sonuçlar SQLite önbelleğine yazılır; sonraki turlarda tekrar
    istek atılmaz. Geocode edilen sayıyı döner.
    """
    import pharmacy_store as store

    filled = 0
    for p in pharmacies:
        if p.get("lat") is not None and p.get("lng") is not None:
            continue
        addr = (p.get("address") or "").strip()
        district = (p.get("district") or "").strip()
        if not addr and not district:
            continue

        key = _norm(addr + "|" + district + "|" + city)[:140]
        cached = store.geocode_get(key)
        if cached is not None:
            p["lat"], p["lng"] = cached
            filled += 1
            continue

        lat = lng = None
        for query in _geocode_queries(addr, district, city):
            try:
                r = requests.get(
                    NOMINATIM_URL,
                    params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "tr"},
                    headers={"User-Agent": NOMINATIM_UA, "Accept-Language": "tr"},
                    timeout=10,
                )
                r.raise_for_status()
                js = r.json()
                if js:
                    lat, lng = float(js[0]["lat"]), float(js[0]["lon"])
            except Exception:
                logger.warning("geocode hatası: %s", query[:60])
            time.sleep(GEOCODE_DELAY)
            if lat is not None:
                break

        store.geocode_put(key, lat, lng)
        if lat is not None:
            p["lat"], p["lng"] = lat, lng
            filled += 1
    return filled


@dataclass
class ScrapeResult:
    success: bool
    plate_code: str
    pharmacies: list[dict] = field(default_factory=list)
    took: float = 0.0


def _fetch(url: str) -> BeautifulSoup | None:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            html = r.content.decode(r.apparent_encoding or "utf-8", "replace")
            try:
                return BeautifulSoup(html, "lxml")
            except Exception:
                return BeautifulSoup(html, "html.parser")
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
    logger.warning("chamber fetch başarısız %s: %s", url, last_exc)
    return None


def _icon_text(container, *needles: str) -> str | None:
    """class'ında verilen ipuçlarından birini içeren <i>'nin yanındaki metin."""
    for i in container.find_all("i"):
        cls = " ".join(i.get("class") or [])
        if any(n in cls for n in needles):
            sib = i.next_sibling
            while sib is not None:
                txt = sib.get_text(strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if txt:
                    return txt
                sib = sib.next_sibling
    return None


def _container_for(anchor):
    """maps çapasından yukarı çıkıp eczane adını (ECZ) kapsayan en küçük bloğu bul.

    Telefon bazı şablonlarda link değil düz metin olduğu için ad (ECZ içeren
    başlık/strong) çapa olarak kullanılır.
    """
    node = anchor
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        for t in parent.find_all(["h3", "h4", "strong", "font"]):
            if "ECZ" in _norm(t.get_text(" ", strip=True)):
                return parent
        node = parent
    return anchor.parent


def _extract_name_district(container) -> tuple[str | None, str | None]:
    """Blok içinden eczane adı (ECZ içeren) ve ilçe çıkar."""
    name = None
    district = None
    # 1) ad: ECZ içeren ilk başlık/strong
    for tag in container.find_all(["h4", "h3", "strong", "font"]):
        small = tag.find("small")
        txt = tag.get_text(" ", strip=True)
        if small:
            district = district or small.get_text(strip=True)
            txt = txt.replace(small.get_text(" ", strip=True), "").strip()
        up = _norm(txt)
        if "ECZ" in up and len(txt) > 3:
            # bazı şablonlarda başlık "AD - İLÇE" şeklinde birleşik
            if " - " in txt:
                left, right = txt.split(" - ", 1)
                name = left.strip()
                district = district or right.strip()
            else:
                name = txt
            break
    # 2) ilçe: <small>, hand-right ikonu, ya da "İL - İLÇE" strong
    if not district:
        district = _icon_text(container, "hand-right")
    if not district:
        for tag in container.find_all(["strong", "p"]):
            t = tag.get_text(" ", strip=True)
            if "-" in t and "ECZ" not in _norm(t) and len(t) < 60:
                parts = [p.strip() for p in t.split("-", 1)]
                if len(parts) == 2 and parts[1]:
                    district = parts[1]
                    break
    return name, district


def _parse_coordless(soup, plate_code: str, started: float) -> ScrapeResult:
    """maps linki olmayan sayfalar için ad başlıklarından eczane çıkar."""
    seen: set[str] = set()
    pharmacies: list[dict] = []
    for tag in soup.find_all(["h3", "h4", "strong", "font"]):
        txt = tag.get_text(" ", strip=True)
        if "ECZANES" not in _norm(txt) or len(txt) < 4:
            continue
        if " - " in txt:
            txt = txt.split(" - ", 1)[0].strip()
        # adı kapsayan en küçük bloğa çık (adres/telefon için)
        container = tag
        for _ in range(4):
            if container.find("a", href=lambda h: h and h.startswith("tel:")) or \
               _icon_text(container, "fa-home", "icon-home"):
                break
            if container.parent is None:
                break
            container = container.parent
        address = _icon_text(container, "fa-home", "icon-home") or ""
        phone_a = container.find("a", href=lambda h: h and h.startswith("tel:"))
        if phone_a:
            phone = _normalize_phone(phone_a.get("href", ""))
        else:
            phone = _normalize_phone(_icon_text(container, "fa-phone", "icon-phone") or "")
        key = _norm(txt) + _norm(address)[:20]
        if key in seen:
            continue
        seen.add(key)
        pharmacies.append({
            "district": "",
            "name": txt,
            "address": address,
            "phone": phone,
            "lat": None,
            "lng": None,
        })
    return ScrapeResult(
        success=True,
        plate_code=plate_code,
        pharmacies=pharmacies,
        took=round(time.time() - started, 1),
    )


# Gaziantep Eczacı Odası (Eflatunweb) hem Gaziantep hem Kilis'i tek tabloda
# verir. Kilis ilçelerinin bölge (bolge) id'leri:
GAZIANTEP_EO_URL = "https://www.gaziantepeo.org.tr/nobetci-eczaneler"
KILIS_BOLGE_IDS = {9, 14, 20}  # MUSABEYLİ, ELBEYLİ, KİLİS MERKEZ
BOLGE_RE = re.compile(r"bolge=(\d+)")
PHONE_LABEL_RE = re.compile(r"Telefon\s*:?\s*(.+)$", re.IGNORECASE | re.DOTALL)


def scrape_gaziantep_eo(plate_code: str, want_kilis: bool) -> ScrapeResult:
    """Gaziantep Eczacı Odası tablosundan Gaziantep (27) veya Kilis (79) çeker.

    Tablo ilçe başlıklarıyla (bolge id) bölünür; want_kilis True ise yalnız
    Kilis ilçeleri, değilse yalnız Gaziantep ilçeleri döner. Sayfada koordinat
    yoktur; lat/lng None döner, scrape_all Nominatim ile doldurur.
    """
    started = time.time()
    soup = _fetch(GAZIANTEP_EO_URL)
    if soup is None:
        return ScrapeResult(False, plate_code, took=round(time.time() - started, 1))

    table = soup.find("table", class_=lambda c: c and "customTable" in c)
    tbody = table.find("tbody") if table else None
    if not tbody:
        return ScrapeResult(False, plate_code, took=round(time.time() - started, 1))

    pharmacies: list[dict] = []
    seen: set[str] = set()
    cur_is_kilis = False
    cur_district = ""
    for tr in tbody.find_all("tr"):
        bas = tr.find("td", class_="ilce-baslik")
        if bas:
            link = bas.find("a", href=BOLGE_RE)
            bolge = int(BOLGE_RE.search(link["href"]).group(1)) if link else -1
            cur_is_kilis = bolge in KILIS_BOLGE_IDS
            cur_district = bas.get_text(" ", strip=True).split("NÖBET")[0].strip()
            continue
        if cur_is_kilis != want_kilis:
            continue
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = tds[0].get_text(" ", strip=True)
        if "ECZANES" not in _norm(name):
            continue
        info = tds[1]
        b = info.find("b")
        district = b.get_text(strip=True) if b else cur_district
        # adres: <b>'den önceki metin, sonundaki " /" temizlenir
        full = info.get_text("\n", strip=True)
        phone = None
        mp = PHONE_LABEL_RE.search(full)
        if mp:
            phone = _normalize_phone(mp.group(1))
            full = full[: mp.start()].strip()
        addr = info.get_text(" ", strip=True)
        addr = re.split(r"Telefon", addr, flags=re.IGNORECASE)[0]
        if district:
            addr = re.sub(r"/\s*" + re.escape(district) + r".*$", "", addr).strip()
        addr = addr.rstrip("/ ").strip()
        key = _norm(name) + _norm(addr)[:20]
        if key in seen:
            continue
        seen.add(key)
        pharmacies.append({
            "district": district,
            "name": name,
            "address": addr,
            "phone": phone,
            "lat": None,
            "lng": None,
        })
    return ScrapeResult(
        success=True,
        plate_code=plate_code,
        pharmacies=pharmacies,
        took=round(time.time() - started, 1),
    )


def scrape_chamber(
    url: str,
    expected_province: str,
    plate_code: str,
    multi: bool = False,
) -> ScrapeResult:
    """Bir oda sayfasından, beklenen ile ait nöbetçi eczaneleri çeker.

    multi=True ise sayfa birden çok il içerir (Van=Van+Bitlis+Hakkari,
    Zonguldak=Zonguldak+Bartın); her eczane, en yakın önceki bölüm
    başlığındaki ile göre süzülür. multi=False ise sayfa tek illidir ve
    tüm eczaneler beklenen ile atanır (başlık formatları çok değişken
    olduğu için tek illi sayfalarda başlığa güvenilmez).
    """
    started = time.time()
    soup = _fetch(url)
    if soup is None:
        return ScrapeResult(False, plate_code, took=round(time.time() - started, 1))

    want = _norm(expected_province)
    anchors = soup.find_all("a", href=MAPS_RE)
    seen: set[tuple] = set()
    pharmacies: list[dict] = []

    # Koordinatsız sayfa (ör. Malatya): maps linki yok; ad başlıklarından
    # parse et, koordinatları sonra (scrape_all) Nominatim ile doldur.
    if not anchors:
        return _parse_coordless(soup, plate_code, started)

    for a in anchors:
        m = MAPS_RE.search(a.get("href", ""))
        if not m:
            continue
        lat, lng = float(m.group(1)), float(m.group(2))

        if multi:
            province = None
            for h in a.find_all_previous(["h2", "h3", "h4"]):
                province = _province_from_header(h.get_text(" ", strip=True))
                if province:
                    break
            if not province or _norm(province) != want:
                continue

        container = _container_for(a)
        name, district = _extract_name_district(container)
        if not name:
            continue
        phone_a = container.find("a", href=lambda h: h and h.startswith("tel:"))
        if phone_a:
            phone = _normalize_phone(phone_a.get("href", ""))
        else:
            phone = _normalize_phone(_icon_text(container, "fa-phone", "icon-phone") or "")
        address = _icon_text(container, "fa-home", "icon-home")

        key = (name, round(lat, 5), round(lng, 5))
        if key in seen:
            continue
        seen.add(key)
        pharmacies.append({
            "district": district or "",
            "name": name,
            "address": address or "",
            "phone": phone,
            "lat": lat,
            "lng": lng,
        })

    return ScrapeResult(
        success=True,
        plate_code=plate_code,
        pharmacies=pharmacies,
        took=round(time.time() - started, 1),
    )


if __name__ == "__main__":
    import json
    import sys

    from provinces import PLATE_CITY  # type: ignore

    logging.basicConfig(level=logging.INFO)
    plate = sys.argv[1] if len(sys.argv) > 1 else "15"
    url = sys.argv[2] if len(sys.argv) > 2 else CHAMBER_SOURCES[int(plate)]["url"]
    prov = PLATE_CITY.get(int(plate), "")
    res = scrape_chamber(url, prov, plate)
    print(f"plaka {plate} ({prov}): success={res.success} "
          f"count={len(res.pharmacies)} took={res.took}s")
    for p in res.pharmacies[:8]:
        print(json.dumps(p, ensure_ascii=False))
