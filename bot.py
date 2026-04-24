"""Kahramanmaraş nöbetçi eczane Telegram botu."""
import logging
import math
import os

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pharmacy_scraper import fetch_pharmacies

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "BURAYA_TOKEN_YAPISTIR")

BUTTON_TEXT = "📍 Bana en yakın nöbetçi eczaneyi bul"


def main_keyboard() -> ReplyKeyboardMarkup:
    button = KeyboardButton(text=BUTTON_TEXT, request_location=True)
    return ReplyKeyboardMarkup(
        [[button]], resize_keyboard=True, one_time_keyboard=False
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki koordinat arası mesafe (km)."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def format_distance(km: float) -> str:
    if km < 1:
        return f"{int(round(km * 1000))} m"
    return f"{km:.2f} km"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"Merhaba {user.first_name}! 👋\n\n"
        "Ben Kahramanmaraş Nöbetçi Eczane botuyum.\n\n"
        f"Aşağıdaki *{BUTTON_TEXT}* butonuna bas, "
        "konumunu paylaş, sana en yakın nöbetçi eczaneyi adres "
        "ve telefonuyla göstereyim.\n\n"
        "📋 Tüm nöbetçi eczaneleri görmek için /liste yazabilirsin."
    )
    await update.message.reply_text(
        text, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN
    )


async def liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        pharmacies = fetch_pharmacies()
    except Exception:
        logger.exception("Eczane verisi çekilemedi")
        await update.message.reply_text(
            "❌ Eczane listesi alınamadı. Lütfen az sonra tekrar dene.",
            reply_markup=main_keyboard(),
        )
        return

    if not pharmacies:
        await update.message.reply_text(
            "Bugün için nöbetçi eczane bulunamadı.",
            reply_markup=main_keyboard(),
        )
        return

    satirlar = ["🏥 *Bugünkü Nöbetçi Eczaneler*\n"]
    for i, p in enumerate(pharmacies, 1):
        ad = p["ad"]
        if p.get("ilce"):
            ad = f"{ad} ({p['ilce']})"
        tel = p.get("tel") or "-"
        harita = p.get("harita") or ""
        harita_md = f"[🗺️ harita]({harita})" if harita else ""
        satirlar.append(
            f"*{i}. {ad}*\n"
            f"📍 {p.get('adres') or '-'}\n"
            f"📞 {tel}\n"
            f"{harita_md}\n"
        )

    await update.message.reply_text(
        "\n".join(satirlar),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    if not loc:
        return

    user_lat, user_lng = loc.latitude, loc.longitude
    logger.info("Konum: %s, %s", user_lat, user_lng)

    try:
        pharmacies = fetch_pharmacies()
    except Exception as e:
        logger.exception("Eczane verisi çekilemedi")
        await update.message.reply_text(
            "❌ Eczane listesi alınamadı. Lütfen az sonra tekrar dene.",
            reply_markup=main_keyboard(),
        )
        return

    geo = [p for p in pharmacies if p.get("lat") and p.get("lng")]
    if not geo:
        await update.message.reply_text(
            "Bugün için koordinatlı nöbetçi eczane bulunamadı.",
            reply_markup=main_keyboard(),
        )
        return

    for p in geo:
        p["_km"] = haversine_km(user_lat, user_lng, p["lat"], p["lng"])

    geo.sort(key=lambda x: x["_km"])
    nearest = geo[0]

    ad = nearest["ad"]
    if nearest.get("ilce"):
        ad = f"{ad} ({nearest['ilce']})"

    tel = nearest.get("tel") or "-"

    mesaj = (
        f"🏥 *En Yakın Nöbetçi Eczane*\n\n"
        f"*{ad}*\n"
        f"📏 Mesafe: *{format_distance(nearest['_km'])}*\n"
        f"📍 Adres: {nearest.get('adres') or '-'}\n"
        f"📞 Telefon: {tel}\n"
        f"🗺️ [Google Maps'te Aç]({nearest['harita']})\n\n"
        "_Telefon numarasına dokununca arama ekranı açılır._\n"
        "_Yol tarifi için aşağıdaki konum mesajına dokun._"
    )

    await update.message.reply_text(
        mesaj,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )

    await update.message.reply_location(
        latitude=nearest["lat"],
        longitude=nearest["lng"],
        reply_markup=main_keyboard(),
    )

    if len(geo) > 1:
        diger = "*Diğer nöbetçi eczaneler (mesafeye göre):*\n\n"
        for p in geo[1:]:
            ad2 = p["ad"]
            if p.get("ilce"):
                ad2 = f"{ad2} ({p['ilce']})"
            tel2 = p.get("tel") or "-"
            diger += (
                f"*{ad2}*\n"
                f"📏 {format_distance(p['_km'])}   🗺️ [harita]({p['harita']})\n"
                f"📞 {tel2}\n\n"
            )
        await update.message.reply_text(
            diger,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=main_keyboard(),
        )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    if text.strip() == BUTTON_TEXT:
        msg = (
            "💻 Masaüstü Telegram konum butonunu desteklemiyor.\n\n"
            "• 📱 Telefondan aynı butona basınca çalışır.\n"
            "• Bilgisayardaysan 📎 *ataç* → *Konum* ile konumunu gönder.\n"
            "• Veya /liste yaz, tüm nöbetçi eczaneleri göstereyim."
        )
    else:
        msg = (
            f"Konum paylaşmak için aşağıdaki *{BUTTON_TEXT}* butonuna bas "
            "(telefondan) ya da /liste yaz."
        )
    await update.message.reply_text(
        msg, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN
    )


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "BURAYA_TOKEN_YAPISTIR":
        raise SystemExit(
            "Hata: TELEGRAM_BOT_TOKEN ortam değişkeni ayarlı değil. "
            "Önce BotFather'dan token al ve şu şekilde çalıştır:\n"
            '  set TELEGRAM_BOT_TOKEN=123456:ABC...\n'
            "  python bot.py"
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("liste", liste))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    logger.info("Bot başlıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
