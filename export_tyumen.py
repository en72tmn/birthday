import csv
import json
import os
import time
from typing import List, Optional, Set, Tuple

import requests

# ========== НАСТРОЙКИ ==========
API_KEY = "8ea63ed5-5003-4ab9-91ba-53782c450517"

# Город и регион нужны, чтобы:
# - region_id: найти rubric_id через /2.0/catalog/rubric/search (Categories API)
# - city_id: выгружать компании через /3.0/items (Places API)
CITY_NAME = "Тюмень"   # можно оставить так
STOP_AT = 50_000       # остановка после N уникальных названий

# Приоритетные категории (как ты дал)
PRIORITY_CATEGORIES = [
    "Поесть",
    "Автосервис",
    "Красота",
    "Развлечения",
    "Медицина",
    "Автотовары",
    "Продукты",
    "Товары",
    "Услуги",
    "Туризм",
    "Спецмагазины",
    "Спорт",
    "Образование",
    "Ремонт, стройка",
    "Пром. товары",
]

# Ограничители/скорость
PAGE_SIZE = 50              # для /3.0/items
SLEEP_SEC = 0.2             # пауза между запросами
MAX_RUBRICS_PER_CATEGORY = 50  # сколько rubric_id брать на одну категорию из поиска

# Файлы
OUT_CSV = "tyumen_names.csv"
CHECKPOINT_JSON = "checkpoint.json"

# API endpoints
REGION_SEARCH_URL = "https://catalog.api.2gis.com/2.0/region/search"
RUBRIC_SEARCH_URL = "https://catalog.api.2gis.com/2.0/catalog/rubric/search"
ITEMS_URL = "https://catalog.api.2gis.com/3.0/items"
GEOCODE_URL = "https://catalog.api.2gis.com/3.0/items/geocode"


# ========== УТИЛИТЫ ==========

def safe_get(data: dict, path: List[str], default=None):
    cur = data
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_checkpoint() -> Optional[dict]:
    if os.path.exists(CHECKPOINT_JSON):
        with open(CHECKPOINT_JSON, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return None


def save_checkpoint(state: dict) -> None:
    tmp = CHECKPOINT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, CHECKPOINT_JSON)


def ensure_csv_header() -> None:
    if not os.path.exists(OUT_CSV):
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name"])


def append_names_to_csv(names: List[str]) -> None:
    if not names:
        return
    with open(OUT_CSV, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for name in names:
            writer.writerow([name])


def read_existing_names(limit: int = 200_000) -> Set[str]:
    """
    Чтобы резюмировать и продолжать после перезапуска —
    читаем уже сохранённые имена из CSV.
    limit — защитный лимит на случай очень больших файлов.
    """
    seen: Set[str] = set()
    if not os.path.exists(OUT_CSV):
        return seen

    with open(OUT_CSV, "r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for index, row in enumerate(reader, start=1):
            if index > limit:
                break
            if row and row[0]:
                seen.add(row[0].strip())
    return seen


# ========== 2ГИС: получение region_id и city_id ==========

def get_region_id(city_name: str) -> str:
    # /2.0/region/search?q=Тюмень&key=...
    params = {"q": city_name, "key": API_KEY}
    response = requests.get(REGION_SEARCH_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    items = safe_get(data, ["result", "items"], [])
    if not items:
        raise RuntimeError(f"Не нашёл region_id для '{city_name}'. Ответ: {data}")
    return str(items[0]["id"])  # берём первый матч


def get_city_id(city_name: str) -> str:
    # /3.0/items/geocode?q=Тюмень&type=adm_div.city&key=...
    params = {"q": city_name, "type": "adm_div.city", "key": API_KEY}
    response = requests.get(GEOCODE_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    items = safe_get(data, ["result", "items"], [])
    if not items:
        raise RuntimeError(f"Не нашёл city_id для '{city_name}'. Ответ: {data}")
    # У geocode обычно id вида "4504222..._..." — city_id нужен до "_" (часто в примерах так),
    # но Places API принимает и city_id целиком в ряде сценариев.
    raw_id = str(items[0]["id"])
    city_id = raw_id.split("_")[0]
    return city_id


# ========== 2ГИС: поиск rubric_id по тексту категории ==========

def search_rubrics(region_id: str, query: str) -> List[Tuple[str, str]]:
    """
    Возвращает список (rubric_id, rubric_name) по запросу query
    через /2.0/catalog/rubric/search
    """
    results: List[Tuple[str, str]] = []
    page = 1
    while len(results) < MAX_RUBRICS_PER_CATEGORY:
        params = {
            "q": query,
            "region_id": region_id,
            "key": API_KEY,
            "page": page,
            "page_size": 20,
        }
        response = requests.get(RUBRIC_SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = safe_get(data, ["result", "items"], [])
        if not items:
            break

        for item in items:
            rubric_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if rubric_id:
                results.append((rubric_id, name))
            if len(results) >= MAX_RUBRICS_PER_CATEGORY:
                break

        page += 1
        time.sleep(SLEEP_SEC)

    return results


# ========== 2ГИС: выгрузка компаний по rubric_id ==========

def fetch_company_names_by_rubric(city_id: str, rubric_id: str, page: int) -> List[str]:
    """
    /3.0/items?rubric_id=...&city_id=...&type=branch&page=...&page_size=...
    """
    params = {
        "key": API_KEY,
        "rubric_id": rubric_id,
        "city_id": city_id,
        "type": "branch",
        "page": page,
        "page_size": PAGE_SIZE,
        "fields": "items.name",
    }
    response = requests.get(ITEMS_URL, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    items = safe_get(data, ["result", "items"], [])
    names = []
    for item in items:
        name = item.get("name")
        if name:
            names.append(str(name).strip())
    return names


# ========== ОСНОВНОЙ ПРОЦЕСС (с чекпоинтом) ==========

def main() -> None:
    ensure_csv_header()
    seen = read_existing_names()

    checkpoint = load_checkpoint()

    print("Уже сохранено уникальных имён:", len(seen))

    region_id = None
    city_id = None

    # Если в чекпоинте уже есть region_id/city_id — используем их
    if checkpoint and checkpoint.get("region_id") and checkpoint.get("city_id"):
        region_id = checkpoint["region_id"]
        city_id = checkpoint["city_id"]
    else:
        region_id = get_region_id(CITY_NAME)
        city_id = get_city_id(CITY_NAME)

    # Собираем план: категории -> список rubric_id
    # (чтобы при продолжении не “дрейфовали” рубрики, сохраняем их в чекпоинт)
    if checkpoint and checkpoint.get("plan"):
        plan = checkpoint["plan"]
    else:
        plan = []
        for category in PRIORITY_CATEGORIES:
            rubrics = search_rubrics(region_id, category)
            # оставляем только id, но для удобства сохраняем и имя рубрики
            plan.append({
                "category": category,
                "rubrics": [{"id": rid, "name": rname} for rid, rname in rubrics]
            })
            time.sleep(SLEEP_SEC)

        checkpoint = checkpoint or {}
        checkpoint.update({
            "region_id": region_id,
            "city_id": city_id,
            "plan": plan,
            "category_index": 0,
            "rubric_index": 0,
            "page": 1,
            "unique_count": len(seen),
        })
        save_checkpoint(checkpoint)

    # Восстанавливаем позицию
    category_index = int(checkpoint.get("category_index", 0))
    rubric_index = int(checkpoint.get("rubric_index", 0))
    page = int(checkpoint.get("page", 1))

    plan = checkpoint["plan"]

    # Идём по категориям -> по rubric -> по страницам
    for ci in range(category_index, len(plan)):
        category = plan[ci]["category"]
        rubrics = plan[ci]["rubrics"]

        start_ri = rubric_index if ci == category_index else 0

        for ri in range(start_ri, len(rubrics)):
            rubric_id = rubrics[ri]["id"]
            rubric_name = rubrics[ri].get("name", "")

            start_page = page if (ci == category_index and ri == start_ri) else 1
            current_page = start_page

            while True:
                names = fetch_company_names_by_rubric(city_id, rubric_id, current_page)

                # пустая страница => рубрика закончилась
                if not names:
                    break

                new_names = []
                for name in names:
                    if name and name not in seen:
                        seen.add(name)
                        new_names.append(name)

                append_names_to_csv(new_names)

                # обновляем чекпоинт
                checkpoint.update({
                    "category_index": ci,
                    "rubric_index": ri,
                    "page": current_page + 1,
                    "unique_count": len(seen),
                    "last": {
                        "category": category,
                        "rubric_id": rubric_id,
                        "rubric_name": rubric_name,
                        "page_just_processed": current_page,
                    },
                })
                save_checkpoint(checkpoint)

                # прогресс в консоль
                if current_page % 5 == 0:
                    print(
                        f"[{len(seen):,}] {category} -> {rubric_name} "
                        f"(rubric {rubric_id}) page {current_page}"
                    )

                # стоп на 50k
                if len(seen) >= STOP_AT:
                    print(f"\nДостигли STOP_AT={STOP_AT}. Остановились.")
                    print("Чекпоинт сохранён в", CHECKPOINT_JSON)
                    print("Файл имён:", OUT_CSV)
                    return

                current_page += 1
                time.sleep(SLEEP_SEC)

        # после завершения категории сбрасываем page/rubric_index
        rubric_index = 0
        page = 1

    print("\nПлан завершён. Уникальных имён:", len(seen))
    print("Чекпоинт:", CHECKPOINT_JSON)
    print("Файл:", OUT_CSV)


if __name__ == "__main__":
    main()
