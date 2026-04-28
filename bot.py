#!/usr/bin/env python3
"""
Telegram Bot — Bulk Vehicle → Mobile Checker + Generator
Each user has their own independent job
"""

import csv
import time
import requests
import threading
import io
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)

BOT_TOKEN    = "8598911569:AAE_Z1JZOkUU_yxjgfedirQ25n-AH_Sgq6Q"
API_BASE     = "https://vehicle-2-num.rasiksarkarrasiksarkar.workers.dev/"
API_KEY      = "raj_ki_mkc"
MAX_WORKERS  = 5
TIMEOUT      = 8

# ─────────────────────────────────────────────
#  PER-USER JOB STATE
# ─────────────────────────────────────────────
user_jobs = {}   # user_id -> job dict
jobs_lock = threading.Lock()

def get_job(user_id: int) -> dict:
    with jobs_lock:
        if user_id not in user_jobs:
            user_jobs[user_id] = {
                "running": False,
                "stop":    False,
                "total":   0,
                "done":    0,
                "found":   0,
                "hits":    [],
                "start":   None,
                "lock":    threading.Lock(),
            }
        return user_jobs[user_id]


# ─────────────────────────────────────────────
#  API LOOKUP
# ─────────────────────────────────────────────
def lookup(vehicle_number: str):
    url = f"{API_BASE}?key={API_KEY}&vehicle_number={vehicle_number.strip().upper()}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            mobile = (
                data.get("result", {})
                    .get("data", {})
                    .get("result", {})
                    .get("mobile_no", "")
            )
            return mobile if mobile else None
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  GENERATOR
# ─────────────────────────────────────────────
def generate_numbers(prefix: str, start: int = 0, end: int = 9999) -> list:
    return [f"{prefix.strip().upper()}{str(i).zfill(4)}" for i in range(start, end + 1)]


# ─────────────────────────────────────────────
#  PARSE
# ─────────────────────────────────────────────
def parse_input(text: str) -> list:
    text = text.strip().upper()
    if "-" in text:
        parts = text.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            pp = parts[0]
            end = int(parts[1])
            for i, c in enumerate(pp):
                if c.isdigit():
                    return generate_numbers(pp[:i], int(pp[i:]), end)
    if len(text) == 6 and text[:2].isalpha():
        return generate_numbers(text)
    return [n.strip() for n in text.replace(",", " ").split() if n.strip()]


def parse_file(content: bytes, filename: str) -> list:
    lines = content.decode("utf-8", errors="ignore").splitlines()
    numbers = []
    if filename.endswith(".csv"):
        for row in csv.reader(lines):
            if row:
                numbers.append(row[0].strip().upper())
    else:
        numbers = [l.strip().upper() for l in lines if l.strip()]
    return [n for n in numbers if not n.lower().startswith("vehicle")]


def dedup(lst: list) -> list:
    seen, out = set(), []
    for n in lst:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ─────────────────────────────────────────────
#  PROGRESS TEXT
# ─────────────────────────────────────────────
def progress_text(done, total, found, elapsed) -> str:
    speed  = done / elapsed if elapsed > 0 else 0
    eta    = (total - done) / speed if speed > 0 else 0
    pct    = done / total * 100 if total else 0
    filled = int(pct / 5)
    bar    = "█" * filled + "░" * (20 - filled)
    return (
        f"🚗 *Vehicle → Mobile Checker*\n\n"
        f"`[{bar}]`\n"
        f"📊 Progress : `{done:,} / {total:,}` ({pct:.1f}%)\n"
        f"✅ Hits     : `{found}`\n"
        f"⚡ Speed    : `{speed:.1f} req/s`\n"
        f"⏱ ETA      : `{int(eta//60)}m {int(eta%60)}s`"
    )


# ─────────────────────────────────────────────
#  WORKERS  (per user)
# ─────────────────────────────────────────────
def run_workers(vehicle_numbers: list, job: dict):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_v = {executor.submit(lookup, v): v for v in vehicle_numbers}
        for future in as_completed(future_to_v):
            vehicle = future_to_v[future]
            mobile  = future.result()
            with job["lock"]:
                job["done"] += 1
                if mobile:
                    job["found"] += 1
                    job["hits"].append(f"{vehicle}:{mobile}")
            if job["stop"]:
                for f in future_to_v:
                    f.cancel()
                break


# ─────────────────────────────────────────────
#  RUN JOB  (per user)
# ─────────────────────────────────────────────
async def run_job(vehicle_numbers: list, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    job   = get_job(user_id)
    total = len(vehicle_numbers)

    with job["lock"]:
        job["running"] = True
        job["stop"]    = False
        job["total"]   = total
        job["done"]    = 0
        job["found"]   = 0
        job["hits"]    = []
        job["start"]   = time.time()

    msg = await context.bot.send_message(
        chat_id,
        progress_text(0, total, 0, 0.001),
        parse_mode="Markdown"
    )
    msg_id = msg.message_id

    loop        = asyncio.get_event_loop()
    worker_task = loop.run_in_executor(None, run_workers, vehicle_numbers, job)
    last_edit   = 0

    while not worker_task.done():
        await asyncio.sleep(5)
        if job["stop"]:
            break
        done  = job["done"]
        found = job["found"]
        if done != last_edit:
            last_edit = done
            elapsed = time.time() - job["start"]
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=progress_text(done, total, found, elapsed),
                    parse_mode="Markdown"
                )
            except Exception as e:
                err = str(e).lower()
                if "flood" in err or "retry" in err or "429" in err:
                    await asyncio.sleep(15)

    await worker_task

    elapsed = time.time() - job["start"]
    done    = job["done"]
    found   = job["found"]
    hits    = list(job["hits"])

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"{'✅ Done' if not job['stop'] else '🛑 Stopped'}!\n\n"
                f"📊 Total   : `{total:,}`\n"
                f"✅ Hits    : `{found}`\n"
                f"❌ No Data : `{done - found:,}`\n"
                f"⏱ Time    : `{elapsed:.1f}s`"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    if hits:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_content = "vehicle_number,mobile_no\n"
        for h in hits:
            v, m = h.rsplit(":", 1)
            csv_content += f"{v},{m}\n"
        await context.bot.send_document(
            chat_id,
            document=io.BytesIO(csv_content.encode()),
            filename=f"hits_{ts}.csv",
            caption=f"📋 {found} hits — CSV"
        )
        await context.bot.send_document(
            chat_id,
            document=io.BytesIO("\n".join(hits).encode()),
            filename=f"hits_{ts}.txt",
            caption="📄 VEHICLE:MOBILE format"
        )
    else:
        await context.bot.send_message(chat_id, "❌ No hits found.")

    with job["lock"]:
        job["running"] = False


# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚗 *Vehicle → Mobile Checker Bot*\n\n"
        "*Generator:*\n"
        "`/gen MH48AX` — generate & check 0000–9999\n"
        "`/gen MH48AX 2000 5000` — custom range\n\n"
        "*Checker:*\n"
        "`/check MH48AX0000-4999` — check a range\n"
        "`/check MH12AB1234,DL4CAF5678` — specific numbers\n"
        "Send a `.txt` or `.csv` file — bulk check\n\n"
        "`/status` — your job progress\n"
        "`/stop` — stop your job",
        parse_mode="Markdown"
    )


async def cmd_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = get_job(user_id)

    if job["running"]:
        await update.message.reply_text("⚠️ You already have a job running. Use /stop first.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/gen MH48AX` or `/gen MH48AX 0000 4999`", parse_mode="Markdown")
        return

    prefix = args[0].strip().upper()
    if len(prefix) != 6:
        await update.message.reply_text("❌ Prefix must be 6 characters. Example: `MH48AX`", parse_mode="Markdown")
        return

    try:
        start = int(args[1]) if len(args) > 1 else 0
        end   = int(args[2]) if len(args) > 2 else 9999
    except ValueError:
        await update.message.reply_text("❌ Invalid range.", parse_mode="Markdown")
        return

    if not (0 <= start <= end <= 9999):
        await update.message.reply_text("❌ Range must be between 0 and 9999.")
        return

    numbers = generate_numbers(prefix, start, end)
    await update.message.reply_text(
        f"🔧 Generated *{len(numbers):,}* numbers: `{numbers[0]}` → `{numbers[-1]}`\nStarting...",
        parse_mode="Markdown"
    )
    context.application.create_task(run_job(numbers, update.effective_chat.id, user_id, context))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = get_job(user_id)

    if job["running"]:
        await update.message.reply_text("⚠️ You already have a job running. Use /stop first.")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: `/check MH48AX0000-4999`", parse_mode="Markdown")
        return

    numbers = dedup(parse_input(text))
    if not numbers:
        await update.message.reply_text("❌ Could not parse vehicle numbers.")
        return

    context.application.create_task(run_job(numbers, update.effective_chat.id, user_id, context))


async def cmd_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = get_job(user_id)

    if job["running"]:
        await update.message.reply_text("⚠️ You already have a job running. Use /stop first.")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Send a .txt or .csv file.")
        return

    file    = await doc.get_file()
    content = await file.download_as_bytearray()
    numbers = dedup(parse_file(bytes(content), doc.file_name))

    if not numbers:
        await update.message.reply_text("❌ No vehicle numbers found in file.")
        return

    context.application.create_task(run_job(numbers, update.effective_chat.id, user_id, context))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = get_job(user_id)

    if not job["running"]:
        await update.message.reply_text("❌ No active task.")
        return

    elapsed = max(time.time() - job["start"], 0.001)
    await update.message.reply_text(
        progress_text(job["done"], job["total"], job["found"], elapsed),
        parse_mode="Markdown"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = get_job(user_id)

    if not job["running"]:
        await update.message.reply_text("❌ No active task.")
        return

    job["stop"] = True
    await update.message.reply_text(
        f"🛑 *Stopping your job...*\n"
        f"📊 Checked so far : `{job['done']:,} / {job['total']:,}`\n"
        f"✅ Hits so far    : `{job['found']}`",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("gen",    cmd_gen))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, cmd_file))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()