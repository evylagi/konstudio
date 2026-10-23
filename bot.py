"""
Shopify Auto Checker — Telegram Bot
Long polling + plain text HTTP status. Host-agnostic.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, Document, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import (
    ADMIN_ID, BOT_TOKEN,
    MAX_CARDS_PER_CHECK, MAX_CONCURRENT_PER_USER,
    PROXIES,
)
from shopify_auto import shopify_auto_check


PORT = int(os.getenv("PORT", "8080"))
KNOWN_CHATS_FILE = os.getenv("KNOWN_CHATS_FILE", "known_chats.json")


def _load_known_chats():
    try:
        with open(KNOWN_CHATS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_known_chats(chats):
    try:
        with open(KNOWN_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(chats), f)
    except Exception:
        pass


KNOWN_CHATS = _load_known_chats()


async def announce_online(bot):
    targets = set(KNOWN_CHATS)
    if ADMIN_ID:
        targets.add(ADMIN_ID)
    if not targets:
        print("[boot] no chat targets — /start the bot once, or set ADMIN_ID")
        return
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, "Bot is online")
            print(f"[boot] announced to {chat_id}")
        except Exception as e:
            print(f"[boot] announce failed for {chat_id}: {e}")


class Flow(StatesGroup):
    waiting_store = State()
    waiting_cards = State()


@dataclass
class UserSession:
    store: Optional[str] = None
    cards: List[Tuple[str, str, str, str, str]] = field(default_factory=list)
    running: bool = False
    cancel_flag: bool = False
    proxy_index: int = 0
    hits: int = 0
    approved: int = 0
    declined: int = 0
    errors: int = 0
    total: int = 0
    done: int = 0
    started_at: float = 0.0
    product_cache: Optional[dict] = None
    token_cache: Optional[str] = None
    progress_msg_id: Optional[int] = None
    chat_id: Optional[int] = None


SESSIONS: Dict[int, UserSession] = {}


def get_session(user_id: int) -> UserSession:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = UserSession()
    return SESSIONS[user_id]


CARD_RE = re.compile(r"^\s*(\d{13,19})\s*[|:\s/]\s*(\d{1,2})\s*[|:\s/]\s*(\d{2,4})\s*[|:\s/]\s*(\d{3,4})\s*$")


def parse_card_line(line: str) -> Optional[Tuple[str, str, str, str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = CARD_RE.match(line)
    if not m:
        return None
    num, mon, yer, cvc = m.groups()
    if len(yer) == 2:
        yer = "20" + yer
    mon = mon.zfill(2)
    return (num, mon, yer, cvc, line)


def parse_cards_from_text(text: str) -> List[Tuple[str, str, str, str, str]]:
    out = []
    for line in text.splitlines():
        c = parse_card_line(line)
        if c:
            out.append(c)
    return out


def next_proxy(sess: UserSession) -> Optional[str]:
    if not PROXIES:
        return None
    p = PROXIES[sess.proxy_index % len(PROXIES)]
    sess.proxy_index += 1
    return p


def is_hit(result: str) -> bool:
    r = result.upper()
    return "APPROVED" in r or "CCN LIVE" in r or "INSUFFICIENT" in r


def classify(result: str) -> str:
    r = result.upper()
    if "APPROVED" in r:
        return "approved"
    if "CCN LIVE" in r or "INSUFFICIENT" in r or "CVV" in r or "3DS" in r:
        return "hits"
    if r.startswith("ERROR") or "TIMEOUT" in r or "RATE" in r:
        return "errors"
    return "declined"


def progress_bar(done: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "░" * width
    filled = int(width * done / total)
    return "█" * filled + "░" * (width - filled)


def fmt_progress(sess: UserSession) -> str:
    elapsed = time.time() - sess.started_at
    cpm = (sess.done / elapsed * 60) if elapsed > 0 and sess.done > 0 else 0
    pct = (sess.done / sess.total * 100) if sess.total else 0
    bar = progress_bar(sess.done, sess.total)
    store_short = sess.store.replace("https://", "").replace("http://", "").split("/")[0] if sess.store else "-"
    return (
        f"<b>⏳ Checking…</b>\n"
        f"<code>{store_short}</code>\n\n"
        f"<code>{bar}</code>  {sess.done}/{sess.total} ({pct:.0f}%)\n\n"
        f"✅ Approved  <b>{sess.approved}</b>\n"
        f"💎 Hits      <b>{sess.hits}</b>\n"
        f"❌ Declined  <b>{sess.declined}</b>\n"
        f"⚠️ Errors    <b>{sess.errors}</b>\n\n"
        f"⚡ {cpm:.1f} CPM  •  ⏱ {elapsed:.0f}s"
    )


def fmt_summary(sess: UserSession) -> str:
    elapsed = time.time() - sess.started_at
    store_short = sess.store.replace("https://", "").replace("http://", "").split("/")[0] if sess.store else "-"
    return (
        f"<b>✅ Check finished</b>\n"
        f"<code>{store_short}</code>\n\n"
        f"📦 Total    <b>{sess.total}</b>\n"
        f"✅ Approved <b>{sess.approved}</b>\n"
        f"💎 Hits     <b>{sess.hits}</b>\n"
        f"❌ Declined <b>{sess.declined}</b>\n"
        f"⚠️ Errors   <b>{sess.errors}</b>\n\n"
        f"⏱ {elapsed:.1f}s  •  ⚡ {(sess.total / elapsed * 60 if elapsed else 0):.1f} CPM"
    )


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 New check", callback_data="new")],
            [InlineKeyboardButton(text="🛑 Stop current", callback_data="stop")],
            [InlineKeyboardButton(text="ℹ️ Help", callback_data="help")],
        ]
    )


async def run_one(sess: UserSession, card: Tuple[str, str, str, str, str]) -> str:
    num, mon, yer, cvc, _raw = card
    result, _alive = await shopify_auto_check(
        sess.store, num, mon, yer, cvc,
        proxy=next_proxy(sess),
        cached_product=sess.product_cache,
        cached_token=sess.token_cache,
    )
    return result


async def process_cards(sess: UserSession, bot: Bot, user_id: int) -> None:
    sess.running = True
    sess.started_at = time.time()
    sess.total = len(sess.cards)
    sess.done = sess.approved = sess.hits = sess.declined = sess.errors = 0

    hits_buf: List[str] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT_PER_USER)
    last_edit = 0.0
    lock = asyncio.Lock()

    async def worker(card):
        if sess.cancel_flag:
            return
        async with sem:
            if sess.cancel_flag:
                return
            try:
                result = await run_one(sess, card)
            except Exception as e:
                result = f"Error: {str(e)[:50]}"
            cls = classify(result)
            async with lock:
                sess.done += 1
                if cls == "approved":
                    sess.approved += 1
                    sess.hits += 1
                    hits_buf.append(f"{card[4]} | {result}")
                elif cls == "hits":
                    sess.hits += 1
                    hits_buf.append(f"{card[4]} | {result}")
                elif cls == "errors":
                    sess.errors += 1
                else:
                    sess.declined += 1

    tasks = [asyncio.create_task(worker(c)) for c in sess.cards]

    while True:
        done_count = sum(1 for t in tasks if t.done())
        if done_count >= len(tasks) or sess.cancel_flag:
            break
        now = time.time()
        if sess.progress_msg_id and sess.chat_id and now - last_edit > 3.0:
            last_edit = now
            try:
                await bot.edit_message_text(
                    chat_id=sess.chat_id, message_id=sess.progress_msg_id,
                    text=fmt_progress(sess), parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        await asyncio.sleep(0.4)

    if sess.cancel_flag:
        for t in tasks:
            t.cancel()
    else:
        await asyncio.gather(*tasks, return_exceptions=True)

    sess.running = False

    if sess.progress_msg_id and sess.chat_id:
        try:
            await bot.edit_message_text(
                chat_id=sess.chat_id, message_id=sess.progress_msg_id,
                text=fmt_summary(sess), parse_mode=ParseMode.HTML,
                reply_markup=main_kb(),
            )
        except Exception:
            pass

    if hits_buf and sess.chat_id:
        fname = f"hits_{user_id}_{int(time.time())}.txt"
        path = f"/tmp/{fname}"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(hits_buf))
            await bot.send_document(
                chat_id=sess.chat_id,
                document=FSInputFile(path, filename=fname),
                caption=f"<b>💎 {len(hits_buf)} hit(s)</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    sess.cards.clear()
    sess.cancel_flag = False


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if msg.chat.id not in KNOWN_CHATS:
        KNOWN_CHATS.add(msg.chat.id)
        _save_known_chats(KNOWN_CHATS)
    await msg.reply(
        "<b>Shopify Auto Checker</b>\n\n"
        "Commands:\n"
        "• <code>/cc &lt;store&gt; &lt;card&gt;</code> — single check\n"
        "• <code>/chk</code> — bulk check (store + file)\n"
        "• <code>/stop</code> — cancel running check\n\n"
        "Card format: <code>num|mm|yyyy|cvv</code>\n\n"
        "Example:\n"
        "<code>/cc https://store.com 4111111111111111|12|2026|123</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "<b>How to use</b>\n\n"
        "<b>Single card</b>\n"
        "<code>/cc store.com card|mm|yyyy|cvv</code>\n\n"
        "<b>Bulk</b>\n"
        "1. <code>/chk</code>\n"
        "2. Send store URL\n"
        "3. Send .txt file or paste cards\n\n"
        "One card per line in the .txt file.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Flow.waiting_store)
    await cb.message.edit_text(
        "Send the <b>store URL</b>.\nExample: <code>https://example.com</code>",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@dp.callback_query(F.data == "stop")
async def cb_stop(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if sess.running:
        sess.cancel_flag = True
        await cb.answer("Stopping…", show_alert=False)
    else:
        await cb.answer("Nothing running.", show_alert=False)


@dp.message(Command("cc"))
async def cmd_cc(msg: Message, state: FSMContext):
    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await msg.reply(
            "Usage: <code>/cc &lt;store&gt; &lt;card&gt;</code>\n"
            "Example: <code>/cc store.com 4111111111111111|12|2026|123</code>",
            parse_mode=ParseMode.HTML,
        )
    _, store, card_raw = parts
    card = parse_card_line(card_raw)
    if not card:
        return await msg.reply("Bad card format. Use <code>num|mm|yyyy|cvv</code>", parse_mode=ParseMode.HTML)

    sess = get_session(msg.from_user.id)
    if not store.startswith("http"):
        store = "https://" + store
    sess.store = store
    sess.chat_id = msg.chat.id

    status = await msg.reply("<i>Checking…</i>", parse_mode=ParseMode.HTML)
    t0 = time.time()
    try:
        result, _alive = await shopify_auto_check(
            store, card[0], card[1], card[2], card[3],
            proxy=next_proxy(sess),
            cached_product=sess.product_cache,
            cached_token=sess.token_cache,
        )
    except Exception as e:
        result = f"Error: {str(e)[:80]}"
    elapsed = time.time() - t0

    emoji = "✅" if "APPROVED" in result.upper() else ("💎" if is_hit(result) else "❌")
    await status.edit_text(
        f"{emoji} <b>{result}</b>\n\n"
        f"<code>{card[4]}</code>\n"
        f"⏱ {elapsed:.1f}s",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(),
    )


@dp.message(Command("chk"))
async def cmd_chk(msg: Message, state: FSMContext):
    sess = get_session(msg.from_user.id)
    if sess.running:
        return await msg.reply("A check is already running. Use /stop first.")
    await state.clear()
    await state.set_state(Flow.waiting_store)
    await msg.reply(
        "Send the <b>store URL</b> for bulk check.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Flow.waiting_store)
async def on_store(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text:
        return await msg.reply("Send a URL.")
    if not text.startswith("http"):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return await msg.reply("Invalid URL. Try again.")
    sess = get_session(msg.from_user.id)
    sess.store = text
    sess.chat_id = msg.chat.id
    sess.product_cache = None
    sess.token_cache = None
    await state.set_state(Flow.waiting_cards)
    await msg.reply(
        f"Store set: <code>{parsed.netloc}</code>\n\n"
        f"Now send:\n"
        f"• a <b>.txt file</b> of cards, or\n"
        f"• <b>paste cards</b> as text (one per line)",
        parse_mode=ParseMode.HTML,
    )


async def _kick_off(msg: Message, state: FSMContext, cards_raw: str):
    sess = get_session(msg.from_user.id)
    cards = parse_cards_from_text(cards_raw)
    if not cards:
        return await msg.reply("No valid cards found.")
    if len(cards) > MAX_CARDS_PER_CHECK:
        return await msg.reply(f"Max {MAX_CARDS_PER_CHECK} cards per run. You sent {len(cards)}.")

    sess.cards = cards
    sess.hits = sess.approved = sess.declined = sess.errors = sess.done = 0
    sess.cancel_flag = False
    await state.clear()

    prog = await msg.reply(
        f"<b>Starting…</b>\nLoaded <b>{len(cards)}</b> cards.\nStore: <code>{sess.store}</code>",
        parse_mode=ParseMode.HTML,
    )
    sess.progress_msg_id = prog.message_id
    sess.chat_id = msg.chat.id

    asyncio.create_task(process_cards(sess, msg.bot, msg.from_user.id))


@dp.message(Flow.waiting_cards, F.document)
async def on_cards_doc(msg: Message, state: FSMContext, bot: Bot):
    doc: Document = msg.document
    if not (doc.file_name or "").lower().endswith((".txt", ".csv")):
        return await msg.reply("Send a .txt or .csv file.")
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        return await msg.reply("File too big (max 5MB).")
    try:
        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        raw = buf.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return await msg.reply(f"Could not read file: {e}")
    await _kick_off(msg, state, raw)


@dp.message(Flow.waiting_cards, F.text)
async def on_cards_text(msg: Message, state: FSMContext):
    await _kick_off(msg, state, msg.text or "")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    sess = get_session(msg.from_user.id)
    if sess.running:
        sess.cancel_flag = True
        await msg.reply("🛑 Stopping…")
    else:
        await msg.reply("Nothing running.")


async def index(request: web.Request) -> web.Response:
    return web.Response(text="Bot is running", content_type="text/plain")


async def ping(request: web.Request) -> web.Response:
    return web.Response(text="pong", content_type="text/plain")


async def main():
    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ping", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"[boot] http server on :{PORT}")
    print("[boot] polling started")

    await announce_online(bot)

    await polling_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
