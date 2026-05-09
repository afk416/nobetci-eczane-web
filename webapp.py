"""En yakın nöbetçi eczaneyi bulan web uygulaması."""
from flask import Flask, jsonify, render_template, request

from pharmacy_scraper import fetch_pharmacies, get_district_for_location

DISTRICT_DISPLAY = {
    "merkez": "Merkez (Onikişubat + Dulkadiroğlu)",
    "afsin": "Afşin",
    "andirin": "Andırın",
    "caglayancerit": "Çağlayancerit",
    "ekinozu": "Ekinözü",
    "elbistan": "Elbistan",
    "goksun": "Göksun",
    "narli": "Narlı",
    "nurhak": "Nurhak",
    "pazarcik": "Pazarcık",
    "turkoglu": "Türkoğlu",
}

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pharmacies")
def api_pharmacies():
    """lat/lng verilirse kullanıcının ilçesindeki eczaneleri döner.
    Verilmezse tüm liste (geriye dönük uyumluluk)."""
    try:
        all_data = fetch_pharmacies()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)

    if lat is None or lng is None:
        return jsonify(all_data)

    district = get_district_for_location(lat, lng)
    district_pharmacies = (
        [p for p in all_data if p.get("ilce_key") == district] if district else []
    )

    if district_pharmacies:
        return jsonify(
            {
                "pharmacies": district_pharmacies,
                "user_district": district,
                "user_district_display": DISTRICT_DISPLAY.get(district, district),
                "fallback": False,
            }
        )
    return jsonify(
        {
            "pharmacies": all_data,
            "user_district": district,
            "user_district_display": DISTRICT_DISPLAY.get(district) if district else None,
            "fallback": True,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
