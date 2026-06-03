"""Nobetcim — Türkiye geneli nöbetçi eczane web servisi."""
import logging
import os

from flask import Flask, jsonify, render_template, request

from pharmacy_scraper import fetch_pharmacies, get_location_info

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Sentry hata izleme (env'de SENTRY_DSN tanımlıysa aktif)
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            send_default_pii=False,
            traces_sample_rate=0.0,
            integrations=[FlaskIntegration()],
            environment=os.environ.get("ENVIRONMENT", "production"),
            release=os.environ.get("RELEASE", "nobetcim@1.0.0"),
        )
        logger.info("Sentry başlatıldı (webapp)")
    except Exception:
        logger.exception("Sentry init başarısız")

app = Flask(__name__)


@app.after_request
def add_no_cache(response):
    """API ve HTML — tarayıcı önbelleklemesin, taze veri her zaman."""
    if request.path.startswith("/api/") or request.path in ("/", "/gizlilik", "/kullanim"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/gizlilik")
def gizlilik():
    return render_template("gizlilik.html")


@app.route("/kullanim")
def kullanim():
    return render_template("kullanim.html")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/api/pharmacies")
def api_pharmacies():
    """En yakın nöbetçi eczaneleri döner.

    Parametre:
        lat, lng — kullanıcı konumu (verirse il + ilçe otomatik tespit)
        il — manuel il (lat/lng yoksa)
    """
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    il_param = (request.args.get("il") or "").strip()

    # 1) Konumdan il + ilçe tespit
    user_il = None
    user_district = None
    ilce_display = None
    if lat is not None and lng is not None:
        info = get_location_info(lat, lng)
        if info:
            user_il = info.get("il")
            user_district = info.get("ilce_key")
            ilce_display = info.get("ilce_display")

    # Manuel il parametresi varsa onu öncelikle kullan
    target_il = il_param or user_il
    if not target_il:
        return jsonify(
            {
                "error": "Konum tespit edilemedi. Türkiye sınırları içinde olduğundan emin ol.",
            }
        ), 400

    # 2) İl için eczane listesi
    try:
        all_data = fetch_pharmacies(target_il)
    except Exception as e:
        logger.exception("CollectAPI hatası (il=%s)", target_il)
        return jsonify({"error": f"Eczane verisi alınamadı: {e}"}), 500

    # 3) İlçe farketmeksizin ildeki tüm nöbetçi eczaneleri döner;
    # frontend kullanıcı konumuna göre 15 km yarıçapında süzüp sıralar.
    return jsonify(
        {
            "pharmacies": all_data,
            "user_province": target_il,
            "user_district": user_district,
            "user_district_display": ilce_display,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
