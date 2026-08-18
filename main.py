import os
import sys
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from dotenv import load_dotenv

# Groq va Gemini API bilan ishlashda kesh va moslik xatoliklarini oldini olish uchun
import litellm
litellm.drop_params = True

# CrewAI'ning ma'lum bug'i uchun vaqtinchalik yechim (GitHub Issue #5886):
# CrewAI 1.14.4+ versiyalarida xabarlarga 'cache_breakpoint' maydoni qo'shiladi,
# lekin bu faqat Anthropic uchun olib tashlanadi — Groq kabi boshqa provayderlar
# buni qo'llab-quvvatlamaydi va BadRequestError beradi. Shu sababli bu maydonni
# hech qachon qo'shmaslikni majburlaymiz.
import crewai.llms.cache as _crewai_cache
_crewai_cache.mark_cache_breakpoint = lambda msg: msg

from crewai import Agent, Task, Crew, Process, LLM
from crewai_tools import SerperDevTool
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

# LLM Obyektini Avtomatik Tanlash (Groq yoki Gemini)
if GROQ_API_KEY and GROQ_API_KEY != "gsk_your_groq_api_key_here":
    logging.info("🧠 AI LLM Provayderi: Groq (openai/gpt-oss-120b)")
    os.environ["GROQ_API_KEY"] = GROQ_API_KEY
    llm = LLM(
        model="groq/openai/gpt-oss-120b",
        api_key=GROQ_API_KEY,
        temperature=0.3,
        max_tokens=1200,
        num_retries=5
    )
elif GEMINI_API_KEY:
    logging.info("🧠 AI LLM Provayderi: Google Gemini (gemini-2.0-flash)")
    os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
    llm = LLM(
        model="gemini/gemini-2.0-flash",
        api_key=GEMINI_API_KEY,
        temperature=0.3
    )
else:
    logging.warning("⚠️ Diqqat: GROQ_API_KEY yoki GEMINI_API_KEY ko'rsatilmadi!")
    llm = None

# Qidiruv Vositasi (SerperDevTool)
if SERPER_API_KEY and SERPER_API_KEY != "your_serper_api_key_here":
    os.environ["SERPER_API_KEY"] = SERPER_API_KEY

search_tool = SerperDevTool()

# Agent Yaratish: Kiberxavfsizlik va Axborot Texnologiyalari Bosh Tahlilchisi
cyber_agent = Agent(
    role="Kiberxavfsizlik va Axborot Texnologiyalari Bosh Tahlilchisi",
    goal="Bugungi kiberxavfsizlik, zaifliklar (vulnerabilities), kiberhujumlar va IT-xavfsizlik yangiliklarini topish va tahlil qilish.",
    backstory=(
        "Siz har kuni The Hacker News, BleepingComputer, SecurityWeek kabi dunyoning yetakchi kiberxavfsizlik "
        "manbalarini kuzatib boruvchi, murakkab texnik xabarlarni oddiy va tushunarli o'zbek tiliga o'gira oladigan "
        "tajribali SMM tahlilchisi va kiberxavfsizlik ekspertisiz."
    ),
    tools=[search_tool],
    llm=llm,
    verbose=True,
    memory=False
)

# Task Prompt Yaratish
cyber_task = Task(
    description=(
        "Google va nufuzli manbalar (The Hacker News, BleepingComputer, SecurityWeek) orqali "
        "bugungi eng muhim va dolzarb 2 ta kiberxavfsizlik yangiligini toping va tahlil qiling.\n\n"
        "TALABLAR VA QOIDALAR:\n"
        "1. Barcha matn va xulosalar FAQAT va FAQAT O'ZBEK TILIDA bo'lishi shart.\n"
        "2. Har bir yangilik strictly quyidagi QISQA strukturada bo'lsin:\n\n"
        "📌 [Mavzu nomi / Sarlavha]\n"
        "• MOHIYATI: [Nima bo'lgani va kim/qaysi tizim xavf ostida ekani - FAQAT 1 gap]\n"
        "• XAVF DARAJASI: [Kritik / Yuqori / O'rta]\n"
        "• TAVSIYA: [1 ta qisqa maslahat]\n"
        "🔗 MANBA: [Original maqola havolasi (URL)]\n\n"
        "3. Telegram uchun moslashtirilgan emojilardan foydalanilsin.\n"
        "4. Har bir yangilik orasiga '-----------------------------------' ajratuvchisini qo'ying.\n"
        "5. Kirish, salomlashish yoki chiqish matnlari yozilmasin. Faqat yangiliklar posti berilsin.\n"
        "6. Umumiy javob juda qisqa va lo'nda bo'lsin, ortiqcha tafsilotlarga bormang."
    ),
    expected_output="2 ta eng muhim kiberxavfsizlik yangiligi belgilangan qisqa struktura va O'zbek tilidagi Telegram posti ko'rinishida.",
    agent=cyber_agent
)

# Crew Yaratish
crew = Crew(
    agents=[cyber_agent],
    tasks=[cyber_task],
    process=Process.sequential,
    verbose=True,
    memory=False
)


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
    """CrewAI taskini ishga tushiradi va natijani Telegram'ga yuboradi."""
    logging.info("🚀 [START] Kiberxavfsizlik yangiliklarini yig'ish va tahlil qilish boshlandi...")
    try:
        if not TELEGRAM_BOT_TOKEN:
            logging.error("❌ TELEGRAM_BOT_TOKEN topilmadi! .env faylini tekshiring.")
            return

        target_chat_id = MY_CHAT_ID
        if not target_chat_id:
            logging.warning("⚠️ MY_CHAT_ID belgilanmagan. .env fayliga MY_CHAT_ID va serper_api_key qo'shing.")
            return

        # CrewAI kickoff funksiyasini asinxron ishga tushirish
        result = await asyncio.to_thread(crew.kickoff)
        report_text = str(result)

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
