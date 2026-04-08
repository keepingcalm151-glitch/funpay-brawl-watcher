# watcher.py
#
# Воркер для Railway:
#   Procfile: worker: python watcher.py
#
# Логика:
#   - каждые N минут заходим на страницу аккаунтов Brawl Stars на FunPay
#   - парсим список офферов (ссылки, цена, количество бойцов)
#   - при необходимости заходим в конкретный оффер, чтобы уточнить кол-во бойцов
#   - фильтруем по твоим правилам выгодности
#   - отправляем подходящие варианты в Telegram

import random
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
CHECK_INTERVAL_SECONDS: int = int(config.get("check_interval_seconds", 60))

BASE_URL: str = config.get("base_url", "https://funpay.com").rstrip("/")
BRAWL_ACCOUNTS_URL: str = config.get(
    "brawl_accounts_url",
    f"{BASE_URL}/lots/436/"
)

MAX_SIGNALS_PER_DAY: int = int(config.get("max_signals_per_day", 100))


# ===== 2. Локальное состояние (state.json) =====

def load_state() -> dict:
    """
    Загружаем state.json (память о уже отправленных/просмотренных офферах).
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


# ===== 4. Структуры данных =====

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
    или просто "Бойцов: 14" / "Бравлеров: 14".

    Берём первое найденное число после слов "Бойцов" или "Бравлеров".
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

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


def is_description_forbidden(html: str) -> bool:
    """
    Проверяем подробное описание оффера на наличие фраз,
    означающих, что перепривязка/смена почты недоступна.

    Ищем в блоке "Подробное описание" текст и проверяем по ключевым словам.
    """
    soup = BeautifulSoup(html, "html.parser")

    # находим param-item, где <h5>Подробное описание</h5>
    detailed_block = None
    for item in soup.find_all("div", class_="param-item"):
        h5 = item.find("h5")
        if not h5:
            continue
        title = h5.get_text(" ", strip=True).lower()
        if "подробное описание" in title:
            detailed_block = item
            break

    if not detailed_block:
        return False  # нет подробного описания — не режем по этой причине

    # текст самого описания
    text_div = detailed_block.find("div")
    if not text_div:
        return False

    description_text = text_div.get_text(" ", strip=True).lower()

    # список фраз, по которым считаем оффер неподходящим
    forbidden_phrases = [
        "перевязку не делаю",
        "перепривязку не делаю",
        "перевязка недоступна",
        "перепривязка недоступна",
        "без перепривязки",
        "без перевязки",
        "не делаю перевязку",
        "не делаю перепривязку",
        "нет перепривязки",
        "нет перевязки",
        "пп не делаю",
        "без пп",
        "пп недоступна",
    ]

    for phrase in forbidden_phrases:
        if phrase in description_text:
            return True

    return False


# ===== 6. Сбор офферов (с пропуском уже просмотренных) =====

def collect_offers(state: dict) -> List[Offer]:
    """
    Загружаем страницу со всеми аккаунтами Brawl Stars и парсим офферы.

    На этом этапе:
      - НЕ ходим во внутренние страницы офферов;
      - НЕ читаем описания;
      - heroes берём только из data-f-hero (если его нет — оффер пропускаем);
      - запоминаем только новые офферы (по seen_offers).
    """
    html = fetch_page(BRAWL_ACCOUNTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    offers: List[Offer] = []
    seen_offers: Dict[str, bool] = state.setdefault("seen_offers", {})

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

        # уже видели — пропускаем
        if seen_offers.get(offer_id):
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

        # Кол-во бойцов только из data-f-hero
        heroes_raw = a.get("data-f-hero")
        if not heroes_raw:
            continue
        try:
            heroes_count = int(heroes_raw)
        except ValueError:
            continue

        # Краткое описание
        desc_div = a.find("div", class_="tc-desc-text")
        title = desc_div.get_text(" ", strip=True) if desc_div else ""

        # Ник продавца
        seller_div = a.find("div", class_="media-user-name")
        seller_name = seller_div.get_text(" ", strip=True) if seller_div else ""

        offer = Offer(
            offer_id=offer_id,
            url=offer_url,
            price_rub=price_val,
            heroes=heroes_count,
            title=title,
            seller_name=seller_name,
        )
        offers.append(offer)

        # помечаем как просмотренный
        seen_offers[offer_id] = True

    save_state(state)
    print(f"[INFO] На странице Brawl Stars найдено офферов (новых): {len(offers)}")
    return offers


# ===== 7. Выгодность по бойцам и цене =====

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
    if 100 <= heroes <= 130:
        return 100.0, 1400.0
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
    t = max(0.0, min(1.0, t))
    percent = 30.0 + t * (100.0 - 30.0)

    if percent <= 50.0:
        return "Блестящая"
    if percent <= 70.0:
        return "Средняя"
    return None


def filter_profitable_offers(offers: List[Offer]) -> List[Offer]:
    """
    Фильтрация офферов по количеству бойцов и цене.

    Логика:
      - если heroes отсутствует или < 70 или > 170 — оффер не рассматриваем (get_price_range_for_heroes вернёт None);
      - по heroes выбираем базовый ценовой диапазон (min, max);
      - глобальный нижний порог: цена >= 200 ₽;
      - жёсткий нижний порог по диапазону: price_rub >= price_min;
      - мягкий верхний порог: price_rub <= price_max + 40 (если хочешь чуть дороже).
    """
    profitable: List[Offer] = []

    GLOBAL_MIN_PRICE = 200.0
    EXTRA_ABOVE_MAX = 40.0  # допуск выше верхней границы диапазона

    for offer in offers:
        if offer.heroes is None:
            continue

        heroes = offer.heroes
        price = offer.price_rub

        # глобальный минимум цены
        if price < GLOBAL_MIN_PRICE:
            continue

        rng = get_price_range_for_heroes(heroes)
        if rng is None:
            continue

        price_min, price_max = rng
        soft_max = price_max + EXTRA_ABOVE_MAX

        # отсекаем и ниже диапазона, и сильно выше
        if price < price_min or price > soft_max:
            continue

        profitable.append(offer)

    return profitable


def get_brawlers_base_range(heroes: int) -> tuple[int, float]:
    """
    По количеству бойцов возвращаем:
      (нижняя_граница_диапазона, базовая_цена_в_рублях)

    Диапазоны и базовые цены:
      70–79  -> от 70 бойцов, 300 ₽
      80–84  -> от 80 бойцов, 420 ₽
      85–89  -> от 85 бойцов, 450 ₽
      90–94  -> от 90 бойцов, 650 ₽
      95–99  -> от 95 бойцов, 700 ₽
      100+   -> от 100 бойцов, 1000 ₽

    Если heroes < 70 — всё равно возвращаем (70, 300) как базу по умолчанию.
    """
    if heroes < 70:
        return 70, 300.0

    if 70 <= heroes <= 79:
        return 70, 300.0
    if 80 <= heroes <= 84:
        return 80, 420.0
    if 85 <= heroes <= 89:
        return 85, 450.0
    if 90 <= heroes <= 94:
        return 90, 650.0
    if 95 <= heroes <= 99:
        return 95, 700.0

    # 100 и выше
    return 100, 1000.0


def format_offer_message(offer: Offer) -> str:
    """
    Формируем текст для Telegram по офферу с учётом
    разницы относительно базового диапазона.
    Пример:
      Найден аккаунт:
      Бойцов: 74
      Стоимость: 327.19 ₽ на +27 рублей относительно диапазона от 70 бойцов
      Ссылка: ...
    """
    heroes = offer.heroes if offer.heroes is not None else "неизвестно"
    price = offer.price_rub

    # если количество бойцов известно — считаем разницу
    diff_text = ""
    if isinstance(heroes, int):
        base_from, base_price = get_brawlers_base_range(heroes)
        delta = price - base_price
        delta_rounded = round(delta)

        if delta_rounded > 0:
            sign = "+"
        elif delta_rounded < 0:
            sign = "−"  # можно заменить на "-"
        else:
            sign = "±"

        diff_text = f" на {sign}{abs(delta_rounded)} рублей относительно диапазона от {base_from} бойцов"

    lines: List[str] = []
    lines.append("Найден аккаунт:")
    lines.append(f"Бойцов: {heroes}")
    lines.append(f"Стоимость: {price:.2f} ₽{diff_text}")
    lines.append(f"Ссылка: {offer.url}")

    return "\n".join(lines)


# ===== 8. Отправка выгодных офферов в Telegram =====

def send_new_offers_to_telegram(offers: List[Offer], state: dict) -> None:
    """
    Отправляем новые выгодные офферы в Telegram
    и помечаем их в state, чтобы не дублировать.
    Перед отправкой дополнительно проверяем подробное описание.
    """
    if not offers:
        print("[INFO] Нет новых выгодных офферов для отправки.")
        return

    sent_offers: Dict[str, bool] = state.setdefault("sent_offers", {})
    sent_now = 0

    for offer in offers:
        if sent_offers.get(offer.offer_id):
            continue

        # Дополнительная проверка: подробное описание (перепривязка и т.п.)
        offer_html = None
        try:
            offer_html = fetch_page(offer.url)
        except Exception as e:
            print(f"[WARN] Не удалось загрузить HTML оффера {offer.offer_id} для проверки описания: {e}")

        if offer_html is not None and is_description_forbidden(offer_html):
            print(f"[INFO] Оффер {offer.offer_id} отфильтрован по описанию (перепривязка недоступна).")
            # здесь намеренно НЕ помечаем как sent_offers, чтобы в будущем можно было
            # изменить правила и пересмотреть такие офферы, если захочешь
            continue

        text = format_offer_message(offer)

        try:
            heroes_log = offer.heroes if offer.heroes is not None else "неизвестно"
            print(
                f"[INFO] Отправляем оффер {offer.offer_id} "
                f"({offer.price_rub:.2f} ₽, {heroes_log} бойцов)"
            )
            send_telegram_message(text)
            sent_offers[offer.offer_id] = True
            save_state(state)
            sent_now += 1
            if sent_now >= MAX_SIGNALS_PER_DAY:
                print("[INFO] Достигнут дневной лимит отправки офферов.")
                break
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")


# ===== 9. Основной цикл =====

def run_single_iteration() -> None:
    """
    Одна полная итерация:
      - загрузить состояние
      - собрать офферы (только новые)
      - отфильтровать выгодные
      - отправить в Telegram
    """
    print("=" * 60)
    print("[INFO] Запуск проверки FunPay аккаунтов Brawl Stars")

    state = load_state()

    offers = collect_offers(state)
    print(f"[INFO] Собрано офферов (новых): {len(offers)}")

    profitable = filter_profitable_offers(offers)
    print(f"[INFO] Найдено выгодных офферов: {len(profitable)}")

    if profitable:
        send_new_offers_to_telegram(profitable, state)
    else:
        print("[INFO] Выгодных офферов в этой итерации нет.")


def main_loop() -> None:
    """
    Бесконечный цикл для Railway.
    Интервал читаем в секундах из CHECK_INTERVAL_SECONDS.
    Перед каждой итерацией выбираем случайный интервал вокруг базового,
    чтобы это выглядело как ручное обновление (чуть раньше/чуть позже).
    """
    # читаем базовый интервал из конфига
    try:
        base_interval = int(CHECK_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        base_interval = 5  # дефолт 5 секунд

    # безопасный базовый диапазон: минимум 3, максимум 10
    if base_interval < 3:
        base_interval = 3
    elif base_interval > 10:
        base_interval = 10

    print(
        f"[INFO] Старт главного цикла. "
        f"Базовый интервал: {base_interval} секунд "
        f"(динамический диапазон: base±1)."
    )

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            print(f"[FATAL] Необработанное исключение в итерации: {e}")

        # выбираем случайный интервал: base-1 .. base+1
        low = max(1, base_interval - 1)
        high = base_interval + 1
        interval_sec = random.randint(low, high)

        print(f"[INFO] Спим {interval_sec} секунд...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
