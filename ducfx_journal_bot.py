"""
DucFX Trading Journal Bot - Telegram
=====================================
Bot xử lý interactive journal: checklist, cảm xúc, tổng kết tuần.
Chạy song song với EA MQL5 DucFX_TelegramJournal.mq5

EA gửi trade data qua HTTP POST → Bot nhận, lưu, gửi journal prompt lên Telegram.

Cài đặt:
    pip install python-telegram-bot==20.7 apscheduler aiohttp

Chạy:
    python ducfx_journal_bot.py

Biến môi trường:
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id
    PORT=8080  (Railway tự set)
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ═══════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = Path("journal_data")
DATA_DIR.mkdir(exist_ok=True)

# Global bot reference for HTTP handler to send Telegram messages
g_bot: Bot = None
TRADES_FILE = DATA_DIR / "trades.json"
WEEKLY_REPORT_DAY = "sunday"  # Gửi tổng kết vào Chủ Nhật
WEEKLY_REPORT_HOUR = 20       # 8 PM

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DucFX_Bot")

# ═══════════════════════════════════════════
# DATA STORAGE
# ═══════════════════════════════════════════
def load_trades() -> list:
    if TRADES_FILE.exists():
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_trades(trades: list):
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2, default=str)

def find_trade(ticket: str) -> dict | None:
    trades = load_trades()
    for t in trades:
        if str(t.get("ticket")) == str(ticket):
            return t
    return None

def update_trade(ticket: str, updates: dict):
    trades = load_trades()
    for t in trades:
        if str(t.get("ticket")) == str(ticket):
            t.update(updates)
            break
    save_trades(trades)

# ═══════════════════════════════════════════
# PARSE TRADE DATA FROM EA
# ═══════════════════════════════════════════
def parse_trade_data(text: str) -> dict | None:
    """Parse TRADE_DATA message from EA"""
    if not text.startswith("TRADE_DATA|"):
        return None
    
    parts = text.split("|")
    if len(parts) < 14:
        return None
    
    try:
        return {
            "ticket": parts[1],
            "symbol": parts[2],
            "direction": parts[3],
            "entry": float(parts[4]),
            "close": float(parts[5]),
            "sl": float(parts[6]) if parts[6] != "0" else None,
            "tp": float(parts[7]) if parts[7] != "0" else None,
            "lots": float(parts[8]),
            "pips": float(parts[9]),
            "pl_usd": float(parts[10]),
            "open_time": parts[11],
            "close_time": parts[12],
            "hold_time": parts[13],
            "timestamp": datetime.now().isoformat(),
            # Journal fields (to be filled by user)
            "checklist": {
                "ema_trend": None,
                "dow_structure": None,
                "value_zone": None,
                "confirmation": None,
                "sl_lot_calc": None,
            },
            "emotion": None,
            "note": None,
            "journal_complete": False,
        }
    except (ValueError, IndexError) as e:
        logger.error(f"Parse error: {e}")
        return None

# ═══════════════════════════════════════════
# INLINE KEYBOARDS
# ═══════════════════════════════════════════
def checklist_keyboard(ticket: str, step: int) -> InlineKeyboardMarkup:
    """Generate checklist keyboard for each step"""
    questions = [
        "1️⃣ EMA 34/89 Trend rõ ràng?",
        "2️⃣ Dow Structure đồng thuận?",
        "3️⃣ Giá trong Value Zone?",
        "4️⃣ Có tín hiệu xác nhận?",
        "5️⃣ SL & Lot đã tính đúng?",
    ]
    
    if step >= 5:
        return emotion_keyboard(ticket)
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ CÓ", callback_data=f"cl_{ticket}_{step}_yes"),
         InlineKeyboardButton(f"❌ KHÔNG", callback_data=f"cl_{ticket}_{step}_no")],
        [InlineKeyboardButton(f"⏭ Bỏ qua lệnh này", callback_data=f"cl_{ticket}_skip")]
    ])

def emotion_keyboard(ticket: str) -> InlineKeyboardMarkup:
    """Generate emotion selection keyboard"""
    emotions = [
        ("😌 Bình tĩnh", "calm"),
        ("💪 Tự tin", "confident"),
        ("😰 Lo lắng", "anxious"),
        ("🤩 Hưng phấn", "excited"),
        ("😤 FOMO", "fomo"),
        ("🔥 Revenge", "revenge"),
        ("😓 Sợ hãi", "fear"),
        ("😑 Chán nản", "bored"),
    ]
    
    rows = []
    for i in range(0, len(emotions), 2):
        row = [InlineKeyboardButton(emotions[i][0], callback_data=f"em_{ticket}_{emotions[i][1]}")]
        if i + 1 < len(emotions):
            row.append(InlineKeyboardButton(emotions[i+1][0], callback_data=f"em_{ticket}_{emotions[i+1][1]}"))
        rows.append(row)
    
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🚀 *DucFX Trading Journal Bot*\n\n"
        "Bot tự động ghi nhật ký giao dịch từ MT5.\n\n"
        "*Lệnh có sẵn:*\n"
        "/journal `<ticket>` — Ghi chú cho lệnh\n"
        "/stats — Thống kê tổng quan\n"
        "/week — Tổng kết tuần này\n"
        "/lastweek — Tổng kết tuần trước\n"
        "/streak — Chuỗi thắng/thua hiện tại\n"
        "/rules — 10 Quy Tắc Vàng\n"
        "/discipline — Điểm kỷ luật\n"
        "/help — Trợ giúp\n\n"
        "EA MQL5 sẽ tự động gửi data khi lệnh đóng."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start journal entry for a specific trade"""
    if not context.args:
        # Show last trade
        trades = load_trades()
        if not trades:
            await update.message.reply_text("Chưa có lệnh nào được ghi nhận.")
            return
        trade = trades[-1]
        ticket = trade["ticket"]
    else:
        ticket = context.args[0]
    
    trade = find_trade(ticket)
    if not trade:
        await update.message.reply_text(f"Không tìm thấy lệnh #{ticket}")
        return
    
    if trade.get("journal_complete"):
        await update.message.reply_text(f"✅ Lệnh #{ticket} đã ghi journal rồi.")
        return
    
    questions = [
        "1️⃣ EMA 34/89 Trend rõ ràng?",
        "2️⃣ Dow Structure đồng thuận?",
        "3️⃣ Giá trong Value Zone?",
        "4️⃣ Có tín hiệu xác nhận?",
        "5️⃣ SL & Lot đã tính đúng?",
    ]
    
    msg = (
        f"📝 *JOURNAL — Lệnh #{ticket}*\n"
        f"{trade['symbol']} | {trade['direction']} | "
        f"{'✅' if trade['pl_usd'] >= 0 else '❌'} {trade['pl_usd']:+.2f} USD\n\n"
        f"*CHECKLIST — Lúc vào lệnh:*\n"
        f"{questions[0]}"
    )
    
    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=checklist_keyboard(ticket, 0)
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overall statistics"""
    trades = load_trades()
    if not trades:
        await update.message.reply_text("Chưa có data. EA sẽ tự gửi khi lệnh đóng.")
        return
    
    msg = generate_stats_message(trades, "📊 *THỐNG KÊ TỔNG QUAN*")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show this week's summary"""
    trades = load_trades()
    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    
    week_trades = [t for t in trades if datetime.fromisoformat(t["timestamp"]) >= start_of_week]
    
    if not week_trades:
        await update.message.reply_text("Tuần này chưa có lệnh nào.")
        return
    
    msg = generate_stats_message(week_trades, "📅 *TỔNG KẾT TUẦN NÀY*")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_lastweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last week's summary"""
    trades = load_trades()
    now = datetime.now()
    start_of_this_week = now - timedelta(days=now.weekday())
    start_of_this_week = start_of_this_week.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_last_week = start_of_this_week - timedelta(days=7)
    
    week_trades = [t for t in trades 
                   if start_of_last_week <= datetime.fromisoformat(t["timestamp"]) < start_of_this_week]
    
    if not week_trades:
        await update.message.reply_text("Tuần trước không có lệnh nào.")
        return
    
    msg = generate_stats_message(week_trades, "📅 *TỔNG KẾT TUẦN TRƯỚC*")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current win/loss streak"""
    trades = load_trades()
    if not trades:
        await update.message.reply_text("Chưa có data.")
        return
    
    streak = 0
    streak_type = ""
    
    for t in reversed(trades):
        if t["pl_usd"] >= 0:
            if streak_type == "" or streak_type == "win":
                streak_type = "win"
                streak += 1
            else:
                break
        else:
            if streak_type == "" or streak_type == "loss":
                streak_type = "loss"
                streak += 1
            else:
                break
    
    if streak_type == "win":
        emoji = "🔥" * min(streak, 5)
        msg = f"{emoji} *Chuỗi THẮNG: {streak} lệnh liên tiếp!*"
    else:
        emoji = "⚠️"
        msg = f"{emoji} *Chuỗi THUA: {streak} lệnh liên tiếp*\n\n"
        if streak >= 2:
            msg += "🛑 *DỪNG LẠI! Quy tắc 3: Thua 2 lệnh → Nghỉ trong ngày!*"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show 10 Golden Rules"""
    msg = (
        "📜 *10 QUY TẮC VÀNG — DucFX*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Checklist 5/5 mới vào lệnh\n"
        "2️⃣ Risk tối đa 5% mỗi lệnh\n"
        "3️⃣ Thua 2 liên tiếp → Nghỉ ngày\n"
        "4️⃣ Thua 5% tuần → Nghỉ tuần\n"
        "5️⃣ Không Revenge Trade\n"
        "6️⃣ Không chống Momentum mạnh\n"
        "7️⃣ Sideways = Không trade\n"
        "8️⃣ Phân tích tối, thực thi ngày\n"
        "9️⃣ Ghi nhật ký MỖI lệnh\n"
        "🔟 Kiên nhẫn là siêu năng lực\n\n"
        "_\"Tiền lớn kiếm được từ việc NGỒI CHỜ\"_\n"
        "_— Jesse Livermore_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_discipline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculate discipline score"""
    trades = load_trades()
    journaled = [t for t in trades if t.get("journal_complete")]
    
    if not journaled:
        await update.message.reply_text("Chưa có đủ data journal để chấm điểm.")
        return
    
    # Calculate discipline metrics
    total = len(journaled)
    checklist_pass = sum(1 for t in journaled 
                        if all(v == True for v in t.get("checklist", {}).values() if v is not None))
    
    bad_emotions = {"fomo", "revenge", "fear"}
    calm_trades = sum(1 for t in journaled if t.get("emotion") not in bad_emotions)
    
    checklist_rate = checklist_pass / total * 100 if total > 0 else 0
    emotion_rate = calm_trades / total * 100 if total > 0 else 0
    journal_rate = len(journaled) / len(trades) * 100 if trades else 0
    
    discipline_score = (checklist_rate * 0.4 + emotion_rate * 0.3 + journal_rate * 0.3)
    
    # Grade
    if discipline_score >= 90: grade = "🏆 XUẤT SẮC"
    elif discipline_score >= 75: grade = "✅ TỐT"
    elif discipline_score >= 60: grade = "⚠️ CẦN CẢI THIỆN"
    else: grade = "🚨 NGUY HIỂM"
    
    # Emotion breakdown
    emotion_counts = {}
    for t in journaled:
        em = t.get("emotion", "unknown")
        emotion_counts[em] = emotion_counts.get(em, 0) + 1
    
    emotion_labels = {
        "calm": "😌 Bình tĩnh", "confident": "💪 Tự tin",
        "anxious": "😰 Lo lắng", "excited": "🤩 Hưng phấn",
        "fomo": "😤 FOMO", "revenge": "🔥 Revenge",
        "fear": "😓 Sợ hãi", "bored": "😑 Chán nản",
    }
    
    emotion_str = ""
    for em, count in sorted(emotion_counts.items(), key=lambda x: -x[1]):
        label = emotion_labels.get(em, em)
        emotion_str += f"  {label}: {count} lần\n"
    
    # Emotion-win correlation
    emotion_wins = {}
    emotion_losses = {}
    for t in journaled:
        em = t.get("emotion", "unknown")
        if t["pl_usd"] >= 0:
            emotion_wins[em] = emotion_wins.get(em, 0) + 1
        else:
            emotion_losses[em] = emotion_losses.get(em, 0) + 1
    
    msg = (
        f"🎯 *ĐIỂM KỶ LUẬT: {discipline_score:.0f}/100 — {grade}*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Tuân thủ Checklist: {checklist_rate:.0f}% ({checklist_pass}/{total})\n"
        f"🧠 Tâm lý ổn định: {emotion_rate:.0f}% ({calm_trades}/{total})\n"
        f"📝 Tỷ lệ ghi Journal: {journal_rate:.0f}% ({len(journaled)}/{len(trades)})\n\n"
        f"*Phân tích cảm xúc:*\n{emotion_str}"
    )
    
    # Top insight
    if "revenge" in emotion_losses and emotion_losses["revenge"] > 0:
        msg += f"\n🚨 *CẢNH BÁO: {emotion_losses.get('revenge',0)} lệnh Revenge đều THUA!*"
    if "fomo" in emotion_losses and emotion_losses["fomo"] > 0:
        msg += f"\n⚠️ *FOMO dẫn đến {emotion_losses.get('fomo',0)} lệnh thua*"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# ═══════════════════════════════════════════
# CALLBACK HANDLERS
# ═══════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # --- CHECKLIST CALLBACK ---
    if data.startswith("cl_"):
        parts = data.split("_")
        ticket = parts[1]
        
        if parts[2] == "skip":
            await query.edit_message_text(f"⏭ Bỏ qua journal cho lệnh #{ticket}")
            return
        
        step = int(parts[2])
        answer = parts[3] == "yes"
        
        # Save checklist answer
        trade = find_trade(ticket)
        if not trade:
            await query.edit_message_text(f"Không tìm thấy lệnh #{ticket}")
            return
        
        checklist_keys = ["ema_trend", "dow_structure", "value_zone", "confirmation", "sl_lot_calc"]
        trade["checklist"][checklist_keys[step]] = answer
        update_trade(ticket, {"checklist": trade["checklist"]})
        
        questions = [
            "1️⃣ EMA Trend?",
            "2️⃣ Dow Structure?",
            "3️⃣ Value Zone?",
            "4️⃣ Tín hiệu xác nhận?",
            "5️⃣ SL & Lot?",
        ]
        
        # Build progress
        progress = ""
        for i in range(step + 1):
            v = trade["checklist"][checklist_keys[i]]
            progress += f"{'✅' if v else '❌'} {questions[i]}\n"
        
        next_step = step + 1
        
        if next_step < 5:
            msg = (
                f"📝 *JOURNAL — Lệnh #{ticket}*\n\n"
                f"*Checklist:*\n{progress}\n"
                f"{questions[next_step]} — Lúc vào lệnh có pass không?"
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=checklist_keyboard(ticket, next_step)
            )
        else:
            # All checklist done, ask emotion
            pass_count = sum(1 for k in checklist_keys if trade["checklist"][k])
            pass_emoji = "✅ PASS" if pass_count == 5 else f"❌ FAIL ({pass_count}/5)"
            
            msg = (
                f"📝 *JOURNAL — Lệnh #{ticket}*\n\n"
                f"*Checklist: {pass_emoji}*\n{progress}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🧠 *Cảm xúc lúc vào lệnh?*"
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=emotion_keyboard(ticket)
            )
    
    # --- EMOTION CALLBACK ---
    elif data.startswith("em_"):
        parts = data.split("_")
        ticket = parts[1]
        emotion = parts[2]
        
        update_trade(ticket, {"emotion": emotion})
        
        trade = find_trade(ticket)
        emotion_labels = {
            "calm": "😌 Bình tĩnh", "confident": "💪 Tự tin",
            "anxious": "😰 Lo lắng", "excited": "🤩 Hưng phấn",
            "fomo": "😤 FOMO", "revenge": "🔥 Revenge",
            "fear": "😓 Sợ hãi", "bored": "😑 Chán nản",
        }
        
        # Warning for bad emotions
        warning = ""
        if emotion in ("fomo", "revenge"):
            warning = "\n\n🚨 *CẢNH BÁO: Lệnh này có dấu hiệu phá kỷ luật!*"
        
        msg = (
            f"📝 *JOURNAL — Lệnh #{ticket}*\n\n"
            f"Cảm xúc: {emotion_labels.get(emotion, emotion)}{warning}\n\n"
            f"💬 *Gõ ghi chú / bài học cho lệnh này:*\n"
            f"(Hoặc gõ /skip để bỏ qua)"
        )
        
        # Store pending note ticket
        context.user_data["pending_note_ticket"] = ticket
        
        await query.edit_message_text(msg, parse_mode="Markdown")

# ═══════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # --- Save trade note ---
    pending_ticket = context.user_data.get("pending_note_ticket")
    if pending_ticket and text != "/skip":
        update_trade(pending_ticket, {"note": text, "journal_complete": True})
        context.user_data.pop("pending_note_ticket", None)
        
        trade = find_trade(pending_ticket)
        checklist = trade.get("checklist", {})
        pass_count = sum(1 for v in checklist.values() if v == True)
        
        await update.message.reply_text(
            f"✅ *Journal hoàn tất — Lệnh #{pending_ticket}*\n\n"
            f"📋 Checklist: {pass_count}/5\n"
            f"🧠 Cảm xúc: {trade.get('emotion', '?')}\n"
            f"📝 Ghi chú: {text}\n"
            f"💰 P/L: {trade['pl_usd']:+.2f} USD",
            parse_mode="Markdown"
        )
        return
    
    if pending_ticket and text == "/skip":
        update_trade(pending_ticket, {"note": "(bỏ qua)", "journal_complete": True})
        context.user_data.pop("pending_note_ticket", None)
        await update.message.reply_text("✅ Journal đã lưu (không có ghi chú).")
        return

# ═══════════════════════════════════════════
# STATS GENERATOR
# ═══════════════════════════════════════════
def generate_stats_message(trades: list, title: str) -> str:
    total = len(trades)
    wins = [t for t in trades if t["pl_usd"] >= 0]
    losses = [t for t in trades if t["pl_usd"] < 0]
    
    total_pl = sum(t["pl_usd"] for t in trades)
    total_pips = sum(t["pips"] for t in trades)
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_win = sum(t["pl_usd"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pl_usd"] for t in losses) / len(losses) if losses else 0
    
    # Profit factor
    gross_profit = sum(t["pl_usd"] for t in wins)
    gross_loss = abs(sum(t["pl_usd"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Best and worst trade
    best = max(trades, key=lambda t: t["pl_usd"])
    worst = min(trades, key=lambda t: t["pl_usd"])
    
    # Checklist compliance
    journaled = [t for t in trades if t.get("journal_complete")]
    if journaled:
        checklist_pass = sum(1 for t in journaled 
                           if all(v == True for v in t.get("checklist", {}).values() if v is not None))
        checklist_rate = checklist_pass / len(journaled) * 100
    else:
        checklist_rate = 0
        checklist_pass = 0
    
    pl_emoji = "📈" if total_pl >= 0 else "📉"
    
    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Tổng lệnh: {total}\n"
        f"✅ Thắng: {len(wins)} | ❌ Thua: {len(losses)}\n"
        f"🎯 Win Rate: {win_rate:.1f}%\n\n"
        f"{pl_emoji} *Tổng P/L: {total_pl:+.2f} USD*\n"
        f"📊 Tổng Pips: {total_pips:+.1f}\n"
        f"📈 TB Thắng: {avg_win:+.2f} USD\n"
        f"📉 TB Thua: {avg_loss:+.2f} USD\n"
        f"⚖️ Profit Factor: {profit_factor:.2f}\n\n"
        f"🏆 Best: {best['pl_usd']:+.2f} USD ({best['symbol']})\n"
        f"💀 Worst: {worst['pl_usd']:+.2f} USD ({worst['symbol']})\n\n"
        f"📋 Tuân thủ Checklist: {checklist_rate:.0f}%\n"
        f"📝 Đã ghi Journal: {len(journaled)}/{total}"
    )
    
    return msg

# ═══════════════════════════════════════════
# WEEKLY REPORT (SCHEDULED)
# ═══════════════════════════════════════════
async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Auto-send weekly report every Sunday"""
    trades = load_trades()
    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    
    week_trades = [t for t in trades if datetime.fromisoformat(t["timestamp"]) >= start_of_week]
    
    if not week_trades:
        msg = (
            "📅 *TỔNG KẾT TUẦN*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Tuần này không có lệnh nào.\n"
            "Nhớ: Ngày không trade = Ngày thắng! 💪"
        )
    else:
        msg = generate_stats_message(week_trades, "📅 *TỔNG KẾT TUẦN TỰ ĐỘNG*")
        
        # Add discipline reminder
        journaled = [t for t in week_trades if t.get("journal_complete")]
        unjournaled = len(week_trades) - len(journaled)
        if unjournaled > 0:
            msg += f"\n\n⚠️ Còn {unjournaled} lệnh chưa ghi journal!"
    
    msg += "\n\n_Chúc thầy cuối tuần vui vẻ! 🙏_"
    
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ═══════════════════════════════════════════
# HTTP ENDPOINT — Nhận trade data từ EA
# ═══════════════════════════════════════════
async def handle_trade_post(request):
    """EA gửi POST /trade với body = TRADE_DATA|..."""
    global g_bot
    try:
        body = await request.text()
        logger.info(f"HTTP received: {body[:80]}")

        if not body.startswith("TRADE_DATA|"):
            return web.Response(text="IGNORED", status=200)

        trade_data = parse_trade_data(body)
        if not trade_data:
            return web.Response(text="PARSE_ERROR", status=400)

        # Save trade
        trades = load_trades()
        trades.append(trade_data)
        save_trades(trades)
        logger.info(f"Trade #{trade_data['ticket']} saved. Total: {len(trades)}")

        # Send journal prompt to Telegram
        if g_bot:
            ticket = trade_data["ticket"]
            pl = trade_data["pl_usd"]
            sym = trade_data["symbol"]
            d = trade_data["direction"]
            emoji = "✅" if pl >= 0 else "❌"

            msg = (
                f"{emoji} *Lệnh #{ticket} đã lưu!*\n"
                f"{sym} | {d} | {pl:+.2f} USD\n\n"
                f"📝 Dùng /journal {ticket} để ghi checklist & cảm xúc."
            )
            await g_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

            # Check loss streak
            recent = trades[-3:]
            losses = sum(1 for t in recent if t["pl_usd"] < 0)
            if losses >= 2:
                await g_bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        "🛑 *CẢNH BÁO: Thua 2+ lệnh liên tiếp!*\n"
                        "Quy tắc 3: NGHỈ TRONG NGÀY.\n"
                        "Tắt MT5, đi làm việc khác. 🙏"
                    ),
                    parse_mode="Markdown",
                )

        return web.Response(text="OK", status=200)

    except Exception as e:
        logger.error(f"HTTP error: {e}")
        return web.Response(text=str(e), status=500)

async def handle_health(request):
    """Health check endpoint"""
    trades = load_trades()
    return web.Response(text=f"OK | {len(trades)} trades", status=200)

# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════
def main():
    global g_bot

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("⚠️  CHƯA CẤU HÌNH BOT TOKEN!")
        print("=" * 50)
        print()
        print("Các bước cài đặt:")
        print("1. Mở Telegram, tìm @BotFather")
        print("2. Gõ /newbot → đặt tên → nhận TOKEN")
        print("3. Tìm @userinfobot → lấy CHAT_ID")
        print("4. Set biến môi trường:")
        print("   export TELEGRAM_BOT_TOKEN='your_token'")
        print("   export TELEGRAM_CHAT_ID='your_chat_id'")
        print("5. Chạy lại: python ducfx_journal_bot.py")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    g_bot = app.bot

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("lastweek", cmd_lastweek))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("discipline", cmd_discipline))
    app.add_handler(CommandHandler("help", cmd_start))

    # Callback handler (inline keyboards)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Message handler (notes from user)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule weekly report
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_weekly_report,
        trigger="cron",
        day_of_week="sun",
        hour=WEEKLY_REPORT_HOUR,
        minute=0,
        args=[app],
        misfire_grace_time=3600,
    )
    scheduler.start()

    logger.info("🚀 DucFX Journal Bot started!")
    logger.info(f"🌐 HTTP server on port {PORT}")
    logger.info(f"📅 Weekly report: {WEEKLY_REPORT_DAY} at {WEEKLY_REPORT_HOUR}:00")

    # Run both: Telegram polling + HTTP server
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_all():
        # Start HTTP server
        web_app = web.Application()
        web_app.router.add_post("/trade", handle_trade_post)
        web_app.router.add_get("/", handle_health)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"✅ HTTP listening on 0.0.0.0:{PORT}")

        # Start Telegram bot
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Keep running
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            await runner.cleanup()

    loop.run_until_complete(run_all())

if __name__ == "__main__":
    main()
