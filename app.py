import re
import time
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st

MAX_PAGES = 10        # Apple отдаёт не более 10 страниц
MAX_RETRIES = 3       # число попыток на одну страницу
TIMEOUT = 15          # таймаут запроса, секунды
PAUSE = 1.0           # пауза между страницами, секунды

RSS_URL = ("https://itunes.apple.com/{country}/rss/customerreviews/"
           "page={page}/id={app_id}/sortby=mostrecent/json")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ReviewCollector/1.0)"}

COLUMNS = ["review_date", "rating", "title", "review_text", "author", "app_version"]


# ---------------------------------------------------------------------------
# Логика сбора данных
# ---------------------------------------------------------------------------

def parse_app_url(url):
    """Извлекает (country, app_id) из ссылки на приложение в App Store.
    При ошибке выбрасывает ValueError с понятным сообщением."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Ссылка пустая.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parsed = urlparse(url)  # параметры (?l=ru) и #fragment отделяются автоматически
    host = (parsed.netloc or "").lower()
    if not host.endswith("apple.com"):
        raise ValueError("Это не похоже на ссылку App Store (ожидается apps.apple.com).")

    segments = [s for s in parsed.path.split("/") if s]

    # Код страны — первый сегмент пути, ровно две буквы
    if not segments or not re.fullmatch(r"[A-Za-z]{2}", segments[0]):
        raise ValueError("Не удалось определить код страны (ожидается вид .../us/app/...).")
    country = segments[0].lower()

    # Числовой ID — число после "id" в пути
    match = re.search(r"(?:^|/|-)id(\d+)(?:/|$)", parsed.path)
    if not match:
        raise ValueError("Не удалось найти числовой ID приложения (ожидается ...id570060128).")
    app_id = match.group(1)

    return country, app_id


def fetch_page(country, app_id, page, log=None):
    """Загружает одну страницу RSS и возвращает JSON (dict).
    До MAX_RETRIES попыток при сетевых ошибках, 429 и 5xx.
    Если все попытки неудачны — выбрасывает RuntimeError."""
    url = RSS_URL.format(country=country, app_id=app_id, page=page)
    last_error = "неизвестная ошибка"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
            elif resp.status_code == 200:
                return resp.json()
            else:
                # Остальные коды (например, 404) повторять бессмысленно
                raise RuntimeError(f"HTTP {resp.status_code}")
        except (requests.exceptions.RequestException, ValueError) as e:
            # ValueError — невалидный JSON в ответе
            last_error = f"{type(e).__name__}: {e}"

        if attempt < MAX_RETRIES:
            wait = 2 ** attempt  # экспоненциальная пауза: 2, 4 сек
            if log:
                log(f"⚠ Страница {page}: попытка {attempt}/{MAX_RETRIES} не удалась "
                    f"({last_error}). Повтор через {wait} с...")
            time.sleep(wait)

    raise RuntimeError(last_error)


def _label(entry, *path):
    """Безопасно достаёт вложенное поле вида entry[a][b]...; пустая строка, если нет."""
    cur = entry
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return ""
        cur = cur[key]
    return cur if cur is not None else ""


def _format_date(raw):
    """Приводит дату из updated.label к формату YYYY-MM-DD HH:MM:SS."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(raw)  # если формат неожиданный — оставляем как есть


def parse_reviews(data):
    """Превращает JSON страницы в список словарей-отзывов."""
    entries = _label(data, "feed", "entry")
    if not entries:
        return []
    if isinstance(entries, dict):  # единственная запись приходит как словарь
        entries = [entries]

    reviews = []
    for entry in entries:
        # Метаданные приложения не содержат рейтинга — пропускаем
        if not isinstance(entry, dict) or "im:rating" not in entry:
            continue

        try:
            rating = int(_label(entry, "im:rating", "label"))
        except (TypeError, ValueError):
            rating = None

        reviews.append({
            "review_date": _format_date(_label(entry, "updated", "label")),
            "rating": rating,
            "title": _label(entry, "title", "label"),
            "review_text": _label(entry, "content", "label"),
            "author": _label(entry, "author", "name", "label"),
            "app_version": _label(entry, "im:version", "label"),
        })
    return reviews


def collect_all_reviews(country, app_id, on_progress=None, log=None):
    """Последовательно собирает отзывы со страниц 1..MAX_PAGES.
    on_progress(page, total, n_on_page) вызывается после каждой страницы.
    Возвращает (список отзывов, номер страницы с ошибкой или None)."""
    all_reviews = []
    failed_page = None

    for page in range(1, MAX_PAGES + 1):
        try:
            data = fetch_page(country, app_id, page, log=log)
        except RuntimeError as e:
            failed_page = page
            if log:
                log(f"❌ Страница {page}/{MAX_PAGES}: не удалось получить ({e}). "
                    f"Сохраняю уже собранное.")
            break

        reviews = parse_reviews(data)
        if not reviews:
            if log:
                log(f"Страница {page}/{MAX_PAGES}: отзывов нет — останавливаюсь.")
            break

        all_reviews.extend(reviews)
        if on_progress:
            on_progress(page, MAX_PAGES, len(reviews))

        if page < MAX_PAGES:
            time.sleep(PAUSE)

    return all_reviews, failed_page


def build_dataframe(reviews):
    """Собирает DataFrame и удаляет дубликаты (автор + дата + текст)."""
    df = pd.DataFrame(reviews, columns=COLUMNS)
    before = len(df)
    df = df.drop_duplicates(subset=["author", "review_date", "review_text"]).reset_index(drop=True)
    return df, before - len(df)


def to_csv_bytes(df):
    """CSV в utf-8-sig (Excel корректно откроет кириллицу и эмодзи).
    pandas сам экранирует кавычки и переносы строк внутри текста."""
    return df.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Интерфейс Streamlit
# ---------------------------------------------------------------------------

st.set_page_config(page_title="App Store Reviews", page_icon="⭐", layout="wide")
st.title("⭐ Сбор отзывов из App Store")
st.caption("Официальный публичный RSS Apple. Не более ~500 последних отзывов на одну страну.")

with st.form("url_form"):
    link = st.text_input(
        "Ссылка на приложение в App Store",
        placeholder="https://apps.apple.com/us/app/duolingo-language-lessons/id570060128",
    )
    submitted = st.form_submit_button("Собрать отзывы", type="primary")

if submitted:
    st.session_state.pop("result", None)
    try:
        country, app_id = parse_app_url(link)
    except ValueError as e:
        st.error(f"Ошибка: {e} Попробуйте ещё раз.")
    else:
        st.info(f"Страна: **{country}** · ID приложения: **{app_id}**")
        progress = st.progress(0.0, text="Начинаю сбор...")
        log_box = st.empty()
        messages = []

        def log(msg):
            messages.append(msg)
            log_box.markdown("\n\n".join(messages[-5:]))

        def on_progress(page, total, n):
            progress.progress(page / total, text=f"Страница {page}/{total}: получено {n} отзывов")

        reviews, failed_page = collect_all_reviews(country, app_id, on_progress, log)
        progress.empty()

        if not reviews:
            st.warning("Отзывы не найдены — файл не создаётся.")
        else:
            df, removed = build_dataframe(reviews)
            st.session_state["result"] = {
                "df": df,
                "removed": removed,
                "failed_page": failed_page,
                "filename": f"reviews_{app_id}_{country}.csv",
            }

# Результат хранится в session_state, чтобы переживать перезапуск скрипта
# (например, после нажатия кнопки скачивания)
result = st.session_state.get("result")
if result:
    df = result["df"]

    if result["failed_page"]:
        st.warning(f"Сбор прерван на странице {result['failed_page']}: данные неполные.")
    if result["removed"]:
        st.caption(f"Удалено дубликатов: {result['removed']}")

    # Итоговая статистика
    dates = df["review_date"].replace("", pd.NA).dropna()
    c1, c2, c3 = st.columns(3)
    c1.metric("Всего отзывов", len(df))
    c2.metric("Средний рейтинг", f"{df['rating'].mean():.2f}")
    c3.metric("Диапазон дат",
              f"{dates.min()[:10]} — {dates.max()[:10]}" if not dates.empty else "—")

    st.subheader("Распределение по оценкам")
    dist = df["rating"].value_counts().reindex([1, 2, 3, 4, 5], fill_value=0)
    st.bar_chart(dist.rename("Отзывов"))

    st.subheader("Первые 5 строк")
    st.dataframe(df.head(), use_container_width=True)

    st.download_button(
        "⬇️ Скачать CSV",
        data=to_csv_bytes(df),
        file_name=result["filename"],
        mime="text/csv",
    )

    with st.expander("Все отзывы"):
        st.dataframe(df, use_container_width=True)
