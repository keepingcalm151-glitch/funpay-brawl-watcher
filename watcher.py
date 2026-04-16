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
import re


def extract_heroes_from_text(text: str) -> Optional[int]:
    """
    Пытаемся вытащить количество бойцов/бравлеров из произвольного текста.
    Ловим варианты:
      - "бойцов 85", "бравлеров: 85", "brawlers - 85"
      - "85 бойцов", "85 бравлеров", "85 brawlers"
    """
    if not text:
        return None

    t = text.lower()

    # 1) Слово перед числом: "бойцов 85", "brawlers: 85"
    pattern_word_num = r"(бойц\w*|бравлер\w*|brawler\w*)[:\s\-]+(\d{1,3})"
    m = re.search(pattern_word_num, t, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            pass

    # 2) Число перед словом: "85 бойцов", "85 brawlers"
    pattern_num_word = r"(\d{1,3})[:\s\-]+(бойц\w*|бравлер\w*|brawler\w*)"
    m = re.search(pattern_num_word, t, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


# ===== 0. Ключевые скины и надбавки к цене =====

SKIN_BONUSES = {
    "майор роза": 20,
    "звездочка шелли": 50,
    "челленджер кольт": 50,
    "вирусный 8-бит": 50,
    "торговец гейл": 50,
    "паладин вольт": 50,
    "поко старр": 50,
    "сантамайк": 20,
    "помощница пенни": 50,
    "нита красный нос": 50,
    "ведьма шелли": 50,
    "оборотень леон": 50,
    "гвардеец кольт": 50,
    "кунг-фу брок": 50,
    "соевый дэррил": 20,
    "контрабандистка пенни": 20,
    "8-бит из салуна": 20,
    "волшебник байрон": 20,
    "героиня биби": 100,
    "пельмень дэррил": 50,
    "ниан нита": 20,
    "директор базз": 20,
    "оса бо": 20,
    "лола пантера": 20,
    "кот-воришка джесси": 20,
    "эль губка": 100,
    "сквидварт мортис": 50,
    "планктон": 50,
    "патрик": 50,
    "мистер крабс": 20,
    "вуди кольт": 100,
    "бо пип биби": 100,
    "сэнди джесси": 100,
    "инопланетянин скуик": 20,
    "рекс даг": 100,
    "классическая шелли": 20,
    "красный дракон джесси": 20,
    "squad busters шелли": 20,
    "корсар кольт": 50,
    "б-800": 50,
    "полузащитник булл": 50,
    "король варваров булл": 20,
    "суперрейнджер брок": 20,
    "эль тигро": 50,
    "барли с пирогами": 20,
    "официант барли": 20,
    "трэш-поко": 20,
    "пират поко": 20,
    "скелетная роза": 50,
    "коко роза": 20,
    "кошка-воровка джесси": 20,
    "портье майк": 20,
    "меха годзилла тик": 50,
    "v8-бит": 50,
    "d4r-ry1": 50,
    "безумный карл": 20,
    "карл капитан": 20,
    "серфер карл": 50,
    "сту-панк": 20,
    "безголовый сту": 20,
    "ниндзя эш": 20,
    "белль голдхэнд": 20,
    "трикси колетт": 20,
    "навигатор колетт": 20,
    "инспектор колетт": 20,
    "чола": 20,
    "индиго тара": 50,
    "болотный джин": 20,
    "король лу": 50,
    "плохиш базз": 50,
    "годзилла базз": 20,
}

SKIN_KEYWORDS = {
    "майор роза": ["майор роза", "major rosa", "major rose"],
    "звездная шелли": ["звездная шелли", "звёздная шелли", "star shelly"],
    "челленджер кольт": ["челленджер кольт", "challenger colt"],
    "вирусный 8-бит": ["вирусный 8-бит", "вирус 8-бит", "вирус 8 бит", "virus 8-bit", "virus 8 bit"],
    "торговец гейл": ["торговец гейл", "merchant gale"],
    "паладин вольт": ["паладин вольт", "паладин вольт-меха", "paladin volt"],
    "поко старр": ["поко старр", "poco starr"],
    "сантамайк": ["сантамайк", "santamike", "santa mike", "санта майк"],
    "помощница пенни": ["помощница пенни", "helping penny", "helper penny"],
    "нита красный нос": ["нита красный нос", "red nose nita", "красный нос нита"],
    "ведьма шелли": ["ведьма шелли", "witch shelly"],
    "оборотень леон": ["оборотень леон", "werewolf leon"],
    "гвардеец кольт": ["гвардеец кольт", "guard colt"],
    "кунг-фу брок": ["кунг-фу брок", "кунг фу брок", "kung fu brock", "kung-fu brock"],
    "соевый дэррил": ["соевый деррил", "соевый дэррил", "soy darryl", "soy d4rryl", "соевый d4rry1", "соевый d4r-ry1"],
    "контрабандистка пенни": ["контрабандистка пенни", "smuggler penny"],
    "8-бит из салуна": ["8-бит из салуна", "8 бит из салуна", "saloon 8-bit", "saloon 8 bit"],
    "волшебник байрон": ["волшебник байрон", "wizard byron"],
    "героиня биби": ["героиня биби", "heroine bibi"],
    "пельмень дэррил": ["пельмень дэррил", "dumpling darryl"],
    "ниан нита": ["ниан нита", "nian nita"],
    "директор базз": ["директор базз", "director buzz"],
    "оса бо": ["оса бо", "wasp bo"],
    "лола пантера": ["лола пантера", "lola panther"],
    "кот-воришка джесси": ["кот-воришка джесси", "кот воровка джесси", "cat burglar jessie"],
    "эль губка": ["эль губка", "el esponja", "el sponja", "sponge el"],
    "сквидварт мортис": ["сквидварт мортис", "сквидвард мортис", "squidward mortis"],
    "планктон": ["планктон", "plankton"],
    "патрик": ["патрик", "patrick"],
    "мистер крабс": ["мистер крабс", "mr крабс", "mr krabs", "mister krabs"],
    "вуди кольт": ["вуди кольт", "woody colt"],
    "бо пип биби": ["бо пип биби", "bo peep bibi", "bo pip bibi"],
    "сэнди джесси": ["сэнди джесси", "sandy jessie"],
    "инопланетянин скуик": ["инопланетянин скуик", "alien squeak"],
    "рекс даг": ["рекс даг", "rex doug", "rex dag"],
    "классическая шелли": ["классическая шелли", "classic shelly"],
    "красный дракон джесси": ["красный дракон джесси", "red dragon jessie"],
    "мстительная биби": ["мстительная биби", "vengeful bibi"],
    "squad busters шелли": ["squad busters шелли", "squad busters shelly"],
    "корсар кольт": ["корсар кольт", "corsair colt"],
    "б-800": ["б-800", "б 800", "b-800", "b800"],
    "полузащитник булл": ["полузащитник булл", "midfielder bull"],
    "король варваров булл": ["король варваров булл", "king barbarian bull", "barbarian king bull"],
    "суперрейнджер брок": ["суперрейнджер брок", "super ranger brock", "superranger brock"],
    "эль корасон": ["эль корасон", "el corazon", "el corazón"],
    "бэйби шарк примо": ["бэйби шарк примо", "бэби шарк примо", "baby shark primo"],
    "эль тигро": ["эль тигро", "el tigro", "el tigre"],
    "барли с пирогами": ["барли с пирогами", "bakesale barley", "pie barley"],
    "официант барли": ["официант барли", "waiter barley"],
    "трэш-поко": ["трэш-поко", "trash poco", "trash-poco"],
    "пират поко": ["пират поко", "pirate poco"],
    "скелетная роза": ["скелетная роза", "skeleton rosa"],
    "коко роза": ["коко роза", "coco rosa"],
    "кошка-воровка джесси": ["кошка-воровка джесси", "кошка воровка джесси", "cat burglar jessie"],
    "кукольная джесси": ["кукольная джесси", "puppet jessie"],
    "портье майк": ["портье майк", "bellhop mike", "doorman mike"],
    "меха годзилла тик": ["меха годзилла тик", "mecha godzilla tick", "mecha godzila tick"],
    "v8-бит": ["v8-бит", "v8 бит", "v8-bit", "v8 bit"],
    "d4r-ry1": ["d4r-ry1", "d4rry1", "d4rryl", "d4r ry1"],
    "безумный карл": ["безумный карл", "mad scientist carl", "mad carl"],
    "карл капитан": ["карл капитан", "captain carl"],
    "серфер карл": ["серфер карл", "surfer carl"],
    "сту-панк": ["сту-панк", "stu-punk", "punk stu"],
    "безголовый сту": ["безголовый сту", "headless stu"],
    "ниндзя эш": ["ниндзя эш", "ninja ash"],
    "белль голдхэнд": ["белль голдхэнд", "belle goldhand", "goldhand belle"],
    "трикси колетт": ["трикси колетт", "trixie collette", "trixie colette", "trixie collett"],
    "навигатор колетт": ["навигатор колетт", "navigator collette", "navigator colette"],
    "инспектор колетт": ["инспектор колетт", "inspector collette", "inspector colette"],
    "чола": ["чола", "chola"],
    "индиго тара": ["индиго тара", "indigo tara"],
    "уличная тара": ["уличная тара", "streetwear tara", "street tara"],
    "болотный джин": ["болотный джин", "swamp gene"],
    "король лу": ["король лу", "king lou"],
    "плохиш базз": ["плохиш базз", "bad buzz"],
    "годзилла базз": ["годзилла базз", "godzilla buzz"],
}


def bonus_for_skins(text: str) -> float:
    """
    Суммируем надбавку по всем скинам, которые нашли в названии.
    """
    total = 0.0
    t = (text or "").lower()
    for skin_name, variants in SKIN_KEYWORDS.items():
        if any(v in t for v in variants):
            total += SKIN_BONUSES.get(skin_name, 0)
    return total

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


def skins_bonus_breakdown(text: str) -> list[tuple[str, float]]:
    """
    Возвращает список (название_скина, бонус), которые нашли в названии.
    """
    result: list[tuple[str, float]] = []
    t = (text or "").lower()
    for skin_name, variants in SKIN_KEYWORDS.items():
        if any(v in t for v in variants):
            bonus = float(SKIN_BONUSES.get(skin_name, 0))
            if bonus > 0:
                result.append((skin_name, bonus))
    return result


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
    cups: Optional[int]    # кубки (трофеи), если знаем
    title: str             # краткое описание
    seller_name: str       # ник продавца


# ===== 5. Парсинг страницы оффера: бойцы =====

def extract_heroes_from_text(text: str) -> Optional[int]:
    """
    Пытаемся вытащить количество бойцов/бравлеров из произвольного текста,
    даже если там опечатки.

    Логика:
      - проверяем, есть ли в тексте хоть какие-то "похожие" куски на слово
        бойцы/бравлеры/brawl (с возможными ошибками);
      - если есть — ищем все 2–3-значные числа 50–150 и берём самое "правдоподобное".
    """
    if not text:
        return None

    t = text.lower()

    # Фрагменты, по которым считаем, что речь идёт про бойцов.
    # Сюда можно добавлять свои варианты опечаток.
    keyword_fragments = [
        "бойц",    # бойцы, бойцов, боцы, бойцев и т.д.
        "бравл",   # бравлер, бравлеров, браул, бравлeр и т.д.
        "браул",
        "брол",
        "броул",
        "brawl",
        "brawler",
        "brawlers",
    ]

    if not any(k in t for k in keyword_fragments):
        # В тексте вообще нет ничего похожего на "бойцы/бравлеры" — выходим
        return None

    # Ищем ВСЕ числа в тексте
    nums = re.findall(r"(\d{2,3})", t)
    if not nums:
        return None

    # Фильтруем по разумному диапазону для количества бойцов
    candidates = []
    for s in nums:
        try:
            n = int(s)
        except ValueError:
            continue
        if 50 <= n <= 150:  # можно подправить диапазон под свои реальные данные
            candidates.append(n)

    if not candidates:
        return None

    # Берём, например, максимальное (чаще всего реальное число бойцов больше прочих чисел)
    return max(candidates)


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
        "аренда",
        "БЕЗ ДОСТУПА К ПОЧТЕ",
        "без доступа к почте",
        "АРЕНДА",
        "В АРЕНДУ",
        "в аренду",
        "на время",
        "НА ВРЕМЯ",
        "ЕСТЬ ПАССАЖИР",
        "есть пассажир",
    ]

    for phrase in forbidden_phrases:
        if phrase in description_text:
            return True

    return False


# ===== 6. Сбор офферов (с пропуском уже просмотренных) =====

def collect_offers(state: dict) -> List[Offer]:
    """
    Загружаем страницу со всеми аккаунтами Brawl Stars и парсим офферы.

    Логика:
      - Сначала пытаемся взять количество бойцов из data-f-hero.
      - Если data-f-hero нет или оно битое — пробуем вытащить из названия карточки.
      - Если в названии нет — заходим внутрь оффера и ищем число бойцов
        в тексте страницы (бойцы/бравлеры/brawlers).
      - Если даже так не нашли бойцов — оффер пропускаем.
      - Запоминаем только новые офферы (по seen_offers).
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
            text_price = price_div.get_text(" ", strip=True).replace(",", ".")
            price_val = None
            for tok in text_price.split():
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

        # Краткое описание (нужно раньше, потому что будем по нему тоже искать бойцов)
        desc_div = a.find("div", class_="tc-desc-text")
        title = desc_div.get_text(" ", strip=True) if desc_div else ""

        # Ник продавца
        seller_div = a.find("div", class_="media-user-name")
        seller_name = seller_div.get_text(" ", strip=True) if seller_div else ""

        # 1) Пытаемся взять количество бойцов из data-f-hero
        heroes_count: Optional[int] = None
        heroes_raw = a.get("data-f-hero")
        if heroes_raw:
            try:
                heroes_count = int(heroes_raw)
            except ValueError:
                heroes_count = None

        # 2) Если data-f-hero нет или битое — пытаемся достать из названия карточки
        if heroes_count is None and title:
            heroes_from_title = extract_heroes_from_text(title)
            if heroes_from_title is not None:
                heroes_count = heroes_from_title

        # 3) Если в названии тоже не нашли — идём внутрь оффера и ищем по полному описанию
        if heroes_count is None:
            try:
                offer_html = fetch_page(offer_url)
                heroes_from_html = extract_heroes_from_offer_html(offer_html)
                heroes_count = heroes_from_html
            except Exception as e:
                print(f"[WARN] Не удалось получить бойцов из оффера {offer_id}: {e}")
                heroes_count = None

        # если так и не нашли количество бойцов — этот оффер пропускаем
        if heroes_count is None:
            continue

        # Кубки из data-f-cup, если есть
        cups_raw = a.get("data-f-cup")
        cups_count: Optional[int] = None
        if cups_raw:
            try:
                cups_count = int(cups_raw)
            except ValueError:
                cups_count = None

        offer = Offer(
            offer_id=offer_id,
            url=offer_url,
            price_rub=price_val,
            heroes=heroes_count,
            cups=cups_count,
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
    if 100 <= heroes <= 120:
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
    t = max(0.0, min(1.0, t))
    percent = 30.0 + t * (100.0 - 30.0)

    if percent <= 50.0:
        return "Блестящая"
    if percent <= 70.0:
        return "Средняя"
    return None


def filter_profitable_offers(offers: List[Offer]) -> List[Offer]:
    """
    Фильтрация офферов по количеству бойцов, цене и кубкам.
    ...
    """
    profitable: List[Offer] = []

    # логируем, сколько офферов пришло на вход фильтра
    print(f"[DEBUG] Перед фильтром выгодности офферов: {len(offers)}")

    GLOBAL_MIN_PRICE = 200.0
    EXTRA_ABOVE_MAX = 100.0
    MIN_CUPS = 10000

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

        # фильтр по кубкам: если знаем кубки и их меньше минимума — отбрасываем
        if offer.cups is not None and offer.cups < MIN_CUPS:
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

        # Разбивка по скинам и суммарный бонус
        bonus_details = skins_bonus_breakdown(offer.title)
        skins_bonus_total = sum(b for _, b in bonus_details)
        base_price += skins_bonus_total

        delta = price - base_price
        delta_rounded = round(delta)

        if delta_rounded > 0:
            sign = "+"
        elif delta_rounded < 0:
            sign = "−"
        else:
            sign = "±"

        diff_text = f" на {sign}{abs(delta_rounded)} рублей относительно диапазона от {base_from} бойцов"

        # Если были учтены скины — дописываем, какие именно
        if bonus_details:
            parts = [f"{name} (+{int(b)}₽)" for name, b in bonus_details]
            skins_line = "; ".join(parts)
            diff_text += f"\nСкины учтены в цене: {skins_line}"

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
    Теперь проверяем сайт примерно раз в секунду
    с небольшим рандомом (от 1.0 до 2.0 секунд между запросами).
    """
    print(
        "[INFO] Старт главного цикла. "
        "Интервал проверки ~1 секунда (динамический диапазон 1.0–2.0 сек)."
    )

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            print(f"[FATAL] Необработанное исключение в итерации: {e}")

        # случайный интервал в диапазоне [1.0, 2.0] секунд
        interval_sec = random.uniform(1.0, 2.0)

        print(f"[INFO] Спим {interval_sec:.2f} секунд...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
