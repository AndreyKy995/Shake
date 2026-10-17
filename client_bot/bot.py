"""Telegram-бот: ИИ-менеджер общается с клиентами, владельцу приходят только нужные.

Запуск: python bot.py (настройки в .env, описание бизнеса в business.md).
"""

import asyncio
import html
import logging
import os
from collections import defaultdict
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from ai import AIManager, Decision
from storage import Client, Storage

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID") or 0)
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "low")
GREETING = os.environ.get("GREETING", "Здравствуйте! Напишите, пожалуйста, чем могу помочь 🙂")
BASE = Path(__file__).parent
BUSINESS = (BASE / os.environ.get("BUSINESS_FILE", "business.md")).read_text(encoding="utf-8")

CHANNEL = "telegram"
STATUS_TITLES = {
    "hot": "🔥 Горячий клиент",
    "human": "🙋 Нужен живой человек",
    "dialog": "💬 В диалоге с ботом",
    "not_target": "🚫 Не целевой",
    "spam": "🗑 Спам",
}

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
store = Storage(str(BASE / os.environ.get("DB_PATH", "bot.db")))
manager = AIManager(BUSINESS, MODEL, EFFORT)
locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

is_owner = F.chat.id == OWNER_CHAT_ID


# ---------- владелец ----------


@dp.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(f"Ваш chat id: <code>{message.chat.id}</code>\nВпишите его в OWNER_CHAT_ID в файле .env")


@dp.message(is_owner, Command("start", "help"))
async def owner_help(message: Message):
    await message.answer(
        "Вы — владелец. Сюда приходят только клиенты, которым нужны вы.\n\n"
        "• Ответьте (reply) на карточку клиента — текст уйдёт клиенту, бот в этом диалоге замолчит.\n"
        "• Кнопка «🤖 Вернуть боту» — бот снова ведёт диалог.\n"
        "• /leads — последние горячие клиенты\n"
        "• /stats — статистика"
    )


@dp.message(is_owner, Command("leads"))
async def owner_leads(message: Message):
    leads = store.leads(("hot", "human"))
    if not leads:
        await message.answer("Пока нет клиентов, которым нужны вы.")
        return
    for client in leads:
        await send_card(client, client.summary, header=STATUS_TITLES[client.status])


@dp.message(is_owner, Command("stats"))
async def owner_stats(message: Message):
    stats = store.stats()
    lines = [f"{STATUS_TITLES.get(s, s)}: {n}" for s, n in stats.items()] or ["Пока пусто"]
    await message.answer("\n".join(lines))


@dp.message(is_owner, F.reply_to_message)
async def owner_reply(message: Message):
    target = store.card_client(message.reply_to_message.message_id)
    if target is None:
        await message.answer("Это не карточка клиента. Ответьте (reply) именно на сообщение с клиентом.")
        return
    channel, user_id = target
    await message.copy_to(int(user_id))
    store.add_message(channel, user_id, "owner", message.text or message.caption or "[вложение]")
    store.update(channel, user_id, mode="human")
    await message.answer("✅ Отправлено. Бот в этом диалоге молчит, пока не нажмёте «🤖 Вернуть боту».")


@dp.message(is_owner)
async def owner_other(message: Message):
    await message.answer("Чтобы написать клиенту, ответьте (reply) на его карточку. /help — подсказка.")


@dp.callback_query(F.data.startswith("mode:"))
async def switch_mode(callback: CallbackQuery):
    if callback.from_user.id != OWNER_CHAT_ID:
        await callback.answer()
        return
    _, mode, channel, user_id = callback.data.split(":", 3)
    store.update(channel, user_id, mode=mode)
    await callback.answer("Диалог ваш — бот молчит" if mode == "human" else "Бот снова ведёт диалог")


# ---------- клиенты ----------


@dp.message(CommandStart())
async def client_start(message: Message):
    client = remember(message)
    store.add_message(CHANNEL, client.user_id, "assistant", GREETING)
    await message.answer(GREETING)


@dp.message(F.chat.type == "private")
async def client_message(message: Message):
    client = remember(message)
    async with locks[client.user_id]:
        client = store.get(CHANNEL, client.user_id)
        text = message.text or message.caption
        if not text:
            # Голосовые, фото, файлы ИИ пока не читает — отдаём владельцу как есть.
            await notify_owner(client, header="📎 Клиент прислал вложение", forward=message)
            if client.mode == "bot":
                await message.answer("Спасибо! Передал специалисту, он посмотрит и ответит.")
            return

        store.add_message(CHANNEL, client.user_id, "user", text)

        if client.mode == "human":
            await notify_owner(client, header="✉️ Новое сообщение", last_text=text)
            return

        try:
            decision = await manager.decide(store.history(CHANNEL, client.user_id))
        except Exception:
            log.exception("AI failed for %s", client.user_id)
            decision = Decision(
                "Спасибо за сообщение! Специалист скоро ответит.",
                "human",
                "Бот не смог обработать сообщение (ошибка ИИ) — ответьте клиенту сами.",
                "",
                "",
            )

        if decision.reply:
            store.add_message(CHANNEL, client.user_id, "assistant", decision.reply)
            await message.answer(html.escape(decision.reply))

        was = client.status
        store.update(
            CHANNEL,
            client.user_id,
            status=decision.status,
            summary=decision.summary,
            contact=decision.contact or client.contact,
            name=decision.client_name or client.name,
        )
        client = store.get(CHANNEL, client.user_id)

        if decision.status in ("hot", "human") and decision.status != was:
            await notify_owner(client, header=STATUS_TITLES[decision.status], last_text=text)
        elif was in ("hot", "human"):
            # Клиент уже у владельца на примете — держим его в курсе дальнейшей переписки.
            await notify_owner(client, header="✉️ Клиент пишет дальше", last_text=text)


def remember(message: Message) -> Client:
    user = message.from_user
    return store.get_or_create(CHANNEL, str(user.id), user.full_name, user.username or "")


async def notify_owner(client: Client, header: str, last_text: str = "", forward: Message | None = None):
    if not OWNER_CHAT_ID:
        log.warning("OWNER_CHAT_ID не задан — некому отправить клиента %s", client.user_id)
        return
    if forward is not None:
        await forward.forward(OWNER_CHAT_ID)
    await send_card(client, client.summary, header=header, last_text=last_text)


async def send_card(client: Client, summary: str, header: str, last_text: str = ""):
    name = html.escape(client.name or "Без имени")
    who = f'<a href="tg://user?id={client.user_id}">{name}</a>'
    if client.username:
        who += f" (@{html.escape(client.username)})"
    lines = [f"<b>{header}</b>", who]
    if client.contact:
        lines.append(f"📞 {html.escape(client.contact)}")
    if summary:
        lines.append(f"\n{html.escape(summary)}")
    if last_text:
        lines.append(f"\n<i>Последнее сообщение:</i> {html.escape(last_text[:1000])}")
    lines.append("\n↩️ Ответьте на это сообщение — текст уйдёт клиенту.")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✋ Беру диалог", callback_data=f"mode:human:{client.channel}:{client.user_id}"),
                InlineKeyboardButton(text="🤖 Вернуть боту", callback_data=f"mode:bot:{client.channel}:{client.user_id}"),
            ]
        ]
    )
    sent = await bot.send_message(OWNER_CHAT_ID, "\n".join(lines), reply_markup=keyboard)
    store.save_card(sent.message_id, client.channel, client.user_id)


async def main():
    if not OWNER_CHAT_ID:
        log.warning("OWNER_CHAT_ID пуст: напишите боту /myid и впишите число в .env")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
