"""Nobetcim — Türkiye geneli nöbetçi eczane Telegram botu."""
import logging
import math
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pharmacy_scraper import (
    fetch_pharmacies,
    get_location_info,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "BURAYA_TOKEN_YAPISTIR")
BUTTON_TEXT = "📍 Bana en yakın nöbetçi eczaneyi bul"
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://nobetcim.com.tr/")

# Bot başlangıcında ısıtılacak il (varsayılan: Maraş)
WARMUP_PROVINCE = os.environ.get("WARMUP_PROVINCE", "kahramanmaras")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def main_keyboard() -> ReplyKeyboardMarkup:
    button = KeyboardButton(text=BUTTON_TEXT, request_location=True)
    return ReplyKeyboardMarkup(
        [[button]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def il_display(il_key: str) -> str:
    """İl anahtarından kullanıcıya gösterilecek başlık."""
    if not il_key:
        return ""
    # Türkçe karakter restore: bazı sık iller için
    overrides = {
        "kahramanmaras": "Kahramanmaraş",
        "istanbul": "İstanbul",
        "izmir": "İzmir",
        "gaziantep": "Gaziantep",
        "sanliurfa": "Şanlıurfa",
        "diyarbakir": "Diyarbakır",
        "mugla": "Muğla",
        "balikesir": "Balıkesir",
        "agri": "Ağrı",
        "elazig": "Elazığ",
        "kutahya": "Kütahya",
        "afyonkarahisar": "Afyonkarahisar",
        "aydin": "Aydın",
        "canakkale": "Çanakkale",
        "cankiri": "Çankırı",
        "corum": "Çorum",
        "tekirdag": "Tekirdağ",
        "usak": "Uşak",
        "nigde": "Niğde",
        "mus": "Muş",
        "icel": "Mersin",
    }
    return overrides.get(il_key, il_key.title())


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"Merhaba {user.first_name}! 👋\n\n"
        "Ben *Nobetcim* — Türkiye'nin her ilindeki nöbetçi eczaneleri "
        "bulmana yardım ederim.\n\n"
        f"📱 *Telefondan:* Aşağıdaki *{BUTTON_TEXT}* butonuna bas, "
        "konumunu paylaş, bulunduğun ilçeye en yakın nöbetçi eczaneyi göstereyim.\n\n"
        f"💻 *Tarayıcıda açmak istersen:* [buraya tıkla]({WEB_APP_URL})"
    )
    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    if not loc:
        return

    user_lat, user_lng = loc.latitude, loc.longitude
    logger.info("Konum: %s, %s", user_lat, user_lng)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    # 1) Kullanıcının il/ilçesini bul
    info = get_location_info(user_lat, user_lng)
    if not info or not info.get("il"):
        await update.message.reply_text(
            "❌ Bulunduğun il tespit edilemedi. "
            "Türkiye dışındaysan veya konum izninle ilgili sorun olabilir.",
            reply_markup=main_keyboard(),
        )
        return

    il_key = info["il"]
    user_district = info.get("ilce_key")
    ilce_display = info.get("ilce_display") or ""
    logger.info("Tespit: il=%s ilce=%s", il_key, user_district)

    # 2) O ilin eczanelerini çek
    try:
        pharmacies = fetch_pharmacies(il_key)
    except Exception:
        logger.exception("Eczane verisi çekilemedi (il=%s)", il_key)
        await update.message.reply_text(
            "❌ Eczane listesi alınamadı. Lütfen az sonra tekrar dene.",
            reply_markup=main_keyboard(),
        )
        return

    all_geo = [p for p in pharmacies if p.get("lat") and p.get("lng")]
    if not all_geo:
        await update.message.reply_text(
            f"Bugün *{il_display(il_key)}* için koordinatlı nöbetçi eczane bulunamadı.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return

    # 3) İlçeye göre filtre, yoksa tüm il
    geo = [p for p in all_geo if p.get("ilce_key") == user_district] if user_district else []
    fallback_used = False
    if not geo:
        geo = all_geo
        fallback_used = True

    # 4) Mesafe hesabı ve sıralama
    for p in geo:
        p["_km"] = haversine_km(user_lat, user_lng, p["lat"], p["lng"])
    geo.sort(key=lambda x: x["_km"])
    nearest = geo[0]

    # 5) Fallback uyarısı
    if fallback_used and user_district:
        note = (
            f"_⚠️ *{ilce_display}* bölgesinde bugün nöbetçi eczane yok. "
            f"Tüm *{il_display(il_key)}* listesini gösteriyorum._\n\n"
        )
        await update.message.reply_text(
            note,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )

    # 6) Liste mesajı
    baslik_ilce = ilce_display if not fallback_used and user_district else il_display(il_key)
    liste_metni = f"*{baslik_ilce} — Nöbetçi Eczaneler (mesafeye göre):*\n\n"
    for i, p in enumerate(geo):
        ad_l = p["ad"]
        if p.get("ilce"):
            ad_l = f"{ad_l} ({p['ilce']})"
        tel_l = p.get("tel") or "-"
        en_yakin = " (🟢 EN YAKIN)" if i == 0 else ""
        liste_metni += (
            f"*{ad_l}*{en_yakin}\n"
            f"📏 {format_distance(p['_km'])}   🗺️ [harita]({p['harita']})\n"
            f"📞 {tel_l}\n\n"
        )
    await update.message.reply_text(
        liste_metni,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )

    # 7) En yakın detay
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
        "_Yol tarifi için aşağıdaki konum mesajına dokun._"
    )
    await update.message.reply_text(
        mesaj,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )

    # 8) Konum pini
    await update.message.reply_location(
        latitude=nearest["lat"],
        longitude=nearest["lng"],
        reply_markup=main_keyboard(),
    )


async def liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/liste KAHRAMANMARAS gibi parametre alır, parametresizde varsayılan il."""
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    args = context.args or []
    il = " ".join(args).strip() if args else WARMUP_PROVINCE
    if not il:
        il = WARMUP_PROVINCE

    try:
        pharmacies = fetch_pharmacies(il)
    except Exception:
        logger.exception("Eczane verisi çekilemedi (/liste il=%s)", il)
        await update.message.reply_text(
            "❌ Eczane listesi alınamadı. Lütfen az sonra tekrar dene "
            "veya il adını kontrol et. Örnek: `/liste istanbul`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return

    if not pharmacies:
        await update.message.reply_text(
            f"*{il_display(il)}* için bugün nöbetçi eczane bulunamadı.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        return

    # Mesaj uzunluğu sınırı (Telegram 4096 karakter) — chunk'a böl
    chunks = [f"🏥 *{il_display(il)} — Nöbetçi Eczaneler*\n\n"]
    for i, p in enumerate(pharmacies, 1):
        ad = p["ad"]
        if p.get("ilce"):
            ad = f"{ad} ({p['ilce']})"
        tel = p.get("tel") or "-"
        harita = p.get("harita") or ""
        harita_md = f"[🗺️ harita]({harita})" if harita else ""
        block = (
            f"*{i}. {ad}*\n"
            f"📍 {p.get('adres') or '-'}\n"
            f"📞 {tel}   {harita_md}\n\n"
        )
        if len(chunks[-1]) + len(block) > 3500:
            chunks.append("")
        chunks[-1] += block

    for chunk in chunks:
        if chunk.strip():
            await update.message.reply_text(
                chunk,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=main_keyboard(),
            )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    if text.strip() == BUTTON_TEXT:
        msg = (
            "💻 Masaüstü Telegram konum paylaşımını desteklemiyor.\n\n"
            "Aşağıdaki butondan siteyi açıp oradan konumunu paylaşabilirsin:"
        )
        inline = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="🌐 Web sayfasını aç", url=WEB_APP_URL)]]
        )
        await update.message.reply_text(
            msg, reply_markup=inline, parse_mode=ParseMode.MARKDOWN
        )
        return

    msg = (
        f"Konum paylaşmak için aşağıdaki *{BUTTON_TEXT}* butonuna bas "
        "(telefondan) ya da `/liste istanbul` gibi yaz."
    )
    await update.message.reply_text(
        msg, reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN
    )


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "BURAYA_TOKEN_YAPISTIR":
        raise SystemExit(
            "Hata: TELEGRAM_BOT_TOKEN ortam değişkeni ayarlı değil."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("liste", liste))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    try:
        fetch_pharmacies(WARMUP_PROVINCE)
        logger.info("Cache ısıtıldı (%s)", WARMUP_PROVINCE)
    except Exception:
        logger.warning("Cache ön-ısıtma başarısız")

    logger.info("Bot başlıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
