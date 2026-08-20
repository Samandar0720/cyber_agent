import os
import sys
import asyncio
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from dotenv import load_dotenv

# Groq va Gemini API bilan ishlashda kesh va moslik xatoliklarini oldini olish uchun
import litellm
litellm.drop_params = True

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Logging sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# .env faylidan o'zgaruvchilarni yuklash
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
MY_CHAT_ID = os.getenv("MY_CHAT_ID")

# Tizim o'zgaruvchilarini tekshirish
if not TELEGRAM_BOT_TOKEN:
    logging.error("❌ TELEGRAM_BOT_TOKEN aniqlanmadi! .env faylini tekshiring.")

# LLM model nomini tanlash (Groq yoki Gemini)
if GROQ_API_KEY and GROQ_API_KEY != "gsk_your_groq_api_key_here":
    logging.info("🧠 AI LLM Provayderi: Groq (openai/gpt-oss-20b)")
    os.environ["GROQ_API_KEY"] = GROQ_API_KEY
    LLM_MODEL = "groq/openai/gpt-oss-20b"
elif GEMINI_API_KEY:
    logging.info("🧠 AI LLM Provayderi: Google Gemini (gemini-2.0-flash)")
    os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
    LLM_MODEL = "gemini/gemini-2.0-flash"
else:
    logging.warning("⚠️ Diqqat: GROQ_API_KEY yoki GEMINI_API_KEY ko'rsatilmadi!")
    LLM_MODEL = None

if SERPER_API_KEY and SERPER_API_KEY != "your_serper_api_key_here":
    os.environ["SERPER_API_KEY"] = SERPER_API_KEY


def search_cyber_news():
    """Serper API orqali to'g'ridan-to'g'ri (LLM agent tool-loopisiz) qidiruv qilish.
    Bu CrewAI Agent'ning ichki 2 martalik LLM chaqiruvini (fikrlash + yakuniy javob)
    1 tagacha kamaytiradi, natijada Groq bepul tarifidagi TPM (daqiqalik token)
    limitidan chiqib ketmaydi."""
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": "cybersecurity vulnerability breach news today", "num": 8}
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("news", []) or data.get("organic", []):
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        link = item.get("link", "")
        if title and link:
            results.append(f"- SARLAVHA: {title}\n  QISQACHA: {snippet}\n  HAVOLA: {link}")
        if len(results) >= 6:
            break
    return "\n".join(results)


def run_health_server():
    """Render 'web service' portini tekshirishi uchun minimal HTTP server.
    Bot ishlashi buning bilan bog'liq emas — bu faqat Render'ning
    port-scan talabini qondirish uchun ishga tushiriladi."""
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"cyber-agent bot ishlab turibdi")

        def log_message(self, format, *args):
            pass  # HTTP so'rov loglarini o'chirish

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info(f"🌐 Health-check server {port}-portda ishga tushdi.")
    server.serve_forever()

async def send_cyber_news():
    """Serper orqali yangiliklarni qidiradi va bitta LLM chaqiruvi bilan
    Telegram posti shakliga keltirib yuboradi."""
    logging.info("🚀 [START] Kiberxavfsizlik yangiliklarini yig'ish va tahlil qilish boshlandi...")
    try:
        if not TELEGRAM_BOT_TOKEN:
            logging.error("❌ TELEGRAM_BOT_TOKEN topilmadi! .env faylini tekshiring.")
            return

        target_chat_id = MY_CHAT_ID
        if not target_chat_id:
            logging.warning("⚠️ MY_CHAT_ID belgilanmagan. .env fayliga MY_CHAT_ID va serper_api_key qo'shing.")
            return

        if not LLM_MODEL:
            logging.error("❌ LLM modeli sozlanmagan (GROQ_API_KEY yoki GEMINI_API_KEY yo'q).")
            return

        # 1. Qidiruv (LLM chaqiruvisiz, oddiy HTTP so'rov)
        news_raw = await asyncio.to_thread(search_cyber_news)
        if not news_raw:
            logging.warning("⚠️ Serper qidiruvi natija bermadi.")
            return

        # 2. Yakuniy Telegram postini tuzish uchun BITTA marta LLM'ga murojaat
        prompt = (
            "Quyida bugungi kiberxavfsizlik yangiliklari haqida xom qidiruv natijalari berilgan.\n\n"
            f"{news_raw}\n\n"
            "Shulardan eng muhim va dolzarb 2 tasini tanlab, quyidagi qat'iy talablarga rioya qilgan holda "
            "Telegram posti tayyorla:\n"
            "1. Barcha matn FAQAT O'ZBEK TILIDA bo'lsin.\n"
            "2. Har bir yangilik aynan quyidagi qisqa strukturada bo'lsin:\n\n"
            "📌 [Mavzu nomi]\n"
            "• MOHIYATI: [1 gap]\n"
            "• XAVF DARAJASI: [Kritik / Yuqori / O'rta]\n"
            "• TAVSIYA: [1 ta qisqa maslahat]\n"
            "🔗 MANBA: [asl HAVOLA]\n\n"
            "3. Telegram uchun mos emojilardan foydalan.\n"
            "4. Har bir yangilik orasiga '-----------------------------------' qo'y.\n"
            "5. Kirish, salomlashish yoki xulosa matni yozma — faqat yangiliklar posti."
        )

        response = await asyncio.to_thread(
            litellm.completion,
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1200,
            num_retries=3,
        )
        report_text = response["choices"][0]["message"]["content"].strip()

        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        # Telegram xabari hajmi (max 4096 belgi) oshib ketsa bo'lib yuborish
        if len(report_text) > 4000:
            chunks = [report_text[i:i+4000] for i in range(0, len(report_text), 4000)]
            for chunk in chunks:
                await bot.send_message(chat_id=target_chat_id, text=chunk, disable_web_page_preview=False)
        else:
            await bot.send_message(chat_id=target_chat_id, text=report_text, disable_web_page_preview=False)

        await bot.session.close()
        logging.info("✅ [SUCCESS] Kiber-yangiliklar Telegram'ga muvaffaqiyatli yuborildi!")
    except Exception as e:
        logging.error(f"❌ [ERROR] Yangiliklarni yuborishda xatolik yuz berdi: {e}", exc_info=True)

async def main():
    # 0. Render port-scan talabini qondirish uchun health-check serverni fon oqimida ishga tushirish
    threading.Thread(target=run_health_server, daemon=True).start()

    # 1. Kod ishga tushganda darhol 1 marta yangilik yuborish
    logging.info("⚡ Bot ishga tushdi. Birinchi martalik yangilik yuborish jarayoni boshlandi...")
    await send_cyber_news()

    # 2. Har kuni ertalab soat 08:00 da avtomatik ishga tushish rejimi (AsyncIOScheduler)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_cyber_news, 'cron', hour=8, minute=0)
    scheduler.start()
    logging.info("⏰ APScheduler ishga tushdi. Har kuni ertalab soat 08:00 da avtomatik yangiliklar yuboriladi.")

    # Fonda doimiy 24/7 ishlash rejimi
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
