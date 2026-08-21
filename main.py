import os
import sys
import json
import asyncio
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from dotenv import load_dotenv

import litellm
litellm.drop_params = True

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

# ==========================================================================
# SOZLAMALAR (chat ro'yxati va yuborish vaqtlari) — JSON faylda saqlanadi.
# ESLATMA: bu fayl Render'ning vaqtinchalik disk fazosida turadi — bot
# qayta ishga tushganda (restart) saqlanadi, lekin yangi kod deploy
# qilinganda (git push) qayta o'rnatiladi (default holatga qaytadi).
# ==========================================================================
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.json")
DEFAULT_CONFIG = {
    "chat_ids": [MY_CHAT_ID] if MY_CHAT_ID else [],
    "post_times": ["08:00"],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                cfg.setdefault("chat_ids", DEFAULT_CONFIG["chat_ids"])
                cfg.setdefault("post_times", DEFAULT_CONFIG["post_times"])
                return cfg
        except Exception as e:
            logging.error(f"⚠️ Config faylini o'qishda xatolik: {e}")
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"⚠️ Config faylini saqlashda xatolik: {e}")


config = load_config()

bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
dp = Dispatcher()
scheduler = AsyncIOScheduler()


def search_cyber_news():
    """Serper API orqali bir nechta qidiruv so'rovi bilan kengroq, xilma-xil
    yangiliklar to'plamini yig'adi (LLM chaqiruvisiz — token sarflamaydi)."""
    if not SERPER_API_KEY:
        return ""

    queries = [
        "cybersecurity breach hack news today",
        "new malware ransomware attack this week",
        "critical vulnerability exploit patch news",
    ]
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    seen_links = set()
    results = []
    for q in queries:
        try:
            resp = requests.post(url, headers=headers, json={"q": q, "num": 6}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning(f"⚠️ Serper so'rovida xatolik ('{q}'): {e}")
            continue

        for item in data.get("news", []) or data.get("organic", []):
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            if title and link and link not in seen_links:
                seen_links.add(link)
                results.append(f"- SARLAVHA: {title}\n  QISQACHA: {snippet}\n  HAVOLA: {link}")

    return "\n".join(results[:14])


def run_health_server():
    """Render 'web service' portini tekshirishi uchun minimal HTTP server."""
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"cyber-agent bot ishlab turibdi")

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info(f"🌐 Health-check server {port}-portda ishga tushdi.")
    server.serve_forever()


async def generate_news_post() -> str | None:
    """Yangiliklarni qidiradi va BITTA LLM chaqiruvi bilan tayyor Telegram
    postini qaytaradi (yoki hech narsa topilmasa None)."""
    if not LLM_MODEL:
        logging.error("❌ LLM modeli sozlanmagan (GROQ_API_KEY yoki GEMINI_API_KEY yo'q).")
        return None

    news_raw = await asyncio.to_thread(search_cyber_news)
    if not news_raw:
        logging.warning("⚠️ Serper qidiruvi natija bermadi.")
        return None

    prompt = (
        "Sen tajribali kiberxavfsizlik jurnalisti va SMM-mutaxassisisan. Quyida bugungi "
        "kiberxavfsizlik mavzusidagi xom qidiruv natijalari berilgan:\n\n"
        f"{news_raw}\n\n"
        "VAZIFA:\n"
        "Shulardan ODDIY FOYDALANUVCHI (texnik bo'lmagan odam) uchun ham QIZIQARLI va "
        "AMALIY AHAMIYATGA EGA bo'lgan eng muhim 3 tasini tanla. Kunlik oddiy CVE "
        "yamoqlari yoki juda tor texnik yangiliklardan ko'ra, ko'proq odamga tegishli "
        "bo'lgan (masalan: mashhur xizmat/ilova buzilishi, keng tarqalgan firibgarlik "
        "sxemasi, mashhur qurilma/dastur zaifligi) yangiliklarni ustuvor qil.\n\n"
        "TALABLAR:\n"
        "1. Barcha matn FAQAT O'ZBEK TILIDA, ODDIY VA RAVON tilda yozilsin — texnik "
        "atamalarni iloji boricha oddiy so'zlar bilan tushuntir, xuddi do'stingga "
        "aytayotgandek. Qisqartma yoki chet so'z ishlatsang, qavs ichida 1-2 so'z bilan "
        "izohla.\n"
        "2. Har bir yangilik aynan quyidagi strukturada bo'lsin:\n\n"
        "📌 [Qiziqarli, tushunarli sarlavha]\n"
        "• NIMA BO'LDI: [2 gapda, oddiy tilda tushuntirish]\n"
        "• NEGA MUHIM: [Bu oddiy odamga qanday ta'sir qilishi mumkinligi — 1 gap]\n"
        "• XAVF DARAJASI: [Kritik / Yuqori / O'rta]\n"
        "• NIMA QILISH KERAK: [1 ta aniq, amaliy maslahat]\n"
        "🔗 MANBA: [asl HAVOLA]\n\n"
        "3. Telegram uchun mos emojilardan foydalan.\n"
        "4. Har bir yangilik orasiga '-----------------------------------' qo'y.\n"
        "5. Kirish, salomlashish yoki xulosa matni yozma — faqat yangiliklar posti."
    )

    response = await asyncio.to_thread(
        litellm.completion,
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=1500,
        num_retries=3,
    )
    return response["choices"][0]["message"]["content"].strip()


async def send_cyber_news():
    """Yangilikni tayyorlaydi va ro'yxatdagi BARCHA chatlarga (shaxsiy, guruh,
    kanal) yuboradi."""
    logging.info("🚀 [START] Kiberxavfsizlik yangiliklarini yig'ish va tahlil qilish boshlandi...")
    try:
        if not bot:
            logging.error("❌ TELEGRAM_BOT_TOKEN topilmadi!")
            return

        chat_ids = config.get("chat_ids", [])
        if not chat_ids:
            logging.warning("⚠️ Hech qanday chat ro'yxatga olinmagan. /qoshish buyrug'idan foydalaning.")
            return

        report_text = await generate_news_post()
        if not report_text:
            logging.warning("⚠️ Yangilik matni tayyorlanmadi (qidiruv yoki LLM natija bermadi).")
            return

        chunks = [report_text[i:i+4000] for i in range(0, len(report_text), 4000)] or [report_text]

        for chat_id in chat_ids:
            try:
                for chunk in chunks:
                    await bot.send_message(chat_id=chat_id, text=chunk, disable_web_page_preview=False)
            except Exception as e:
                logging.error(f"❌ Chat {chat_id}ga yuborishda xatolik: {e}")

        logging.info(f"✅ [SUCCESS] Yangiliklar {len(chat_ids)} ta chatga yuborildi!")
    except Exception as e:
        logging.error(f"❌ [ERROR] Yangiliklarni yuborishda xatolik yuz berdi: {e}", exc_info=True)


def reschedule_jobs():
    """post_times ro'yxati asosida scheduler'dagi barcha 'post_job_*'
    vazifalarini qayta o'rnatadi."""
    for job in scheduler.get_jobs():
        if job.id.startswith("post_job_"):
            scheduler.remove_job(job.id)

    for i, t in enumerate(config.get("post_times", [])):
        try:
            hour, minute = t.split(":")
            scheduler.add_job(
                send_cyber_news, CronTrigger(hour=int(hour), minute=int(minute)),
                id=f"post_job_{i}", replace_existing=True
            )
        except Exception as e:
            logging.error(f"⚠️ '{t}' vaqtini rejalashtirishda xatolik: {e}")

    logging.info(f"⏰ Rejalashtirilgan vaqtlar: {', '.join(config.get('post_times', [])) or 'yo`q'}")


# ==========================================================================
# TELEGRAM BUYRUQLARI
# ==========================================================================

HELP_TEXT = (
    "🤖 <b>Kiberxavfsizlik yangiliklar boti</b>\n\n"
    "Buyruqlar:\n"
    "• /hozir — hoziroq yangiliklarni yuborish\n"
    "• /vaqt 08:00 14:00 20:00 — yuborish vaqtlarini belgilash (bir nechtasi bo'lishi mumkin)\n"
    "• /qoshish — shu guruh/kanalni ro'yxatga qo'shish (guruhda yozing, kanal uchun "
    "kanaldan biror xabarni menga forward qiling)\n"
    "• /ochirish <chat_id> — ro'yxatdan chatni olib tashlash\n"
    "• /royxat — joriy sozlamalarni ko'rish\n\n"
    "Eslatma: kanalga yuborish uchun meni o'sha kanalga <b>admin</b> qilib qo'shishingiz kerak."
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("hozir"))
async def cmd_now(message: Message):
    await message.answer("⏳ Yangiliklar tayyorlanmoqda, biroz kuting...")
    await send_cyber_news()
    await message.answer("✅ Yuborildi (agar xatolik bo'lmasa).")


@dp.message(Command("vaqt"))
async def cmd_settime(message: Message):
    parts = message.text.split()[1:]
    if not parts:
        await message.answer(
            "Vaqt(lar)ni kiriting. Masalan:\n<code>/vaqt 08:00 14:00 20:00</code>",
            parse_mode="HTML"
        )
        return

    valid_times = []
    for p in parts:
        try:
            h, m = p.split(":")
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid_times.append(f"{h:02d}:{m:02d}")
        except Exception:
            pass

    if not valid_times:
        await message.answer("❌ To'g'ri formatda vaqt kiritilmadi. Masalan: 08:00")
        return

    config["post_times"] = valid_times
    save_config(config)
    reschedule_jobs()
    await message.answer(f"✅ Yangi yuborish vaqtlari: {', '.join(valid_times)}")


@dp.message(Command("qoshish"))
async def cmd_add_chat(message: Message):
    if message.chat.type in ("group", "supergroup"):
        chat_id = message.chat.id
    elif message.forward_from_chat:
        chat_id = message.forward_from_chat.id
    else:
        await message.answer(
            "Guruhga qo'shish uchun shu buyruqni guruh ichida yozing.\n"
            "Kanalga qo'shish uchun kanaldan biror xabarni menga shu yerda forward qiling."
        )
        return

    chat_ids = config.setdefault("chat_ids", [])
    if chat_id in chat_ids:
        await message.answer("ℹ️ Bu chat allaqachon ro'yxatda.")
        return

    chat_ids.append(chat_id)
    save_config(config)
    await message.answer(f"✅ Chat qo'shildi (ID: <code>{chat_id}</code>)", parse_mode="HTML")


@dp.message(F.forward_from_chat)
async def handle_forward(message: Message):
    chat_id = message.forward_from_chat.id
    chat_ids = config.setdefault("chat_ids", [])
    if chat_id in chat_ids:
        await message.answer("ℹ️ Bu kanal/guruh allaqachon ro'yxatda.")
        return
    chat_ids.append(chat_id)
    save_config(config)
    await message.answer(
        f"✅ '{message.forward_from_chat.title or chat_id}' ro'yxatga qo'shildi "
        f"(ID: <code>{chat_id}</code>).\n\n⚠️ Bot o'sha kanalda <b>admin</b> ekanligiga ishonch hosil qiling.",
        parse_mode="HTML"
    )


@dp.message(Command("ochirish"))
async def cmd_remove_chat(message: Message):
    parts = message.text.split()[1:]
    if not parts:
        await message.answer("Chat ID kiriting. Masalan: <code>/ochirish -1001234567890</code>", parse_mode="HTML")
        return
    try:
        chat_id = int(parts[0])
    except ValueError:
        await message.answer("❌ Chat ID raqam bo'lishi kerak.")
        return

    chat_ids = config.setdefault("chat_ids", [])
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_config(config)
        await message.answer(f"✅ Chat o'chirildi (ID: {chat_id})")
    else:
        await message.answer("❌ Bu chat ro'yxatda topilmadi.")


@dp.message(Command("royxat"))
async def cmd_list(message: Message):
    chat_ids = config.get("chat_ids", [])
    post_times = config.get("post_times", [])
    text = (
        f"📋 <b>Joriy sozlamalar:</b>\n\n"
        f"🕐 Yuborish vaqtlari: {', '.join(post_times) if post_times else 'belgilanmagan'}\n"
        f"💬 Chatlar soni: {len(chat_ids)}\n"
    )
    if chat_ids:
        text += "\n".join(f"  • <code>{cid}</code>" for cid in chat_ids)
    await message.answer(text, parse_mode="HTML")


async def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    logging.info("⚡ Bot ishga tushdi. Birinchi martalik yangilik yuborish jarayoni boshlandi...")
    await send_cyber_news()

    reschedule_jobs()
    scheduler.start()

    logging.info("🤖 Telegram buyruqlarini tinglash (polling) boshlandi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
