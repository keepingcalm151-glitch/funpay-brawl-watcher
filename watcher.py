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


# ===== 5. Парсинг страницы оффера: бойцы =====

def extract_heroes_from_offer_html(html: str) -> Optional[int]:
    """
    Пытаемся вытащить количество бойцов из страницы конкретного оффера.
    На странице есть блоки вида:
      "👤 Бойцов: 14<br />"
    или просто "Бойцов: 14".

    Берём первое найденное число после слова "Бойцов".
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Ищем "Бойцов:" или "Бравлеров:" и берём ближайшее число
    import re

    patterns = [
        r"[Бб]ойцов[:\s]+(\d+)",
        r"[Бб]равлеров[:\s]+(\d+)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue

    return None


def collect_offers(state: dict) -> List[Offer]:
    """
    Загружаем страницу со всеми аккаунтами Brawl Stars и парсим офферы.

    Ищем элементы вида:
      <a href="https://funpay.com/lots/offer?id=66881057"
         class="tc-item lazyload-hidden"
         data-online="1"
         data-auto="1"
         data-f-cup="1056"
         data-f-hero="14">

      ...

      <div class="tc-price" data-s="157.535642">
          <div>157.54 <span class="unit">₽</span></div>
      </div>

    Нас интересуют:
      - href (ссылка на оффер)
      - data-f-hero (кол-во бойцов, если есть)
      - data-s (цена в рублях)
      - краткое описание (div.tc-desc-text)
      - ник продавца (div.media-user-name)

    Если heroes в data-f-hero нет, пытаемся достать его со страницы оффера.
    """
    html = fetch_page(BRAWL_ACCOUNTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    offers: List[Offer] = []

    for a in soup.find_all("a", class_="tc-item"):
        href = a.get("href")
        if not href:
            continue

        # Полный URL оффера
        if href.startswith("http"):
            offer_url = href
        else:
            offer_url = BASE_URL.rstrip("/") + "/" + href.lstrip("/")

        # ID оффера из параметра ?id=...
        offer_id = None
        if "offer?id=" in offer_url:
            part = offer_url.split("offer?id=", 1)[-1]
            part = part.split("&", 1)[0]
            offer_id = part.strip()
        if not offer_id:
            continue

        # Цена
        price_div = a.find("div", class_="tc-price")
        if not price_div:
            continue
        data_s = price_div.get("data-s")
        if not data_s:
            text = price_div.get_text(" ", strip=True).replace(",", ".")
            price_val = None
            for tok in text.split():
                try:
                    price_val = float(tok)
                    break
                except ValueError:
                    continue
            if price_val is None:
                continue
        else:
            try:
                price_val = float(data_s.replace(",", "."))
            except ValueError:
                continue

        # Кол-во бойцов: сначала пытаемся взять из data-f-hero
        heroes_raw = a.get("data-f-hero")
        heroes_count: Optional[int] = None
        if heroes_raw:
            try:
                heroes_count = int(heroes_raw)
            except ValueError:
                heroes_count = None

        # Если heroes_count всё ещё None — пытаемся вытащить со страницы оффера
        if heroes_count is None:
            try:
                offer_html = fetch_page(offer_url)
                heroes_count = extract_heroes_from_offer_html(offer_html)
            except Exception as e:
                print(f"[WARN] Не удалось получить heroes для оффера {offer_id}: {e}")
                heroes_count = None

        # Краткое описание
        desc_div = a.find("div", class_="tc-desc-text")
        if desc_div:
            title = desc_div.get_text(" ", strip=True)
        else:
            title = ""

        # Ник продавца
        seller_name = ""
        seller_div = a.find("div", class_="media-user-name")
        if seller_div:
            seller_name = seller_div.get_text(" ", strip=True)

        offer = Offer(
            offer_id=offer_id,
            url=offer_url,
            price_rub=price_val,
            heroes=heroes_count,
            title=title,
            seller_name=seller_name,
        )
        offers.append(offer)

    print(f"[INFO] На странице Brawl Stars найдено офферов: {len(offers)}")
    return offers


# ===== 6. Выгодность по бойцам и цене =====

def get_price_range_for_heroes(heroes: int) -> Optional[tuple[float, float]]:
    """
    Возвращает (min_price, max_price) для заданного количества бойцов.
    Диапазоны по ТЗ:

      70–79    -> 100–300
      80–84    -> 100–420
      85–89    -> 100–450
      90–94    -> 100–650
      95–99    -> 100–700
      >= 100   -> 100–1000

    Если heroes < 70 — возвращаем None (оффер нами не интересен).
    """
    if 70 <= heroes <= 79:
        return 100.0, 300.0
    if 80 <= heroes <= 84:
        return 100.0, 420.0
    if 85 <= heroes <= 89:
        return 100.0, 450.0
    if 90 <= heroes <= 94:
        return 100.0, 650.0
    if 95 <= heroes <= 99:
        return 100.0, 700.0
    if heroes >= 100:
        return 100.0, 1000.0
    return None


def calculate_value_label(price: float, price_min: float, price_max: float) -> Optional[str]:
    """
    Считаем "процент цены" внутри диапазона:
      100 руб = 30%
      max     = 100%

    Линейно растягиваем:
      t = (price - price_min) / (price_max - price_min)
      percent = 30 + t * (100 - 30)

    Метки:
      percent <= 50 -> "Блестящая"
      percent <= 70 -> "Средняя"
      иначе         -> None
    """
    if price <= 0 or price_max <= price_min:
        return None

    t = (price - price_min) / (price_max - price_min)
    # зажимаем 0..1 на всякий случай
    t = max(0.0, min(1.0, t))
    percent = 30.0 + t * (100.0 - 30.0)

    if percent <= 50.0:
        return "Блестящая"
    if percent <= 70.0:
        return "Средняя"
    return None


def filter_profitable_offers(offers: List[Offer]) -> List[Offer]:
    """
    Фильтрация офферов по количеству бойцов и цене, с расчётом метки выгодности.

    Логика:
      - если heroes отсутствует или < 70 — оффер не рассматриваем;
      - по heroes выбираем ценовой диапазон (min, max);
      - если price_rub > max — оффер неинтересен;
      - считаем "процент цены" и метку:
            <= 50% -> "Блестящая"
            <= 70% -> "Средняя"
            > 70%  -> метки нет (но оффер всё равно может пройти, если цена <= max);
      - список отфильтрованных офферов возвращаем.
    """
    profitable: List[Offer] = []

    for offer in offers:
        if offer.heroes is None:
            continue
        heroes = offer.heroes
        price = offer.price_rub

        rng = get_price_range_for_heroes(heroes)
        if rng is None:
            continue
        price_min, price_max = rng

        if price > price_max:
            continue

        label = calculate_value_label(price, price_min, price_max)

        # Метку выгодности сохраним в state через поле offer_id -> label,
        # а в send_new_offers_to_telegram будем её подставлять в текст
        offer_value_labels = STATE.get("offer_value_labels", {}) if "STATE" in globals() else {}
        offer_value_labels[offer.offer_id] = label
        if "STATE" in globals():
            STATE["offer_value_labels"] = offer_value_labels

        profitable.append(offer)

    return profitable


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

        heroes = offer.heroes if offer.heroes is not None else "неизвестно"
        price = offer.price_rub

        label_text = ""
        if isinstance(heroes, int):
            rng = get_price_range_for_heroes(heroes)
            if rng is not None:
                price_min, price_max = rng
                label = calculate_value_label(price, price_min, price_max)
                if label:
                    label_text = label

        # Формат сообщения:
        # Найден аккаунт: Бойцов, Стоимость, Метка выгодности (если есть), Ссылка
        parts = [
            f"Найден аккаунт:",
            f"Бойцов: {heroes}",
            f"Стоимость: {price:.2f} ₽",
        ]
        if label_text:
            parts.append(f"Метка выгодности: {label_text}")
        parts.append(f"Ссылка: {offer.url}")

        text = "\n".join(parts)

        try:
            print(f"[INFO] Отправляем оффер {offer.offer_id} ({price:.2f} ₽, {heroes} бойцов, метка: {label_text or 'нет'})")
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
