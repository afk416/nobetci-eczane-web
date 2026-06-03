"""İl/plaka eşlemeleri ve Türkçe metin normalizasyonu — ortak yardımcılar."""
from __future__ import annotations

# Plaka kodu -> il görünen adı
PLATE_CITY: dict[int, str] = {
    1: "Adana", 2: "Adıyaman", 3: "Afyonkarahisar", 4: "Ağrı", 5: "Amasya",
    6: "Ankara", 7: "Antalya", 8: "Artvin", 9: "Aydın", 10: "Balıkesir",
    11: "Bilecik", 12: "Bingöl", 13: "Bitlis", 14: "Bolu", 15: "Burdur",
    16: "Bursa", 17: "Çanakkale", 18: "Çankırı", 19: "Çorum", 20: "Denizli",
    21: "Diyarbakır", 22: "Edirne", 23: "Elazığ", 24: "Erzincan", 25: "Erzurum",
    26: "Eskişehir", 27: "Gaziantep", 28: "Giresun", 29: "Gümüşhane", 30: "Hakkari",
    31: "Hatay", 32: "Isparta", 33: "Mersin", 34: "İstanbul", 35: "İzmir",
    36: "Kars", 37: "Kastamonu", 38: "Kayseri", 39: "Kırklareli", 40: "Kırşehir",
    41: "Kocaeli", 42: "Konya", 43: "Kütahya", 44: "Malatya", 45: "Manisa",
    46: "Kahramanmaraş", 47: "Mardin", 48: "Muğla", 49: "Muş", 50: "Nevşehir",
    51: "Niğde", 52: "Ordu", 53: "Rize", 54: "Sakarya", 55: "Samsun",
    56: "Siirt", 57: "Sinop", 58: "Sivas", 59: "Tekirdağ", 60: "Tokat",
    61: "Trabzon", 62: "Tunceli", 63: "Şanlıurfa", 64: "Uşak", 65: "Van",
    66: "Yozgat", 67: "Zonguldak", 68: "Aksaray", 69: "Bayburt", 70: "Karaman",
    71: "Kırıkkale", 72: "Batman", 73: "Şırnak", 74: "Bartın", 75: "Ardahan",
    76: "Iğdır", 77: "Yalova", 78: "Karabük", 79: "Kilis", 80: "Osmaniye",
    81: "Düzce",
}

ALL_PLATE_CODES: list[int] = sorted(PLATE_CITY.keys())


def normalize_text(s: str) -> str:
    """'Onikişubat' → 'onikisubat'. Türkçe karakterleri önce ASCII'ye çevirir.

    Türkçe büyük 'İ' karakteri Python .lower() ile sorun çıkardığı için
    önce harf dönüşümü, sonra lower yapılır.
    """
    if not s:
        return ""
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


# Normalize edilmiş il adı -> plaka kodu
PLATE_CODES: dict[str, int] = {normalize_text(name): code for code, name in PLATE_CITY.items()}

# Eski/alternatif il adları
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

# Aynı ilçeyi tek anahtara indirger (Kahramanmaraş merkez ilçeleri)
DISTRICT_NORMALIZE = {
    "merkez": "merkez",
    "onikisubat": "merkez",
    "dulkadiroglu": "merkez",
}


def province_to_plate(il: str) -> int | None:
    """İl adından (normalize/alias toleranslı) plaka kodu döner."""
    norm = normalize_text(il)
    if norm in PLATE_CODES:
        return PLATE_CODES[norm]
    if norm in PROVINCE_ALIASES:
        return PLATE_CODES.get(PROVINCE_ALIASES[norm])
    return None


def district_key(name: str) -> str:
    """İlçe adından normalize anahtar. Onikişubat/Dulkadiroğlu → 'merkez'."""
    norm = normalize_text(name)
    return DISTRICT_NORMALIZE.get(norm, norm)


# Türkiye'deki 81 il (normalize anahtarlar) — reverse geocode eşleştirme için
TURKEY_PROVINCES = set(PLATE_CODES.keys()) | {"afyon"}
