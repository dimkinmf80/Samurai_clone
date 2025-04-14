# -*- coding: utf-8 -*-
import subprocess
import sys
import os
import signal
from threading import Thread
import time
import logging

# Используем Pillow для загрузки иконки
try:
    from PIL import Image
except ImportError:
    print("Ошибка: Библиотека Pillow не найдена. Установите ее: pip install Pillow")
    sys.exit(1)

# Используем pystray для иконки в трее
try:
    import pystray
except ImportError:
    print("Ошибка: Библиотека pystray не найдена. Установите ее: pip install pystray")
    sys.exit(1)

# Модуль для работы с реестром Windows (только для Windows)
if sys.platform == 'win32':
    try:
        import winreg
    except ImportError:
        print("Предупреждение: Модуль winreg не найден (нужен для автозагрузки).")
        winreg = None # Устанавливаем в None, чтобы проверки ниже работали
else:
    winreg = None # Не Windows, автозагрузка через реестр не поддерживается

# --- Настройки ---
BOT_SCRIPT_NAME = 'main_bot_script.py' # Имя файла вашего скрипта с ботом
ICON_NAME = 'icon.png' # Имя файла иконки (png или ico)
APP_TITLE = 'Мой Телеграм Бот' # Заголовок при наведении на иконку
PYTHON_EXECUTABLE = sys.executable # Путь к текущему интерпретатору Python

# Настройки для автозагрузки (Windows)
AUTOSTART_APP_NAME = "MyTelegramBotTrayApp" # Уникальное имя для записи в реестре
REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run" # Путь в реестре HKCU

# Настройка логирования для трей-приложения
log_format = '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
tray_log = logging.getLogger('tray_app')

# --- Глобальные переменные ---
bot_process = None          # Хранит объект запущенного процесса бота
monitoring_active = True    # Флаг для управления потоком мониторинга
tray_icon = None            # Хранит объект иконки pystray (важно для обновления меню)

# --- Вспомогательные функции ---

def get_script_path(filename):
    """Получает абсолютный путь к файлу в той же директории, что и скрипт."""
    # Определяем базовую директорию (работает и для .py, и для .exe)
    if getattr(sys, 'frozen', False):
        # Если запущено из .exe (скомпилировано PyInstaller)
        base_dir = os.path.dirname(sys.executable)
    else:
        # Если запущено как .py скрипт
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, filename)

def get_command_for_startup():
    """Возвращает команду для запуска приложения (для записи в реестр)."""
    if getattr(sys, 'frozen', False):
        # Если запущено из .exe, команда - это просто путь к .exe
        exe_path = sys.executable
        return f'"{exe_path}"'
    else:
        # Если запущено как .py, используем pythonw.exe для запуска без консоли
        pythonw_exe = sys.executable.replace("python.exe", "pythonw.exe")
        script_path = os.path.abspath(__file__)
        # Добавляем кавычки на случай пробелов в путях
        return f'"{pythonw_exe}" "{script_path}"'

# --- Функции управления ботом ---

def is_bot_running():
    """Проверяет, запущен ли процесс бота и активен ли он."""
    global bot_process
    return bot_process and bot_process.poll() is None

def start_bot():
    """Запускает скрипт бота в отдельном процессе."""
    global bot_process
    if is_bot_running():
        tray_log.warning("Попытка запуска, но бот уже запущен.")
        return

    bot_script_path = get_script_path(BOT_SCRIPT_NAME)
    if not os.path.exists(bot_script_path):
        tray_log.error(f"Ошибка: Скрипт бота '{BOT_SCRIPT_NAME}' не найден по пути '{bot_script_path}'!")
        # Можно добавить уведомление пользователю через pystray, если оно уже создано
        if tray_icon:
             tray_icon.notify(f"Ошибка: Скрипт {BOT_SCRIPT_NAME} не найден!", APP_TITLE)
        return

    try:
        tray_log.info(f"Запуск бота: {PYTHON_EXECUTABLE} {bot_script_path}")
        # Используем Popen для неблокирующего запуска
        # CREATE_NEW_PROCESS_GROUP нужен для корректного CTRL_BREAK_EVENT на Windows
        flags = 0
        if sys.platform == 'win32':
            flags = subprocess.CREATE_NEW_PROCESS_GROUP

        # Запускаем без создания нового окна консоли
        bot_process = subprocess.Popen(
            [PYTHON_EXECUTABLE, bot_script_path],
            creationflags=flags,
            stdout=subprocess.PIPE, # Перенаправляем вывод, чтобы он не мешал
            stderr=subprocess.PIPE, # Перенаправляем ошибки
            encoding='utf-8',       # Указываем кодировку вывода
            errors='ignore'         # Игнорируем ошибки декодирования, если возникнут
        )
        tray_log.info(f"Бот запущен с PID: {bot_process.pid}")
        # Запускаем потоки для чтения вывода бота (чтобы не блокировать PIPE)
        Thread(target=log_bot_output, args=(bot_process.stdout, "BOT-STDOUT"), daemon=True, name="BotStdoutLogger").start()
        Thread(target=log_bot_output, args=(bot_process.stderr, "BOT-STDERR"), daemon=True, name="BotStderrLogger").start()

        update_menu() # Обновляем меню после успешного запуска
    except Exception as e:
        tray_log.error(f"Не удалось запустить бота: {e}")
        bot_process = None
        update_menu() # Обновляем меню, т.к. запуск не удался

def log_bot_output(pipe, pipe_name):
    """Читает и логирует вывод из stdout/stderr процесса бота."""
    try:
        for line in iter(pipe.readline, ''):
            if line:
                tray_log.info(f"[{pipe_name}] {line.strip()}")
    except Exception as e:
        tray_log.warning(f"Ошибка чтения вывода [{pipe_name}]: {e}")
    finally:
        pipe.close()


def stop_bot():
    """Останавливает процесс бота."""
    global bot_process
    if not is_bot_running():
        tray_log.warning("Попытка остановки, но бот не запущен.")
        return

    pid = bot_process.pid
    tray_log.info(f"Попытка остановить бота (PID: {pid})...")
    terminated_gracefully = False
    try:
        if sys.platform == "win32":
            # На Windows посылаем CTRL_BREAK_EVENT, чтобы aiogram мог перехватить shutdown
            tray_log.debug(f"Отправка CTRL_BREAK_EVENT процессу {pid}")
            bot_process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                bot_process.wait(timeout=5) # Даем время на реакцию (5 секунд)
                tray_log.info(f"Бот (PID: {pid}) корректно завершился после CTRL_BREAK_EVENT.")
                terminated_gracefully = True
            except subprocess.TimeoutExpired:
                tray_log.warning(f"Бот (PID: {pid}) не ответил на CTRL_BREAK за 5 сек. Используем terminate().")
        else:
            # На Linux/macOS отправляем SIGTERM (аналог terminate)
             tray_log.debug(f"Отправка SIGTERM процессу {pid}")
             bot_process.terminate()
             try:
                 bot_process.wait(timeout=5)
                 tray_log.info(f"Бот (PID: {pid}) корректно завершился после SIGTERM.")
                 terminated_gracefully = True
             except subprocess.TimeoutExpired:
                 tray_log.warning(f"Бот (PID: {pid}) не ответил на SIGTERM за 5 сек. Используем kill().")

        # Если не завершился штатно, используем kill (SIGKILL)
        if not terminated_gracefully:
            tray_log.debug(f"Принудительная остановка (kill) процесса {pid}")
            bot_process.kill()
            try:
                bot_process.wait(timeout=2) # Короткое ожидание после kill
                tray_log.info(f"Бот (PID: {pid}) принудительно остановлен (kill).")
            except subprocess.TimeoutExpired:
                 tray_log.error(f"Не удалось дождаться завершения процесса {pid} даже после kill!")
            except Exception as e:
                 tray_log.error(f"Ошибка при ожидании после kill для PID {pid}: {e}")

    except ProcessLookupError:
        # Процесс уже мог завершиться сам по себе
        tray_log.warning(f"Процесс бота (PID: {pid}) не найден при попытке остановки (возможно, уже завершился).")
    except Exception as e:
        tray_log.error(f"Непредвиденная ошибка при остановке бота (PID: {pid}): {e}")
    finally:
        # Закрываем потоки stdout/stderr, если они еще не закрыты
        if bot_process:
            if bot_process.stdout and not bot_process.stdout.closed:
                bot_process.stdout.close()
            if bot_process.stderr and not bot_process.stderr.closed:
                bot_process.stderr.close()
        bot_process = None # Сбрасываем процесс
        update_menu() # Обновляем меню в любом случае

# --- Функции для автозагрузки через реестр (Только Windows) ---

def is_in_startup():
    """Проверяет, есть ли приложение в автозагрузке текущего пользователя (только Windows)."""
    if not winreg: return False # Не Windows или модуль не загружен
    try:
        # Открываем ключ реестра для чтения
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_READ)
        # Пытаемся прочитать значение с нашим именем
        winreg.QueryValueEx(key, AUTOSTART_APP_NAME)
        winreg.CloseKey(key)
        return True # Значение найдено
    except FileNotFoundError:
        return False # Ключ или значение не найдено
    except Exception as e:
        tray_log.error(f"Ошибка при проверке автозагрузки: {e}")
        return False

def add_to_startup():
    """Добавляет приложение в автозагрузку текущего пользователя (только Windows)."""
    if not winreg:
        tray_log.error("Невозможно добавить в автозагрузку: функция не поддерживается или модуль winreg не найден.")
        return False
    if is_in_startup():
        tray_log.info("Приложение уже в автозагрузке.")
        return True
    try:
        # Открываем ключ реестра для записи
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_WRITE)
        command = get_command_for_startup()
        # Устанавливаем строковое значение (REG_SZ) с командой запуска
        winreg.SetValueEx(key, AUTOSTART_APP_NAME, 0, winreg.REG_SZ, command)
        winreg.CloseKey(key)
        tray_log.info(f"Приложение '{AUTOSTART_APP_NAME}' успешно добавлено в автозагрузку.")
        update_menu() # Обновляем меню, чтобы изменить доступность пунктов
        return True
    except Exception as e:
        tray_log.error(f"Ошибка при добавлении в автозагрузку: {e}")
        return False

def remove_from_startup():
    """Удаляет приложение из автозагрузки текущего пользователя (только Windows)."""
    if not winreg:
        tray_log.error("Невозможно удалить из автозагрузки: функция не поддерживается или модуль winreg не найден.")
        return False
    if not is_in_startup():
        tray_log.info("Приложение не найдено в автозагрузке для удаления.")
        return True
    try:
        # Открываем ключ реестра для записи (нужно для удаления значения)
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_WRITE)
        # Удаляем значение с нашим именем
        winreg.DeleteValue(key, AUTOSTART_APP_NAME)
        winreg.CloseKey(key)
        tray_log.info(f"Приложение '{AUTOSTART_APP_NAME}' успешно удалено из автозагрузки.")
        update_menu() # Обновляем меню
        return True
    except FileNotFoundError:
         # Этого не должно случиться из-за проверки is_in_startup, но на всякий случай
         tray_log.warning(f"Пытались удалить '{AUTOSTART_APP_NAME}' из автозагрузки, но значение не найдено.")
         update_menu()
         return True # Считаем успешным, раз его там нет
    except Exception as e:
        tray_log.error(f"Ошибка при удалении из автозагрузки: {e}")
        return False

# --- Функции для трея ---

def get_icon_image():
    """Загружает изображение для иконки."""
    icon_path = get_script_path(ICON_NAME)
    try:
        return Image.open(icon_path)
    except FileNotFoundError:
        tray_log.error(f"Файл иконки '{ICON_NAME}' не найден по пути '{icon_path}'!")
        # Возвращаем заглушку
        return Image.new('RGB', (64, 64), color = 'red')
    except Exception as e:
        tray_log.error(f"Ошибка загрузки иконки '{icon_path}': {e}")
        return Image.new('RGB', (64, 64), color = 'red')

def create_menu():
    """Создает и возвращает меню для иконки трея."""
    running = is_bot_running()
    status_text = "Статус: Запущен" if running else "Статус: Остановлен"

    menu_items = [
        pystray.MenuItem(status_text, None, enabled=False), # Некликабельный статус
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Запустить бота', start_bot_action, enabled=not running),
        pystray.MenuItem('Остановить бота', stop_bot_action, enabled=running),
        pystray.Menu.SEPARATOR,
    ]

    # Добавляем пункты автозагрузки только для Windows и если winreg доступен
    if sys.platform == 'win32' and winreg:
        in_startup = is_in_startup()
        menu_items.extend([
            pystray.MenuItem(
                'Удалить из автозагрузки',
                remove_from_startup_action,
                enabled=in_startup
            ),
            pystray.MenuItem(
                'Добавить в автозагрузку',
                add_to_startup_action,
                enabled=not in_startup
            ),
            pystray.Menu.SEPARATOR,
        ])

    menu_items.append(pystray.MenuItem('Выход', on_exit))

    return pystray.Menu(*menu_items) # Используем * для распаковки списка в аргументы Menu

def update_menu():
    """Обновляет меню и заголовок иконки (вызывается после start/stop/autostart)."""
    global tray_icon
    if tray_icon:
        try:
            tray_icon.menu = create_menu()
            # tray_icon.update_menu() # Этот метод может вызывать ошибки в некоторых версиях pystray
            # Обновляем всплывающую подсказку (title)
            running_status = 'Запущен' if is_bot_running() else 'Остановлен'
            tray_icon.title = f"{APP_TITLE} ({running_status})"
            tray_log.debug("Меню и заголовок трея обновлены.")
        except Exception as e:
            tray_log.error(f"Ошибка при обновлении меню трея: {e}")
    else:
        tray_log.warning("Попытка обновить меню, но объект tray_icon еще не создан.")


# --- Функции-обработчики действий меню ---

def start_bot_action(icon, item):
    """Действие при клике на 'Запустить бота'."""
    tray_log.info("Получена команда 'Запустить бота' из меню.")
    # Запускаем в отдельном потоке, чтобы не блокировать GUI трея
    Thread(target=start_bot, daemon=True, name="StartBotThread").start()

def stop_bot_action(icon, item):
    """Действие при клике на 'Остановить бота'."""
    tray_log.info("Получена команда 'Остановить бота' из меню.")
    # Запускаем в отдельном потоке
    Thread(target=stop_bot, daemon=True, name="StopBotThread").start()

def add_to_startup_action(icon, item):
    """Действие при клике на 'Добавить в автозагрузку'."""
    tray_log.info("Получена команда 'Добавить в автозагрузку'.")
    # Можно запустить в потоке, но операция быстрая
    if add_to_startup():
        icon.notify("Успешно добавлено в автозагрузку", APP_TITLE)
    else:
        icon.notify("Ошибка при добавлении в автозагрузку", APP_TITLE)

def remove_from_startup_action(icon, item):
    """Действие при клике на 'Удалить из автозагрузки'."""
    tray_log.info("Получена команда 'Удалить из автозагрузки'.")
    if remove_from_startup():
         icon.notify("Успешно удалено из автозагрузки", APP_TITLE)
    else:
        icon.notify("Ошибка при удалении из автозагрузки", APP_TITLE)

def on_exit(icon, item):
    """Действие при выходе из приложения."""
    global monitoring_active
    tray_log.info("Получена команда 'Выход'.")
    monitoring_active = False # Останавливаем поток мониторинга
    # Останавливаем бота перед выходом (в отдельном потоке, но ждем его)
    stop_thread = Thread(target=stop_bot, name="ExitStopBotThread")
    stop_thread.start()
    stop_thread.join(timeout=7) # Ждем до 7 секунд завершения stop_bot

    tray_log.info("Остановка иконки трея...")
    icon.stop() # Закрываем трей-приложение
    tray_log.info("Трей-приложение завершено.")
    # Принудительно выходим, если что-то зависло
    # os._exit(0) # Раскомментировать, если приложение иногда не закрывается полностью

# --- Фоновый мониторинг процесса бота ---
def monitor_bot_process():
    """Фоновый поток для проверки, не завершился ли бот сам по себе."""
    global monitoring_active, bot_process
    tray_log.info("Поток мониторинга запущен.")
    while monitoring_active:
        if bot_process and bot_process.poll() is not None:
            # Процесс завершился
            exit_code = bot_process.poll()
            tray_log.warning(f"Процесс бота (PID: {bot_process.pid}) неожиданно завершился с кодом {exit_code}.")
            # Обновляем состояние и меню
            stop_bot() # Вызов stop_bot корректно сбросит bot_process и обновит меню
            # Опционально: Попытаться перезапустить бота
            # tray_log.info("Попытка перезапуска бота...")
            # start_bot()
            # Опционально: Уведомить пользователя
            if tray_icon:
                 tray_icon.notify(f"Бот неожиданно остановился (код {exit_code})", APP_TITLE)
        # Пауза перед следующей проверкой
        time.sleep(10) # Проверять каждые 10 секунд
    tray_log.info("Поток мониторинга остановлен.")

# --- Основная часть ---
if __name__ == "__main__":
    tray_log.info("Запуск трей-приложения...")

    # Загружаем иконку
    icon_image = get_icon_image()

    # Создаем объект иконки pystray
    # Меню будет создано функцией create_menu
    tray_icon = pystray.Icon(
        "telegram_bot_tray",
        icon=icon_image,
        title=APP_TITLE, # Начальный заголовок
        menu=create_menu() # Создаем начальное меню
    )

    # Запускаем бота автоматически при старте приложения (в отдельном потоке)
    tray_log.info("Автоматический запуск бота при старте...")
    initial_start_thread = Thread(target=start_bot, daemon=True, name="InitialStartBot")
    initial_start_thread.start()
    # Дадим немного времени на запуск перед обновлением заголовка (не обязательно)
    time.sleep(1)
    update_menu() # Обновляем заголовок и меню после попытки запуска

    # Запускаем поток мониторинга состояния бота
    monitor_thread = Thread(target=monitor_bot_process, daemon=True, name="BotMonitorThread")
    monitor_thread.start()

    # Запускаем главный цикл pystray (блокирующая операция)
    tray_log.info("Трей-приложение готово и работает в фоне.")
    try:
        tray_icon.run()
    except KeyboardInterrupt:
        tray_log.info("Получен сигнал KeyboardInterrupt (Ctrl+C). Завершение...")
        on_exit(tray_icon, None) # Вызываем нашу функцию выхода
    except Exception as e:
        tray_log.error(f"Непредвиденная ошибка в главном цикле pystray: {e}", exc_info=True)
    finally:
        # Код здесь выполнится при нормальном завершении или ошибке в run()
        # Дополнительная гарантия остановки, если on_exit не сработал
        if monitoring_active: # Если on_exit не был вызван
             monitoring_active = False
             stop_bot()
        tray_log.info("Завершение основного потока приложения.")