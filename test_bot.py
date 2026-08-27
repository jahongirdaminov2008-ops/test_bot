import os
import re
import sqlite3
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== SOZLAMALAR ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]           # Render Environment Variables'da o'rnatiladi
ADMIN_ID = int(os.environ["ADMIN_ID"])        # sizning Telegram user ID'ingiz (@userinfobot orqali bilib oling)
DB_PATH = "test_bot.db"


# ==================== RENDER UCHUN "SOXTA" HTTP SERVER ====================
# Render Web Service (bepul tarif) doim biror portni tinglashni talab qiladi.
# Bot esa polling orqali ishlaydi, portga ehtiyoji yo'q — shuning uchun
# faqat "OK" qaytaradigan minimal server ochib qo'yamiz.
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # konsolni chalkashtirmaslik uchun so'rov loglarini o'chiramiz


def run_ping_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()

logging.basicConfig(level=logging.INFO)


# ==================== BAZA ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            answer_key TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER,
            user_id INTEGER,
            full_name TEXT,
            correct_count INTEGER,
            total_count INTEGER,
            detail TEXT,
            submitted_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_active_test():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, answer_key FROM tests WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row  # (id, answer_key) yoki None


def parse_answer_string(text: str) -> dict:
    """
    '1-A 2-B 3-C' yoki '1. A, 2. B, 3. C' kabi formatlarni o'qiydi.
    Natija: {1: 'A', 2: 'B', 3: 'C'}
    """
    pattern = r"(\d+)\s*[-.\):]\s*([A-DA-Dа-гA-D])"
    matches = re.findall(pattern, text.upper())
    return {int(num): letter.strip() for num, letter in matches}


# ==================== ADMIN: YANGI TEST ====================
async def yangitest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Javoblar kalitini shu formatda yozing:\n"
            "/yangitest 1-A 2-B 3-C 4-D 5-A"
        )
        return

    full_text = " ".join(context.args)
    answers = parse_answer_string(full_text)
    if not answers:
        await update.message.reply_text("❌ Format noto'g'ri. Masalan: 1-A 2-B 3-C")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tests SET is_active = 0")  # eski testlarni yopamiz
    cur.execute(
        "INSERT INTO tests (answer_key, is_active, created_at) VALUES (?, 1, ?)",
        (full_text, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Yangi test yaratildi ({len(answers)} ta savol).\n"
        f"Endi test faylini (rasm/PDF) shu botga yuboring — men uni barcha o'quvchilarga tarqataman.\n"
        f"Yoki o'quvchilar to'g'ridan-to'g'ri javob yubora boshlashi mumkin."
    )


# ==================== ADMIN: TEST FAYLINI TARQATISH ====================
async def broadcast_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM students")
    students = cur.fetchall()
    conn.close()

    if not students:
        await update.message.reply_text("Hali birorta o'quvchi ro'yxatdan o'tmagan.")
        return

    sent = 0
    for (uid,) in students:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            sent += 1
        except Exception as e:
            logging.warning(f"Yuborilmadi {uid}: {e}")

    await update.message.reply_text(f"📤 Test {sent} ta o'quvchiga yuborildi.")


# ==================== O'QUVCHI: /start ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO students (user_id, full_name, username) VALUES (?, ?, ?)",
        (user.id, user.full_name, user.username or ""),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "Assalomu alaykum! Siz ro'yxatdan o'tdingiz.\n"
        "Test kelganda javoblaringizni shu formatda yuboring:\n"
        "1-A 2-B 3-C 4-D"
    )


# ==================== O'QUVCHI: JAVOB YUBORISH ====================
async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    student_answers = parse_answer_string(text)
    if not student_answers:
        return  # oddiy xabar, javob formatiga mos kelmadi — e'tiborsiz qoldiramiz

    test = get_active_test()
    if not test:
        await update.message.reply_text("Hozircha faol test yo'q.")
        return

    test_id, answer_key = test
    correct_answers = parse_answer_string(answer_key)

    correct_count = 0
    detail_lines = []
    for q_num, correct_letter in sorted(correct_answers.items()):
        student_letter = student_answers.get(q_num)
        if student_letter == correct_letter:
            correct_count += 1
            detail_lines.append(f"{q_num}. ✅")
        elif student_letter is None:
            detail_lines.append(f"{q_num}. ⬜ (javob yo'q, to'g'risi: {correct_letter})")
        else:
            detail_lines.append(f"{q_num}. ❌ (siz: {student_letter}, to'g'risi: {correct_letter})")

    total = len(correct_answers)
    detail_text = "\n".join(detail_lines)
    user = update.effective_user

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO submissions
           (test_id, user_id, full_name, correct_count, total_count, detail, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (test_id, user.id, user.full_name, correct_count, total, detail_text, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"📊 Natijangiz: {correct_count}/{total}\n\n{detail_text}"
    )


# ==================== ADMIN: NATIJALARNI KO'RISH ====================
async def natijalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    test = get_active_test()
    if not test:
        await update.message.reply_text("Faol test yo'q.")
        return

    test_id, _ = test
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT full_name, correct_count, total_count, submitted_at
           FROM submissions WHERE test_id = ? ORDER BY correct_count DESC""",
        (test_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Hali hech kim javob yubormagan.")
        return

    lines = [f"{i+1}. {name} — {c}/{t}" for i, (name, c, t, _) in enumerate(rows)]
    await update.message.reply_text("📋 Natijalar:\n\n" + "\n".join(lines))


# ==================== ISHGA TUSHIRISH ====================
def main():
    init_db()

    # Ping serverni alohida threadda ishga tushiramiz (Render "port ochiq" deb bilishi uchun)
    threading.Thread(target=run_ping_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yangitest", yangitest))
    app.add_handler(CommandHandler("natijalar", natijalar))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, broadcast_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))

    print("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
