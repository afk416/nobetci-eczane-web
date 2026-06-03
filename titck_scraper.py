"""TİTCK (e-Devlet) nöbetçi eczane scraper'ı.

Resmi kaynak: https://www.turkiye.gov.tr/saglik-titck-nobetci-eczane-sorgulama

Tek il (plaka kodu) için nöbetçi eczaneleri çeker. e-Devlet rate-limit
uyguladığı için istekler tek oturumda, sıralı ve retry/backoff ile yapılır.
Bu modül DOĞRUDAN kullanıcı isteğinde değil, arka plandaki scrape_all.py
toplu işinde kullanılmak üzere tasarlanmıştır.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.turkiye.gov.tr/saglik-titck-nobetci-eczane-sorgulama"
SUBMIT_URL = BASE_URL + "?submit"
RESULTS_URL = BASE_URL + "?nobetci=Eczaneler"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
    "Connection": "keep-alive",
    "Accept-Encoding": "gzip, deflate",
}

LAT_RE = re.compile(r"var latti = parseFloat\(([\d\.]+)\);")
LON_RE = re.compile(r"var longi = parseFloat\(([\d\.]+)\);")

REQUEST_TIMEOUT = 20
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0          # saniye, denemeyle çarpılır
COORD_DELAY = 0.45           # her koordinat isteği arası nazik gecikme
COORD_MAX_RETRIES = 3
EMPTY_RETRIES = 3            # boş sonuç dönerse token tazeleyip yeniden dene
EMPTY_BACKOFF = 5.0         # boş sonuç denemeleri arası bekleme (throttle penceresi geçsin)


def _soup(content: bytes | str) -> BeautifulSoup:
    try:
        return BeautifulSoup(content, "lxml")
    except Exception:
        return BeautifulSoup(content, "html.parser")


def _normalize_phone(raw: str) -> str | None:
    """'0 - (344) 511 - 9116' → '+903445119116'."""
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


@dataclass
class ScrapeResult:
    success: bool
    plate_code: str
    duty_date: str | None = None
    pharmacies: list[dict] = field(default_factory=list)
    took: float = 0.0


class TitckSession:
    """e-Devlet ile tek bir requests.Session paylaşan istemci."""

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers.update(HEADERS)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._s.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
        raise last_exc if last_exc else requests.RequestException("istek başarısız")

    def fetch_context(self) -> tuple[str | None, list[str]]:
        """Sayfayı yükleyip token ve mevcut nöbet tarihlerini döner."""
        resp = self._request("GET", BASE_URL)
        doc = _soup(resp.content)
        token = doc.body.get("data-token") if doc.body else None
        dates = [
            el.get("value")
            for el in doc.find_all("input", {"name": "nobetTarihi", "type": "radio"})
            if el.get("value")
        ]
        return token, dates

    def scrape_province(self, plate_code: str, prefer_date: str | None = None) -> ScrapeResult:
        """Tek il için nöbetçi eczaneleri (koordinatlarıyla) çeker.

        Args:
            plate_code: '1'..'81'
            prefer_date: 'dd/mm/YYYY' — istenen nöbet günü. Mevcut değilse
                         site listesindeki ilk tarih kullanılır.
        """
        started = time.time()
        date_str = None
        rows: list = []
        # e-Devlet hızlı/ardışık isteklerde token/submit'i bazen reddedip boş
        # forma döndürüyor (geçici 0 sonuç). Boş dönerse token'ı tazeleyip
        # birkaç kez yeniden deneriz; gerçekten boş iller yine boş döner.
        for attempt in range(EMPTY_RETRIES):
            token, dates = self.fetch_context()
            if not token or not dates:
                logger.warning("plaka %s: token/tarih alınamadı", plate_code)
                if attempt < EMPTY_RETRIES - 1:
                    time.sleep(EMPTY_BACKOFF)
                    continue
                return ScrapeResult(success=False, plate_code=plate_code, took=time.time() - started)

            date_str = prefer_date if (prefer_date and prefer_date in dates) else dates[0]
            payload = {
                "ilkod": plate_code,
                "ilkod-address-il": plate_code,
                "ilkod-address-ilce": "",
                "nobetTarihi": date_str,
                "token": token,
                "btn": "Sorgula",
            }
            # POST submit doğrudan sonuç sayfasına yönlendiriyor; yine de
            # sonucu ayrı GET ile de teyit ediyoruz.
            resp = self._request("POST", SUBMIT_URL, data=payload)
            rows = self._extract_rows(resp.content)
            if not rows:
                resp = self._request("GET", RESULTS_URL)
                rows = self._extract_rows(resp.content)

            if rows:
                break
            if attempt < EMPTY_RETRIES - 1:
                logger.info("plaka %s: boş sonuç, yeniden deneniyor (%d)", plate_code, attempt + 1)
                time.sleep(EMPTY_BACKOFF)

        pharmacies = self._parse_rows(rows)
        self._fill_coordinates(pharmacies)

        return ScrapeResult(
            success=True,
            plate_code=plate_code,
            duty_date=date_str,
            pharmacies=pharmacies,
            took=round(time.time() - started, 1),
        )

    @staticmethod
    def _extract_rows(content: bytes) -> list:
        doc = _soup(content)
        table = doc.find("table", {"id": "searchTable"})
        if not table:
            return []
        thead = table.find("thead")
        header_ids = {id(tr) for tr in (thead.find_all("tr") if thead else [])}
        return [
            tr for tr in table.find_all("tr")
            if id(tr) not in header_ids and tr.find("td")
        ]

    @staticmethod
    def _parse_rows(rows: list) -> list[dict]:
        out: list[dict] = []
        for idx, tr in enumerate(rows):
            cols = tr.find_all("td", recursive=False)
            if len(cols) < 4:
                continue
            phone_raw = "".join(cols[3].find_all(string=True, recursive=False)).strip()
            name = cols[1].get_text(strip=True)
            if not name:
                continue
            out.append({
                "district": cols[0].get_text(strip=True),
                "name": name,
                "address": cols[2].get_text(strip=True),
                "phone": _normalize_phone(phone_raw),
                "lat": None,
                "lng": None,
                "_index": idx,
            })
        return out

    def _fetch_coord(self, index: int) -> tuple[float | None, float | None]:
        url = f"{BASE_URL}?harita=Goster&index={index}"
        for attempt in range(COORD_MAX_RETRIES):
            try:
                resp = self._request("POST", url, data={"harita": "Goster", "index": str(index)})
                text = resp.text
                lat = LAT_RE.search(text)
                lon = LON_RE.search(text)
                if lat and lon:
                    return float(lat.group(1)), float(lon.group(1))
                return None, None
            except requests.RequestException:
                if attempt < COORD_MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
        return None, None

    def _fill_coordinates(self, pharmacies: list[dict]) -> None:
        for p in pharmacies:
            lat, lng = self._fetch_coord(p["_index"])
            p["lat"] = lat
            p["lng"] = lng
            if COORD_DELAY:
                time.sleep(COORD_DELAY)
        for p in pharmacies:
            p.pop("_index", None)


def scrape_province(plate_code: str, prefer_date: str | None = None) -> ScrapeResult:
    """Tek kullanımlık oturumla tek il scrape eder."""
    return TitckSession().scrape_province(plate_code, prefer_date)


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    plate = sys.argv[1] if len(sys.argv) > 1 else "46"
    res = scrape_province(plate)
    print(f"plaka {plate}: success={res.success} date={res.duty_date} "
          f"count={len(res.pharmacies)} took={res.took}s")
    for p in res.pharmacies[:5]:
        print(json.dumps(p, ensure_ascii=False))
