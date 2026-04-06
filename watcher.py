# watcher.py
#
# Воркер для Railway:
#   Procfile: worker: python watcher.py
#
# Логика (в общих чертах, детали добавим позже):
#   - каждые N минут заходим на страницу аккаунтов Brawl Stars на FunPay
#   - парсим список офферов (ссылки, цена, количество бойцов)
#   - при необходимости заходим в конкретный оффер, чтобы уточнить кол-во бойцов
#   - фильтруем по твоим правилам выгодности
#   - отправляем подходящие варианты в Telegram

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, List, Dict

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# ===== 1. Загрузка конфигурации =====

if os.getenv("CONFIG_JSON"):
    config = json.loads(os.getenv("CONFIG_JSON"))
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

TELEGRAM_BOT_TOKEN: str = config["telegram_bot_token"]
TELEGRAM_CHAT_ID: str = config["telegram_chat_id"]
CHECK_INTERVAL_MINUTES: int = int(config.get("check_interval_minutes", 5))

BASE_URL: str = config.get("base_url", "https://funpay.com").rstrip("/")
BRAWL_ACCOUNTS_URL: str = config.get(
    "brawl_accounts_url",
    f"{BASE_URL}/lots/436/"
)

MAX_SIGNALS_PER_DAY: int = int(config.get("max_signals_per_day", 100))


# ===== 2. Локальное состояние (state.json) =====

def load_state() -> dict:
    """
    Загружаем state.json (память о уже отправленных офферах).
    Если файла нет или он битый – возвращаем пустой dict.
    """
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    """
    Сохраняем state.json.
    """
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ===== 3. HTTP-сессия и Telegram =====

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
)


def fetch_page(url: str) -> str:
    """
    Скачиваем HTML страницы с нормальным User-Agent'ом.
    Бросает исключение, если не получилось.
    """
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def send_telegram_message(text: str) -> None:
    """
    Отправка сообщения в Telegram в указанный чат.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


# ===== 4. Структуры данных (заполним позже) =====

@dataclass
class Offer:
    offer_id: str          # 66881057
    url: str               # полная ссылка на оффер
    price_rub: float       # цена в рублях
    heroes: Optional[int]  # количество бойцов (если знаем)
    title: str             # краткое описание
    seller_name: str       # ник продавца


# ===== 5. Основные шаги поиска выгодных офферов (заглушки) =====

def collect_offers(state: dict) -> List[Offer]:
    """
    Здесь будет:
      - загрузка страницы с аккаунтами Brawl Stars
      - парсинг HTML и сбор списка Offer
    Пока заглушка.
    """
    print("[INFO] collect_offers() пока не реализован, возвращаем пустой список.")
    return []


def filter_profitable_offers(offers: List[Offer]) -> List[Offer]:
    """
    Здесь будет логика фильтрации по количеству бойцов и цене,
    плюс расчёт метки выгодности.
    Пока заглушка.
    """
    print("[INFO] filter_profitable_offers() пока не реализован, возвращаем пустой список.")
    return []


def send_new_offers_to_telegram(offers: List[Offer], state: dict) -> None:
    """
    Отправляем новые выгодные офферы в Telegram
    и помечаем их в state, чтобы не дублировать.
    """
    if not offers:
        print("[INFO] Нет новых выгодных офферов для отправки.")
        return

    sent_offers: Dict[str, bool] = state.setdefault("sent_offers", {})
    sent_now = 0

    for offer in offers:
        if sent_offers.get(offer.offer_id):
            continue

        text_lines = [
            "Найден аккаунт:",
            f"Бойцов: {offer.heroes if offer.heroes is not None else 'неизвестно'}",
            f"Стоимость: {offer.price_rub:.2f} ₽",
            f"Ссылка: {offer.url}",
        ]
        text = "\n".join(text_lines)

        try:
            print(f"[INFO] Отправляем оффер {offer.offer_id} ({offer.price_rub:.2f} ₽, {offer.heroes} бойцов)")
            send_telegram_message(text)
            sent_offers[offer.offer_id] = True
            save_state(state)
            sent_now += 1
            if sent_now >= MAX_SIGNALS_PER_DAY:
                print("[INFO] Достигнут дневной лимит отправки офферов.")
                break
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")


def run_single_iteration() -> None:
    """
    Одна полная итерация:
      - загрузить состояние
      - собрать офферы
      - отфильтровать выгодные
      - отправить в Telegram
    """
    print("=" * 60)
    print("[INFO] Запуск проверки FunPay аккаунтов Brawl Stars")

    state = load_state()

    offers = collect_offers(state)
    print(f"[INFO] Собрано офферов: {len(offers)}")

    profitable = filter_profitable_offers(offers)
    print(f"[INFO] Найдено выгодных офферов: {len(profitable)}")

    if profitable:
        send_new_offers_to_telegram(profitable, state)
    else:
        print("[INFO] Выгодных офферов в этой итерации нет.")


def main_loop() -> None:
    """
    Бесконечный цикл для Railway.
    """
    interval_sec = max(1, int(CHECK_INTERVAL_MINUTES * 60))
    print(f"[INFO] Старт главного цикла. Интервал: {CHECK_INTERVAL_MINUTES} минут.")

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            print(f"[FATAL] Необработанное исключение в итерации: {e}")
        print(f"[INFO] Спим {CHECK_INTERVAL_MINUTES} минут...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
