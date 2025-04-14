# -*- coding: utf-8 -*-

# =============================================================================
# ВАЖНО! ПРОБЛЕМЫ С ВЕРСИЕЙ AIOGRAM!
# =============================================================================
# Ошибки 'AttributeError' (как 'delete_messages') ОЗНАЧАЮТ НЕВЕРНУЮ ВЕРСИЮ.
# РЕКОМЕНДУЕТСЯ: pip install --force-reinstall aiogram==2.25.1
#
# В ЭТОМ КОДЕ ПРИМЕНЕН МЕДЛЕННЫЙ ОБХОДНОЙ ПУТЬ ДЛЯ ОШИБКИ 'delete_messages':
# Сообщения удаляются ПО ОДНОМУ. Это НЕЭФФЕКТИВНО.
# =============================================================================

import logging
import datetime
import random
import re
import os
import csv
import asyncio
from threading import Lock
from aiogram import Bot, Dispatcher, executor, types
# Убраны UserNotParticipant и CantRestrictChatMember для совместимости
from aiogram.utils.exceptions import (
    BadRequest, ChatAdminRequired, BotKicked, BotBlocked, CantParseEntities,
    MessageToDeleteNotFound, MessageCantBeDeleted, Throttled,
    UserIsAnAdministratorOfTheChat
)
from aiogram.types import ChatPermissions, ParseMode, ChatMemberStatus, BotCommand
from aiogram.dispatcher.filters import BoundFilter

# --- Настройки ---
API_TOKEN = '7888476635:AAF47KT6ASYY2HTmn3LNOavz0JbX_ofx9xE'  # <<<=== ВАШ ТОКЕН БОТА
STATS_FILE = 'user_stats.csv'
REPORTS_FILE = 'reports.csv'
CLEANUP_FILE = 'cleanup_messages.csv'
STATS_FIELDNAMES = ['user_id', 'chat_id', 'mute_count', 'report_count']
REPORTS_FIELDNAMES = ['chat_id', 'reporter_id', 'reported_id', 'reason', 'report_time']
CLEANUP_FIELDNAMES = ['chat_id', 'message_id', 'added_time']
DEFAULT_MUTE_DURATION_MINUTES = 30
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60 # 24 часа
PURGE_MESSAGE_LIMIT = 100

# Список матерных слов
SWEAR_WORDS = {'мат1', 'матюк', 'плохоеслово', 'ругательство', 'блять', 'пиздец', 'хуй'}

# Фразы для команды !бу
BU_PHRASES = [
    "Хватит! 🛑", "Страшна вырубай 😨", "Сам ты б/у 😏", "пон 👌", "Бугага! 👻",
    "Не мешай мне делать сложные компьютерные вычисления 💻🤔", "Не смешно 😒",
    "Ладно ... ✨", "Не пугай так! 👀"
]

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log = logging.getLogger('moderator_bot')

# Инициализация бота и диспетчера
try:
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(bot)
    log.info("Bot and Dispatcher initialized.")
except ValueError: log.critical("Ошибка: Неверный формат токена!"); exit()
except Exception as e: log.critical(f"Не удалось инициализировать бота: {e}"); exit()

file_lock = Lock()

# --- Фильтр для проверки создателя чата ---
class IsChatCreatorFilter(BoundFilter):
    key = 'is_chat_creator'
    def __init__(self, is_chat_creator): self.is_chat_creator = is_chat_creator
    async def check(self, message: types.Message) -> bool:
        if message.chat.type == types.ChatType.PRIVATE: return False
        try: member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        except BadRequest: return False
        is_creator = member.status == ChatMemberStatus.CREATOR
        return self.is_chat_creator == is_creator

dp.filters_factory.bind(IsChatCreatorFilter)

# --- Функции для работы с CSV файлами (без изменений) ---
def initialize_data_files():
    with file_lock:
        for filename, fieldnames in [(STATS_FILE, STATS_FIELDNAMES), (REPORTS_FILE, REPORTS_FIELDNAMES), (CLEANUP_FILE, CLEANUP_FIELDNAMES)]:
            rewrite_header = False
            if not os.path.exists(filename): log.info(f"Файл {filename} не найден, создаю..."); rewrite_header = True
            else:
                try:
                    with open(filename, 'r', newline='', encoding='utf-8') as f:
                        reader = csv.reader(f); header = next(reader, None)
                        if not header or list(header) != list(fieldnames): log.warning(f"Файл {filename} неверный заголовок. Перезаписываю."); rewrite_header = True
                except StopIteration: log.warning(f"Файл {filename} пуст. Перезаписываю."); rewrite_header = True
                except Exception as e: log.error(f"Ошибка проверки {filename}: {e}. Перезаписываю."); rewrite_header = True
            if rewrite_header:
                try:
                    with open(filename, 'w', newline='', encoding='utf-8') as f: writer = csv.DictWriter(f, fieldnames=fieldnames); writer.writeheader()
                    log.info(f"Файл {filename} успешно создан/заголовок перезаписан.")
                except IOError as e: log.error(f"Критическая ошибка: Не удалось создать/перезаписать {filename}: {e}")

def _read_csv_data(filename: str, fieldnames: list) -> list[dict]:
    data = []
    if not os.path.exists(filename): log.warning(f"{filename} не найден при чтении."); initialize_data_files(); return data
    try:
        with open(filename, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None: log.warning(f"Файл {filename} пуст или без данных."); return []
            if list(reader.fieldnames) != fieldnames: log.error(f"Несоотв. заголовков в {filename}!"); initialize_data_files(); return []
            for i, row in enumerate(reader):
                if not row: log.warning(f"Пустая строка #{i+1} в {filename}"); continue
                clean_row = {str(k).strip(): str(v).strip() for k, v in row.items() if k is not None}
                valid = all(field in clean_row for field in fieldnames)
                if valid: data.append(clean_row)
                else: log.warning(f"Пропуск строки #{i+1} с неполными полями в {filename}: {clean_row}")
            return data
    except FileNotFoundError: log.warning(f"{filename} не найден (повторно)."); initialize_data_files(); return []
    except Exception as e: log.error(f"Критическая ошибка чтения {filename}: {e}"); return []

def _write_csv_data(filename: str, fieldnames: list, data: list[dict]):
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames); writer.writeheader()
            validated_data = [{field: row.get(field, '') for field in fieldnames} for row in data]
            writer.writerows(validated_data)
    except IOError as e: log.error(f"Ошибка записи в {filename}: {e}")
    except Exception as e: log.error(f"Непредвиденная ошибка записи в {filename}: {e}")

def add_mute(user_id: int, chat_id: int): # ... (без изменений) ...
    with file_lock:
        stats_data = _read_csv_data(STATS_FILE, STATS_FIELDNAMES); updated = False; user_id_str = str(user_id); chat_id_str = str(chat_id)
        for row in stats_data:
            if row.get('user_id') == user_id_str and row.get('chat_id') == chat_id_str:
                try: row['mute_count'] = str(int(row.get('mute_count', 0)) + 1); updated = True
                except (ValueError, TypeError): row['mute_count'] = '1'; updated = True; break
        if not updated: stats_data.append({'user_id': user_id_str, 'chat_id': chat_id_str, 'mute_count': '1', 'report_count': '0'})
        _write_csv_data(STATS_FILE, STATS_FIELDNAMES, stats_data); log.info(f"Статистика мутов для {user_id} в {chat_id} обновлена")

def add_report(reporter_id: int, reported_id: int, chat_id: int, reason: str): # ... (без изменений) ...
    report_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'); chat_id_str = str(chat_id); reporter_id_str = str(reporter_id); reported_id_str = str(reported_id)
    new_report = {'chat_id': chat_id_str, 'reporter_id': reporter_id_str, 'reported_id': reported_id_str, 'reason': reason, 'report_time': report_time}
    with file_lock:
        try:
            file_exists = os.path.exists(REPORTS_FILE); needs_header = not file_exists or os.path.getsize(REPORTS_FILE) == 0
            with open(REPORTS_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=REPORTS_FIELDNAMES);
                if needs_header: writer.writeheader()
                writer.writerow(new_report)
            log.info(f"Репорт добавлен в {REPORTS_FILE}")
        except IOError as e: log.error(f"Ошибка добавления репорта в {REPORTS_FILE}: {e}")
        stats_data = _read_csv_data(STATS_FILE, STATS_FIELDNAMES); updated = False
        for row in stats_data:
            if row.get('user_id') == reported_id_str and row.get('chat_id') == chat_id_str:
                try: row['report_count'] = str(int(row.get('report_count', 0)) + 1); updated = True
                except (ValueError, TypeError): row['report_count'] = '1'; updated = True; break
        if not updated: stats_data.append({'user_id': reported_id_str, 'chat_id': chat_id_str, 'mute_count': '0', 'report_count': '1'})
        _write_csv_data(STATS_FILE, STATS_FIELDNAMES, stats_data); log.info(f"Статистика репортов для {reported_id} в {chat_id} обновлена")

def get_user_stats(user_id: int, chat_id: int) -> tuple[int, int]: # ... (без изменений) ...
    with file_lock:
        stats_data = _read_csv_data(STATS_FILE, STATS_FIELDNAMES); user_id_str = str(user_id); chat_id_str = str(chat_id)
        for row in stats_data:
            if row.get('user_id') == user_id_str and row.get('chat_id') == chat_id_str:
                try: return int(row.get('mute_count', 0)), int(row.get('report_count', 0))
                except (ValueError, TypeError): log.error(f"Некорректные данные в стате {user_id}/{chat_id}: {row}"); return 0, 0
        return (0, 0)

def get_reports_for_user(user_id: int, chat_id: int) -> list[dict]: # ... (без изменений) ...
    reports_found = []; user_id_str = str(user_id); chat_id_str = str(chat_id)
    with file_lock:
        all_reports = _read_csv_data(REPORTS_FILE, REPORTS_FIELDNAMES)
        for report in all_reports:
            if report.get('reported_id') == user_id_str and report.get('chat_id') == chat_id_str: reports_found.append(report)
    try: reports_found.sort(key=lambda r: r.get('report_time', '0'), reverse=True)
    except Exception as e: log.warning(f"Ошибка сортировки репортов: {e}")
    return reports_found

def add_message_to_cleanup(chat_id: int, message_id: int): # ... (без изменений) ...
    added_time = datetime.datetime.now().isoformat()
    new_cleanup_entry = {'chat_id': str(chat_id), 'message_id': str(message_id), 'added_time': added_time}
    with file_lock:
        try:
            file_exists = os.path.exists(CLEANUP_FILE); needs_header = not file_exists or os.path.getsize(CLEANUP_FILE) == 0
            with open(CLEANUP_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=CLEANUP_FIELDNAMES);
                if needs_header: writer.writeheader()
                writer.writerow(new_cleanup_entry)
            log.debug(f"Сообщение {message_id} в {chat_id} добавлено в список очистки.")
        except IOError as e: log.error(f"Ошибка добавления сообщения в {CLEANUP_FILE}: {e}")

# !!! ИЗМЕНЕНО: Используем delete_message вместо delete_messages !!!
async def perform_cleanup():
    """Читает файл очистки, удаляет сообщения ПО ОДНОМУ и очищает файл."""
    log.info("Запуск очистки сервисных сообщений (по одному)...")
    messages_to_delete = []
    with file_lock:
        messages_to_delete = _read_csv_data(CLEANUP_FILE, CLEANUP_FIELDNAMES)
        _write_csv_data(CLEANUP_FILE, CLEANUP_FIELDNAMES, []) # Очищаем файл
        log.info(f"Прочитано {len(messages_to_delete)} сообщений для удаления. Файл {CLEANUP_FILE} очищен.")

    deleted_count = 0
    failed_count = 0
    for msg_data in messages_to_delete:
        try:
            chat_id = int(msg_data['chat_id'])
            message_id = int(msg_data['message_id'])
            await bot.delete_message(chat_id, message_id) # <<<--- ИСПОЛЬЗУЕМ delete_message
            log.debug(f"Удалено сообщение {message_id} из чата {chat_id}")
            deleted_count += 1
            await asyncio.sleep(0.3) # !!! Увеличили паузу, т.к. удаляем по одному !!!
        except (MessageToDeleteNotFound, MessageCantBeDeleted):
            # Эти ошибки ожидаемы для старых сообщений
            log.debug(f"Не удалось удалить {message_id} из {chat_id} (не найдено/нельзя удалить).")
            failed_count +=1
        except BadRequest as e:
            log.warning(f"Ошибка BadRequest при удалении {message_id} из {chat_id}: {e}")
            failed_count += 1
        except (ValueError, TypeError):
            log.error(f"Некорректные данные в файле очистки: {msg_data}")
            failed_count += 1
        except Throttled as e:
            log.warning(f"Превышен лимит при удалении {message_id} из {chat_id}: {e}. Ждем {e.timeout} сек.")
            await asyncio.sleep(e.timeout or 1.5) # Ждем указанное время или 1.5 сек
            # Повторная попытка (опционально, может затянуть очистку)
            try:
                await bot.delete_message(chat_id, message_id)
                deleted_count += 1
            except Exception: failed_count += 1 # Если и вторая попытка не удалась
        except Exception as e:
            log.error(f"Неизвестная ошибка при удалении {message_id} из {chat_id}: {e}")
            failed_count += 1

    log.info(f"Очистка завершена. Удалено: {deleted_count}, Ошибок: {failed_count}.")
    return deleted_count, failed_count

async def cleanup_scheduler(): # ... (без изменений) ...
    log.info("Планировщик очистки запущен.")
    while True:
        try: await asyncio.sleep(CLEANUP_INTERVAL_SECONDS); log.info("Время для плановой очистки..."); await perform_cleanup()
        except asyncio.CancelledError: log.info("Планировщик очистки остановлен."); break
        except Exception as e: log.error(f"Ошибка в планировщике очистки: {e}"); await asyncio.sleep(60)

def parse_duration(time_str: str) -> datetime.timedelta | None: # ... (без изменений) ...
    match = re.match(r"(\d+)\s+(минут[ауы]?|час[аов]?|ден[ь|я|ей]?|дн[я|ей]?|недел[яиь]?)$", time_str.strip().lower());
    if not match: return None
    try: value = int(match.group(1)); unit = match.group(2)
    except ValueError: log.warning(f"Ошибка парсинга времени: {match.group(1)}"); return None
    if "минут" in unit: return datetime.timedelta(minutes=value)
    if "час" in unit: return datetime.timedelta(hours=value)
    if "ден" in unit or "дн" in unit: return datetime.timedelta(days=value)
    if "недел" in unit: return datetime.timedelta(weeks=value)
    return None

async def is_user_admin(chat_id: int, user_id: int) -> bool: # ... (без изменений) ...
    try: member = await bot.get_chat_member(chat_id, user_id); return member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]
    except BadRequest as e:
        if "user not found" not in str(e).lower(): log.warning(f"Ошибка проверки админа {user_id} в {chat_id}: {e}")
    except Exception as e: log.error(f"Неизвестная ошибка проверки админа {user_id}: {e}")
    return False

async def safe_reply(message: types.Message, text: str, parse_mode: str | None = None, add_to_cleanup: bool = False, **kwargs) -> types.Message | None: # ... (без изменений) ...
    sent_message = None
    try:
        sent_message = await message.reply(text, parse_mode=parse_mode, **kwargs)
        if add_to_cleanup and sent_message: add_message_to_cleanup(sent_message.chat.id, sent_message.message_id)
    except CantParseEntities as e:
        log.error(f"Ошибка парсинга entities: {e}. Отправляю текст.")
        try:
            clean_text = re.sub('<[^>]+>', '', text)
            sent_message = await message.reply(clean_text, **kwargs)
            if add_to_cleanup and sent_message: add_message_to_cleanup(sent_message.chat.id, sent_message.message_id)
        except Exception as ex: log.error(f"Не удалось отправить даже чистый текст: {ex}")
    except Exception as e: log.error(f"Неизвестная ошибка при отправке reply: {e}")
    return sent_message

# --- Обработчики команд !помощь, /help, /start ---
@dp.message_handler(commands=['start', 'help'])
async def send_welcome_cmd(message: types.Message): await send_welcome_text(message)

@dp.message_handler(lambda message: message.text.lower() == '!помощь')
async def send_welcome_text_cmd(message: types.Message): await send_welcome_text(message)

async def send_welcome_text(message: types.Message): # ... (текст помощи без изменений) ...
    help_text = (
        "👋 Привет! Я твой верный помощник-модератор! 🤖\n\n"
        "📜 <b>Вот что я умею:</b>\n"
        "  <code>!мут [время] [причина]</code> - 🤫 Заглушить болтуна (ответом).\n"
        "     <i>(Без аргументов - мут на {DEFAULT_MUTE_DURATION_MINUTES} мин)</i>\n"
        "     <i>Пример:</i> <code>!мут 1 час флуд</code>\n\n"
        "  <code>!размут</code> - 😊 Вернуть голос (ответом).\n\n"
        "  <code>!бан <время> [причина]</code> - 🚫 Отправить в банхаммер (ответом).\n"
        "     <i>Пример:</i> <code>!бан 1 день спам</code>\n\n"
        "  <code>!разбан</code> - ✅ Выпустить из бана (ответом).\n\n"
        "  <code>!репорт [причина]</code> - 📢 Пожаловаться админам (ответом).\n"
        "     <i>Пример:</i> <code>!репорт агрессия</code>\n\n"
        "  <code>!репорты</code> - 🧐 Посмотреть свои грешки (жалобы на вас).\n\n"
        "  <code>!я</code> - 📊 Твоя личная статистика (муты/репорты).\n\n"
        "  <code>!бу</code> - 👻 Сказать что-нибудь эдакое...\n\n"
        "  <code>!чистка N</code> или <code>/purge N</code> - 🧹 Удалить N последних сообщений (создатель).\n\n"
        "  <code>!админ</code> или <code>/promote</code> - 👑 Назначить админа (ответом, создатель).\n\n"
        "  <code>!сброс</code> - 🗑️ Удалить мои сервисные сообщения (админы).\n\n"
        "🗑️ И да, я не люблю мат и удаляю его!"
    ).format(DEFAULT_MUTE_DURATION_MINUTES=DEFAULT_MUTE_DURATION_MINUTES)
    await safe_reply(message, help_text, parse_mode=ParseMode.HTML, add_to_cleanup=False)

# --- Обработчики команд !мут, !размут, !бан, !разбан, !репорт, !репорты, !я, !бу ---
# (Код этих команд остается таким же, как в предыдущей версии, включая safe_reply)
@dp.message_handler(lambda message: message.text.lower().startswith('!мут'))
async def mute_user(message: types.Message):
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи сообщение **ответом**.", add_to_cleanup=True); return
    if not await is_user_admin(message.chat.id, message.from_user.id): await safe_reply(message, "🚫 Только для админов.", add_to_cleanup=True); return
    is_bot_admin = False; can_restrict_real = False
    try:
        bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if hasattr(bot_member, 'can_restrict_members'): can_restrict_real = bot_member.can_restrict_members
        else: can_restrict_real = is_bot_admin
    except (ChatAdminRequired, BotKicked, BotBlocked): await safe_reply(message, "🤖 Я не админ или заблокирован.", add_to_cleanup=True); return
    except Exception as e: log.error(f"Ошибка проверки прав (мут): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not can_restrict_real: await safe_reply(message, "🤖 Нет прав на ограничение.", add_to_cleanup=True); return
    log.info(f"Bot status in chat {message.chat.id} for mute: {bot_member.status}.")
    target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user
    if target_user_id == bot.id: await safe_reply(message, "😅 Себя мутить? Зачем?", add_to_cleanup=True); return
    if await is_user_admin(message.chat.id, target_user_id): await safe_reply(message, "🛡️ Админов мутить нельзя!", add_to_cleanup=True); return
    command_text = message.text.strip(); args_text = command_text[len('!мут'):].strip()
    duration = None; reason = "Причина не указана"; duration_str_parsed = ""
    if not args_text: duration = datetime.timedelta(minutes=DEFAULT_MUTE_DURATION_MINUTES); duration_str_parsed = f"{DEFAULT_MUTE_DURATION_MINUTES} мин (стандарт)"; log.info(f"Мут по умолчанию на {DEFAULT_MUTE_DURATION_MINUTES} мин.")
    else:
        match_duration = re.match(r"(\d+\s+(?:минут[ауы]?|час[аов]?|ден[ь|я|ей]?|дн[я|ей]?|недел[яиь]?))\s*(.*)", args_text, re.IGNORECASE | re.DOTALL)
        if match_duration: duration_str_parsed = match_duration.group(1).strip(); duration = parse_duration(duration_str_parsed); reason_candidate = match_duration.group(2).strip();
        if reason_candidate: reason = reason_candidate
        else: error_message = ("❓ Формат: <code>!мут <время> [причина]</code> или <code>!мут</code>"); await safe_reply(message, error_message, parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    if duration is None: await safe_reply(message, f"⏳ Не понял время '<code>{duration_str_parsed}</code>'.", parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    mute_until_dt = datetime.datetime.now() + duration; mute_until_ts = int(mute_until_dt.timestamp())
    try:
        await bot.restrict_chat_member(message.chat.id, target_user_id, permissions=ChatPermissions(can_send_messages=False), until_date=mute_until_ts)
        add_mute(target_user_id, message.chat.id); log.info(f"User {target_user_id} muted until {mute_until_dt} by {message.from_user.id}. R: {reason}")
        response_text = (f"✅🤫 {target_user_info.get_mention(as_html=True)} замучен на <b>{duration_str_parsed}</b>.\n<b>Причина:</b> {reason}\n<b>До:</b> {mute_until_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        await safe_reply(message, response_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except BadRequest as e: # Убрали CantRestrictChatMember
        log.error(f"Ошибка мута (API) {target_user_id}: {e}"); err_text = f"❌ Ошибка API: {e}"
        if isinstance(e, UserIsAnAdministratorOfTheChat) or "user is an administrator" in str(e): err_text = "🛡️ Нельзя мутить админа."
        elif "chat member status unchanged" in str(e): err_text = f"🤷 Статус {target_user_info.get_mention(as_html=True)} не изменился."
        elif "can't restrict self" in str(e).lower(): err_text = "😅 Себя мутить? Зачем?"
        await safe_reply(message, err_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except Exception as e: log.error(f"Неизвестная ошибка мута {target_user_id}: {e}"); await safe_reply(message, "💥 Ошибка при муте.", add_to_cleanup=True)

@dp.message_handler(lambda message: message.text.lower() == '!размут')
async def unmute_user(message: types.Message): # ... (код как в пред. версии) ...
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи сообщение **ответом**.", add_to_cleanup=True); return
    if not await is_user_admin(message.chat.id, message.from_user.id): await safe_reply(message, "🚫 Только для админов.", add_to_cleanup=True); return
    is_bot_admin = False
    try: bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
    except (ChatAdminRequired, BotKicked, BotBlocked): await safe_reply(message, "🤖 Я не администратор.", add_to_cleanup=True); return
    except Exception as e: log.error(f"Ошибка проверки прав (размут): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not is_bot_admin: await safe_reply(message, "🤖 Нужны права админа.", add_to_cleanup=True); return
    log.info(f"Bot status in chat {message.chat.id} for unmute: {bot_member.status}.")
    target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await bot.restrict_chat_member(message.chat.id, target_user_id, permissions=perms, until_date=0)
        log.info(f"User {target_user_id} unmuted by {message.from_user.id}.")
        await safe_reply(message, f"✅🗣️ {target_user_info.get_mention(as_html=True)} снова может говорить!", parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except BadRequest as e:
         err_text = f"❌ Ошибка API: {e}"
         if "chat member status unchanged" in str(e): err_text = f"🤷 {target_user_info.get_mention(as_html=True)} и так не был ограничен."
         elif "user is an administrator" in str(e): err_text = f"🛡️ Админы не бывают в муте."
         elif "user not found" in str(e): err_text = f"❓ {target_user_info.get_mention(as_html=True)} не найден."
         elif "user not participant" in str(e).lower(): err_text = f"🚶 {target_user_info.get_mention(as_html=True)} не в чате."
         log.error(f"Ошибка размута (BadRequest) {target_user_id}: {e}");
         await safe_reply(message, err_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except Exception as e: log.error(f"Неизвестная ошибка размута {target_user_id}: {e}"); await safe_reply(message, "💥 Ой! Ошибка при размуте.", add_to_cleanup=True)

@dp.message_handler(lambda message: message.text.lower().startswith('!бан'))
async def ban_user(message: types.Message): # ... (код как в пред. версии) ...
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи сообщение **ответом**.", add_to_cleanup=True); return
    if not await is_user_admin(message.chat.id, message.from_user.id): await safe_reply(message, "🚫 Только для админов.", add_to_cleanup=True); return
    is_bot_admin = False
    try: bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
    except (ChatAdminRequired, BotKicked, BotBlocked): await safe_reply(message, "🤖 Я не администратор.", add_to_cleanup=True); return
    except Exception as e: log.error(f"Ошибка проверки прав (бан): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not is_bot_admin: await safe_reply(message, "🤖 Нужны права админа.", add_to_cleanup=True); return
    log.info(f"Bot status in chat {message.chat.id} for ban: {bot_member.status}.")
    target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user
    if target_user_id == bot.id: await safe_reply(message, "😅 Себя банить не буду.", add_to_cleanup=True); return
    if await is_user_admin(message.chat.id, target_user_id): await safe_reply(message, "🛡️ Нельзя банить админа.", add_to_cleanup=True); return
    command_parts = message.text.split(maxsplit=1); args_text = command_parts[1].strip() if len(command_parts) > 1 else ""
    duration, reason, duration_str_parsed = None, "Причина не указана", ""
    match_duration = re.match(r"(\d+\s+(?:минут[ауы]?|час[аов]?|ден[ь|я|ей]?|дн[я|ей]?|недел[яиь]?))\s*(.*)", args_text, re.IGNORECASE | re.DOTALL)
    if match_duration:
        duration_str_parsed = match_duration.group(1).strip(); duration = parse_duration(duration_str_parsed)
        reason_candidate = match_duration.group(2).strip();
        if reason_candidate: reason = reason_candidate
    else:
         error_message = ("❓ Формат: <code>!бан <время> [причина]</code>\n<i>Пример:</i> <code>!бан 7 дней спам</code>")
         await safe_reply(message, error_message, parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    if duration is None: await safe_reply(message, f"⏳ Не понял время '<code>{duration_str_parsed}</code>'.", parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    ban_until_dt = datetime.datetime.now() + duration; ban_until_ts = int(ban_until_dt.timestamp())
    try:
        await bot.kick_chat_member(message.chat.id, target_user_id, until_date=ban_until_ts)
        log.info(f"User {target_user_id} banned until {ban_until_dt} by {message.from_user.id}. R: {reason}")
        response_text = (f"✅🚫 {target_user_info.get_mention(as_html=True)} забанен на <b>{duration_str_parsed}</b>.\n<b>Причина:</b> {reason}\n<b>До:</b> {ban_until_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        await safe_reply(message, response_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except BadRequest as e:
        log.error(f"Ошибка бана (BadRequest) {target_user_id}: {e}"); err_text = f"❌ Ошибка API: {e}"
        if "user is an administrator" in str(e): err_text = "🛡️ Нельзя банить админа."
        elif "user not found" in str(e): err_text = f"❓ {target_user_info.get_mention(as_html=True)} не найден."
        elif "user not participant" in str(e).lower(): err_text = f"🚶 {target_user_info.get_mention(as_html=True)} не участник."
        await safe_reply(message, err_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except Exception as e: log.error(f"Неизвестная ошибка бана {target_user_id}: {e}"); await safe_reply(message, "💥 Ой! Ошибка при бане.", add_to_cleanup=True)

@dp.message_handler(lambda message: message.text.lower() == '!разбан')
async def unban_user(message: types.Message): # ... (код как в пред. версии) ...
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи сообщение **ответом**.", add_to_cleanup=True); return
    if not await is_user_admin(message.chat.id, message.from_user.id): await safe_reply(message, "🚫 Только для админов.", add_to_cleanup=True); return
    is_bot_admin = False
    try: bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
    except (ChatAdminRequired, BotKicked, BotBlocked): await safe_reply(message, "🤖 Я не администратор.", add_to_cleanup=True); return
    except Exception as e: log.error(f"Ошибка проверки прав (разбан): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not is_bot_admin: await safe_reply(message, "🤖 Нужны права админа.", add_to_cleanup=True); return
    log.info(f"Bot status in chat {message.chat.id} for unban: {bot_member.status}.")
    target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user
    try:
        await bot.unban_chat_member(message.chat.id, target_user_id) # Убрали only_if_banned
        log.info(f"User {target_user_id} unbanned by {message.from_user.id}.")
        await safe_reply(message, f"✅🔓 {target_user_info.get_mention(as_html=True)} разбанен!", parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except BadRequest as e:
         err_text = f"❌ Ошибка API: {e}"
         if "user not participant" in str(e).lower(): err_text = f"🚶 {target_user_info.get_mention(as_html=True)} не найден в чате."
         elif "user was not kicked" in str(e).lower(): err_text = f"🤷 {target_user_info.get_mention(as_html=True)} и так не был забанен."
         elif "user_not_found" in str(e).lower(): err_text = f"❓ {target_user_info.get_mention(as_html=True)} не найден."
         log.error(f"Ошибка разбана (BadRequest) {target_user_id}: {e}")
         await safe_reply(message, err_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except Exception as e: log.error(f"Неизвестная ошибка разбана {target_user_id}: {e}"); await safe_reply(message, "💥 Ой! Ошибка при разбане.", add_to_cleanup=True)

@dp.message_handler(lambda message: message.text.lower().startswith('!репорт'))
async def report_user(message: types.Message): # ... (код как в пред. версии) ...
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи сообщение **ответом**.", add_to_cleanup=True); return
    command_parts = message.text.split(maxsplit=1)
    if len(command_parts) < 2 or not command_parts[1].strip(): await safe_reply(message, "❓ Укажи причину: <code>!репорт причина</code>.", parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    reason = command_parts[1].strip(); target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user; reporter_user_info = message.from_user
    if target_user_id == reporter_user_info.id: await safe_reply(message, "😅 На себя жаловаться?", add_to_cleanup=True); return
    if target_user_id == bot.id: await safe_reply(message, "🤖 Не нужно репортить меня.", add_to_cleanup=True); return
    if await is_user_admin(message.chat.id, target_user_id): await safe_reply(message, "🛡️ С админами разбирайтесь лично.", add_to_cleanup=True); return
    add_report(reporter_user_info.id, target_user_id, message.chat.id, reason)
    sent_to_admins = False; reply_text = ""; admins = []
    try:
        admins = await bot.get_chat_administrators(message.chat.id); link_text = f"(Msg ID: {message.reply_to_message.message_id})"
        try: report_link = message.reply_to_message.get_url(); link_text = f"<a href='{report_link}'>🔗 К сообщению</a>"
        except Exception as link_err: log.warning(f"Не удалось получить ссылку: {link_err}")
        chat_title_safe = f"'{message.chat.title}'" if message.chat.title else "ЛС"; reason_safe = reason.replace('&', '&').replace('<', '<').replace('>', '>')
        report_message = (f"🚨 <b>Жалоба!</b> [Чат: {chat_title_safe} (<code>{message.chat.id}</code>)]\n\n👤 <b>От:</b> {reporter_user_info.get_mention(as_html=True)} (<code>{reporter_user_info.id}</code>)\n🎯 <b>На:</b> {target_user_info.get_mention(as_html=True)} (<code>{target_user_id}</code>)\n💬 <b>Причина:</b> {reason_safe}\n\n{link_text}")
        admin_ids_notified = set()
        for admin in admins:
            admin_user = admin.user
            if not admin_user.is_bot and admin_user.id != reporter_user_info.id and admin_user.id not in admin_ids_notified:
                try: await bot.send_message(admin_user.id, report_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True); sent_to_admins = True; admin_ids_notified.add(admin_user.id); log.info(f"Репорт отправлен админу {admin_user.id}")
                except (BotBlocked, ChatAdminRequired) as e: log.warning(f"Не могу ЛС админу {admin_user.id}: {e}")
                except Exception as e: log.error(f"Ошибка ЛС админу {admin_user.id}: {e}")
    except Exception as e: log.error(f"Критическая ошибка отправки репорта: {e}")
    if sent_to_admins: reply_text = f"✅📢 Жалоба на {target_user_info.get_mention(as_html=True)} отправлена админам!"
    else:
         other_admins_exist = any(not adm.user.is_bot and adm.user.id != reporter_user_info.id for adm in admins) if admins else False
         if other_admins_exist: reply_text = "⚠️ Жалоба записана, но не смог уведомить админов (нет диалога?)."
         else: reply_text = "⚠️ Жалоба записана, но нет других админов для уведомления."
    await safe_reply(message, reply_text, parse_mode=ParseMode.HTML, add_to_cleanup=True)


# --- Команды без очистки ---
@dp.message_handler(lambda message: message.text.lower() == '!репорты')
async def show_my_reports(message: types.Message): # ... (код как в пред. версии) ...
    user_id = message.from_user.id; chat_id = message.chat.id; user_reports = get_reports_for_user(user_id, chat_id)
    if not user_reports: await safe_reply(message, "🎉 Поздравляю! На вас нет жалоб в этом чате.", add_to_cleanup=False); return
    chat_title_safe = f"<b>'{message.chat.title}'</b>" if message.chat.title else "этом чате"; response_text = f"🧐 <b>Жалобы на вас в {chat_title_safe}:</b>\n\n"
    for i, report in enumerate(user_reports, 1):
        report_time_str = report.get('report_time', '??'); reason = report.get('reason', '-'); safe_reason = reason.replace('&', '&').replace('<', '<').replace('>', '>')
        report_entry = f"📌 <b>#{i}</b> (<code>{report_time_str}</code>)\n   <i>Причина:</i> {safe_reason}\n\n"
        if len(response_text) + len(report_entry) > 4000: response_text += "\n... (слишком много жалоб)"; break
        response_text += report_entry
    await safe_reply(message, response_text, parse_mode=ParseMode.HTML, add_to_cleanup=False)

@dp.message_handler(lambda message: message.text.lower() == '!бу')
async def bu_command(message: types.Message): await safe_reply(message, f"{random.choice(BU_PHRASES)}", add_to_cleanup=False)

@dp.message_handler(lambda message: message.text.lower() == '!я')
async def my_stats_command(message: types.Message): # ... (код как в пред. версии) ...
    user_id = message.from_user.id; chat_id = message.chat.id; mute_count, report_count = get_user_stats(user_id, chat_id)
    response_text = (f"📊 <b>Твоя статистика здесь:</b>\n  ├─ 🤫 Мутов получено: <b>{mute_count}</b>\n  └─ 📢 Жалоб на тебя: <b>{report_count}</b>")
    await safe_reply(message, response_text, parse_mode=ParseMode.HTML, add_to_cleanup=False)

# --- Команда !сброс (ручная очистка) ---
@dp.message_handler(lambda message: message.text.lower() == '!сброс')
async def manual_cleanup_command(message: types.Message): # ... (код как в пред. версии) ...
    if not await is_user_admin(message.chat.id, message.from_user.id): await safe_reply(message, "🚫 Только для админов.", add_to_cleanup=True); return
    msg = await safe_reply(message, "🗑️ Запускаю ручную очистку...", add_to_cleanup=False)
    deleted, failed = await perform_cleanup()
    result_text = f"✅ Очистка завершена. Удалено: {deleted}, Ошибок: {failed}."
    try:
        if msg: await msg.edit_text(result_text)
        else: await safe_reply(message, result_text, add_to_cleanup=True)
    except Exception as e: log.warning(f"Не удалось отредактировать сообщение о сбросе: {e}"); await safe_reply(message, result_text, add_to_cleanup=True)

# --- Команда !чистка ---
# !!! ИЗМЕНЕНО: Используем delete_message вместо delete_messages !!!
@dp.message_handler(IsChatCreatorFilter(True), lambda message: message.text.lower().startswith(('!чистка', '/purge')))
async def purge_messages_command(message: types.Message):
    log.info(f"Команда чистки вызвана создателем {message.from_user.id} в чате {message.chat.id}")
    is_bot_admin = False; can_delete = False
    try:
        bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if hasattr(bot_member, 'can_delete_messages'): can_delete = bot_member.can_delete_messages
        else: can_delete = is_bot_admin
    except Exception as e: log.error(f"Ошибка проверки прав (чистка): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not can_delete: await safe_reply(message, "🤖 Нет прав на удаление.", add_to_cleanup=True); return

    parts = message.text.split(); count = 0
    if len(parts) > 1 and parts[1].isdigit(): count = int(parts[1])
    else: await safe_reply(message, "❓ Укажи кол-во: <code>!чистка N</code> или <code>/purge N</code>", parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    if count < 1: await safe_reply(message, "Число > 0.", add_to_cleanup=True); return
    if count > PURGE_MESSAGE_LIMIT: count = PURGE_MESSAGE_LIMIT; await safe_reply(message, f"❗️ Макс {PURGE_MESSAGE_LIMIT} за раз. Удаляю {PURGE_MESSAGE_LIMIT}.", add_to_cleanup=True)

    # Сначала удаляем предыдущие сообщения
    deleted_count = 0; failed_count = 0
    message_ids_to_delete = [msg_id for msg_id in range(message.message_id - 1, message.message_id - count - 1, -1) if msg_id > 0]

    log.info(f"Начинаю удаление {len(message_ids_to_delete)} сообщений по одному...")
    status_msg = await message.answer(f"Удаляю {len(message_ids_to_delete)} сообщений...") # Уведомление для пользователя

    for msg_id in message_ids_to_delete:
        try:
            await bot.delete_message(message.chat.id, msg_id) # <<<--- ИСПОЛЬЗУЕМ delete_message
            deleted_count += 1
            await asyncio.sleep(0.35) # !!! ПАУЗА ВАЖНА при удалении по одному !!!
        except (MessageToDeleteNotFound, MessageCantBeDeleted): failed_count += 1
        except BadRequest as e: log.warning(f"Ошибка удаления {msg_id} при чистке: {e}"); failed_count += 1
        except Throttled as e:
            log.warning(f"Лимит при чистке: {e}. Ждем {e.timeout or 1.5} сек."); await asyncio.sleep(e.timeout or 1.5)
            # Можно добавить повторную попытку или просто пропустить
            failed_count += 1
        except Exception as e: log.error(f"Ошибка при чистке {msg_id}: {e}"); failed_count += 1

    # Удаляем сообщение с командой
    try:
        await message.delete()
        deleted_count += 1
    except Exception as e: log.warning(f"Не удалось удалить команду чистки: {e}")

    log.info(f"Удалено {deleted_count} сообщений по !чистка от {message.from_user.id}. Ошибок: {failed_count}")
    try:
        # Редактируем статусное сообщение или отправляем новое
        result_text = f"✅ Удалено {deleted_count} сообщений. Ошибок: {failed_count}."
        if status_msg: await status_msg.edit_text(result_text)
        else: status_msg = await message.answer(result_text)
        await asyncio.sleep(7) # Даем время прочитать результат
        await status_msg.delete()
    except Exception as e: log.warning(f"Не удалось обновить/удалить статус чистки: {e}")


# --- Команда !админ ---
@dp.message_handler(IsChatCreatorFilter(True), lambda message: message.text.lower() in ('!админ', '/promote'))
async def promote_user_command(message: types.Message): # ... (код как в пред. версии) ...
    if not message.reply_to_message: await safe_reply(message, "⚠️ Укажи пользователя **ответом**.", add_to_cleanup=True); return
    is_bot_admin = False; can_promote = False
    try:
        bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if hasattr(bot_member, 'can_promote_members'): can_promote = bot_member.can_promote_members
        else: can_promote = is_bot_admin
    except Exception as e: log.error(f"Ошибка проверки прав (админ): {e}"); await safe_reply(message, "⚙️ Ошибка проверки прав.", add_to_cleanup=True); return
    if not can_promote: await safe_reply(message, "🤖 Нет прав на назначение админов.", add_to_cleanup=True); return
    target_user_id = message.reply_to_message.from_user.id; target_user_info = message.reply_to_message.from_user
    if target_user_info.is_bot: await safe_reply(message, "😅 Нельзя сделать бота админом.", add_to_cleanup=True); return
    if await is_user_admin(message.chat.id, target_user_id): await safe_reply(message, f"🤷 {target_user_info.get_mention(as_html=True)} уже админ.", parse_mode=ParseMode.HTML, add_to_cleanup=True); return
    try:
        await bot.promote_chat_member(chat_id=message.chat.id, user_id=target_user_id, can_change_info=True, can_delete_messages=True, can_invite_users=True, can_restrict_members=True, can_pin_messages=True, can_promote_members=False)
        log.info(f"User {target_user_id} promoted by {message.from_user.id} in {message.chat.id}")
        await safe_reply(message, f"✅👑 {target_user_info.get_mention(as_html=True)} назначен админом!", parse_mode=ParseMode.HTML, add_to_cleanup=True)
    except BadRequest as e:
        log.error(f"Ошибка назначения админа (BadRequest) {target_user_id}: {e}"); err_text = f"❌ Ошибка API: {e}"
        if "not enough rights" in str(e).lower(): err_text = "🛡️ У меня недостаточно прав."
        await safe_reply(message, err_text, add_to_cleanup=True)
    except Exception as e: log.error(f"Неизвестная ошибка назначения админа {target_user_id}: {e}"); await safe_reply(message, "💥 Ошибка при назначении.", add_to_cleanup=True)

# --- Обработчик мата ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def filter_swear_words(message: types.Message): # ... (код как в пред. версии) ...
    if message.text.startswith(('!', '/')) or message.chat.type == types.ChatType.PRIVATE or message.from_user.is_bot: return
    lower_text = message.text.lower(); found_swears = {word for word in SWEAR_WORDS if re.search(r'\b' + re.escape(word) + r'\b', lower_text)}
    if not found_swears: return
    is_bot_admin = False; can_delete = False
    try:
        bot_member = await bot.get_chat_member(message.chat.id, bot.id); is_bot_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
        if hasattr(bot_member, 'can_delete_messages'): can_delete = bot_member.can_delete_messages
        else: can_delete = is_bot_admin
    except (BadRequest, ChatAdminRequired, BotKicked, BotBlocked) as e:
         if "member not found" not in str(e).lower(): log.warning(f"Не удалось проверить права бота (фильтр мата): {e}")
         return
    except Exception as e: log.error(f"Ошибка проверки прав бота (фильтр мата): {e}"); return
    if not can_delete:
        if is_bot_admin: log.warning(f"Бот админ в {message.chat.id}, но нет права 'can_delete_messages'.")
        return
    try: await message.delete(); log.info(f"Удалено сообщение {message.message_id} от {message.from_user.id} (мат: {', '.join(found_swears)})")
    except BadRequest as e:
         if "message to delete not found" in str(e): pass
         elif "message can't be deleted" in str(e): log.warning(f"Не могу удалить {message.message_id} (старое?)")
         elif "not enough rights" in str(e).lower(): log.warning(f"Недостаточно прав для удаления {message.message_id} в {message.chat.id}.")
         else: log.error(f"Ошибка удаления (BadRequest) {message.message_id}: {e}")
    except Exception as e: log.error(f"Неизвестная ошибка удаления {message.message_id}: {e}")

# --- Регистрация команд для меню ---
async def set_default_commands(dp):
    try:
        await dp.bot.set_my_commands([
            BotCommand("help", "Показать список команд"),
            BotCommand("start", "Начать работу с ботом"),
            BotCommand("purge", "Очистить N сообщений (создатель)"),
            BotCommand("promote", "Назначить админа (создатель, ответом)")
        ])
        log.info("Команды бота для меню успешно установлены.")
    except Exception as e:
        log.error(f"Не удалось установить команды бота: {e}")

# --- Запуск бота и планировщика ---
async def on_startup(dp):
    log.info("Инициализация файлов данных...")
    initialize_data_files()
    log.info("Установка команд бота...")
    await set_default_commands(dp)
    log.info("Запуск фоновой задачи очистки сообщений...")
    asyncio.create_task(cleanup_scheduler())

async def on_shutdown(dp): log.info("Остановка бота...")

if __name__ == '__main__':
    log.info("🚀 Запуск бота...")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)