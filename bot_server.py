"""Telegram botu + Render için minimum HTTP sağlık kontrolü."""
import logging
import os
import threading

from flask import Flask
from waitress import serve

from bot import main as run_bot

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

http = Flask(__name__)


@http.route("/")
def root():
    return "OK - Telegram bot is running", 200


@http.route("/health")
def health():
    return {"status": "ok"}, 200


def run_http_server():
    port = int(os.environ.get("PORT", "10000"))
    logger.info("HTTP sağlık sunucusu portu: %s", port)
    serve(http, host="0.0.0.0", port=port)


if __name__ == "__main__":
    threading.Thread(target=run_http_server, daemon=True).start()
    run_bot()
