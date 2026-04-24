"""En yakın nöbetçi eczaneyi bulan web uygulaması."""
from flask import Flask, jsonify, render_template

from pharmacy_scraper import fetch_pharmacies

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pharmacies")
def api_pharmacies():
    try:
        data = fetch_pharmacies()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
