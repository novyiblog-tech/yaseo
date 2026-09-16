#!/usr/bin/env python3
"""
Русская морфология для ядра: схлопывание словоформ и приведение к именительному.

Зачем модуль. В ядре могут лежать строками «сквозная аналитика» 2276 и
«сквозные аналитики» 2276 — одна и та же фраза в двух формах. Совпадение
частотности не случайно: Wordstat сам приводит слова к начальной форме, и обе
записи — это один и тот же запрос к нему. Хранить их двумя строками значит
дважды считать один спрос.

Отсюда правило схлопывания: **одинаковое мультимножество основ ⇒ один запрос
Wordstat ⇒ одна строка ядра.** Порядок слов при этом не важен — «система
сквозной аналитики» и «аналитика система сквозной» тоже одна строка (обе 133).

Что НЕ схлопывается: разный состав слов. «сквозная аналитика битрикс» 146 и
«сквозная аналитика битрикс24» 116 остаются двумя строками — у них разные
основы и разная частотность, Wordstat считает их разными запросами.

Основы берутся стеммером Snowball для русского (Портер в редакции Snowball).
Стеммер, а не словарь: словаря без внешних зависимостей нет, а для сравнения
двух форм одного слова основы достаточно. Стеммер не знает исключений и на
редких словах ошибается — поэтому он используется только для сравнения форм
между собой, и никогда для показа человеку.

Приведение к именительному (`to_nominative`) работает консервативно и только
там, где падеж заведомо навязан снятым словом: когда из заголовка статьи
«6 уровней маркетинговой аналитики застройщика» снимается счётчик «6 уровней»,
и остаток остаётся в родительном. Управляющее слово ушло — падеж больше ничем
не оправдан. Правится только первая именная группа (прилагательные + первое
существительное); зависимое «застройщика» остаётся, оно управляется головой,
а не снятым счётчиком.

Публичный API:
- stem(word)                      -> основа слова
- stem_key(phrase)                -> ключ схлопывания (мультимножество основ)
- same_query(a, b)                -> одна ли это фраза для Wordstat
- pick_canonical(variants)        -> представитель группы словоформ
- to_nominative(phrase)           -> (фраза, поправлено ли)

Запуск как скрипт — самопроверка на живых примерах:
    python3 -m yaseo.morph --selftest
    python3 -m yaseo.morph --stem "сквозные аналитики"
    python3 -m yaseo.morph --nominative "маркетинговой аналитики застройщика"
"""
from __future__ import annotations

import re
import sys

VOWELS = frozenset("аеиоуыэюя")
WORD_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]+(?:-[0-9a-zA-Zа-яёА-ЯЁ]+)*")

# ── стеммер Snowball (русский) ───────────────────────────────────────────────
# Группы окончаний приводятся в порядке проверки: длинные раньше коротких,
# иначе «ившись» срежется как «сь». Внутри групп порядок значим.

_PERFECTIVE_1 = ("вшись", "вши", "в")                    # только после а/я
_PERFECTIVE_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")
_ADJECTIVE = (
    "ими", "ыми", "его", "ого", "ему", "ому", "ее", "ие", "ые", "ое", "ей", "ий",
    "ый", "ой", "ем", "им", "ым", "ом", "их", "ых", "ую", "юю", "ая", "яя", "ою", "ею",
)
_PARTICIPLE_1 = ("ющ", "нн", "вш", "ем", "щ")            # только после а/я
_PARTICIPLE_2 = ("ующ", "ивш", "ывш")
_REFLEXIVE = ("ся", "сь")
_VERB_1 = (
    "ешь", "нно", "ете", "йте", "ли", "й", "л", "ем", "н", "ло", "но", "ет",
    "ют", "ны", "ть", "ла", "на",
)                                                        # только после а/я
_VERB_2 = (
    "уйте", "ейте", "ила", "ыла", "ена", "ите", "или", "ыли", "ило", "ыло",
    "ено", "ует", "уют", "ены", "ить", "ыть", "ишь", "ей", "уй", "ил", "ыл",
    "им", "ым", "ен", "ят", "ит", "ыт", "ую", "ю",
)
_NOUN = (
    "иями", "ями", "ами", "иях", "ией", "иям", "ием", "ях", "ах", "ии", "ие",
    "ье", "еи", "ей", "ой", "ий", "ям", "ем", "ам", "ом", "ию", "ью", "ия",
    "ья", "ев", "ов", "а", "е", "и", "й", "о", "у", "ы", "ь", "ю", "я",
)
_SUPERLATIVE = ("ейше", "ейш")
_DERIVATIONAL = ("ость", "ост")


def _regions(word: str) -> tuple[int, int, int]:
    """Границы RV, R1, R2 по определению Snowball."""
    rv = len(word)
    for i, ch in enumerate(word):
        if ch in VOWELS:
            rv = i + 1
            break

    def after_vowel_consonant(start: int) -> int:
        for i in range(start, len(word) - 1):
            if word[i] in VOWELS and word[i + 1] not in VOWELS:
                return i + 2
        return len(word)

    r1 = after_vowel_consonant(0)
    r2 = after_vowel_consonant(r1)
    return rv, r1, r2


def _cut(word: str, endings: tuple[str, ...], start: int,
         prefix: str = "") -> tuple[str, bool]:
    """
    Срезает первое подошедшее окончание, если оно целиком лежит правее `start`.
    `prefix` — буква, которая обязана стоять перед окончанием (а или я).
    """
    for end in endings:
        tail = prefix + end
        if not word.endswith(tail):
            continue
        cut_at = len(word) - len(tail)
        if cut_at < start:
            continue
        return word[: cut_at + len(prefix)], True
    return word, False


def stem(word: str) -> str:
    """
    Основа слова по Snowball. Латиница и цифры возвращаются как есть:
    «битрикс24», «cpl», «roi» — не русские слова, морфологии у них нет.
    """
    w = (word or "").lower().replace("ё", "е")
    if not w or not any(ch in VOWELS for ch in w):
        return w
    if not re.search(r"[а-я]", w):
        return w

    rv, _r1, r2 = _regions(w)

    # Шаг 1
    base, done = _cut(w, _PERFECTIVE_1, rv, prefix="а")
    if not done:
        base, done = _cut(w, _PERFECTIVE_1, rv, prefix="я")
    if not done:
        base, done = _cut(w, _PERFECTIVE_2, rv)

    if not done:
        base, refl = _cut(w, _REFLEXIVE, rv)

        # Причастие = причастное окончание перед прилагательным.
        after, cut_adj = _cut(base, _ADJECTIVE, rv)
        if cut_adj:
            for prefix in ("а", "я"):
                after2, cut_p = _cut(after, _PARTICIPLE_1, rv, prefix=prefix)
                if cut_p:
                    after = after2
                    break
            else:
                after, _ = _cut(after, _PARTICIPLE_2, rv)
            base, done = after, True
        else:
            for prefix in ("а", "я"):
                after2, cut_p = _cut(base, _PARTICIPLE_1, rv, prefix=prefix)
                if cut_p:
                    base, done = after2, True
                    break
            if not done:
                base, done = _cut(base, _PARTICIPLE_2, rv)

        if not done:
            for prefix in ("а", "я"):
                after2, cut_v = _cut(base, _VERB_1, rv, prefix=prefix)
                if cut_v:
                    base, done = after2, True
                    break
        if not done:
            base, done = _cut(base, _VERB_2, rv)
        if not done:
            base, done = _cut(base, _NOUN, rv)
        if not done and refl:
            done = True

    # Шаг 2
    if base.endswith("и") and len(base) - 1 >= rv:
        base = base[:-1]

    # Шаг 3
    base, _ = _cut(base, _DERIVATIONAL, r2)

    # Шаг 4
    if base.endswith("нн"):
        base = base[:-1]
    else:
        base, cut_sup = _cut(base, _SUPERLATIVE, rv)
        if cut_sup and base.endswith("нн"):
            base = base[:-1]
    if base.endswith("ь"):
        base = base[:-1]

    return base or w


def words(phrase: str) -> list[str]:
    return WORD_RE.findall((phrase or "").lower().replace("ё", "е"))


def stem_key(phrase: str) -> str:
    """
    Ключ схлопывания: основы слов, отсортированные и склеенные. Порядок слов
    в ключ не входит намеренно — Wordstat порядком не различает, и «система
    сквозной аналитики» с «аналитика система сквозной» дают равные 133.
    """
    return " ".join(sorted(stem(w) for w in words(phrase) if w))


def same_query(a: str, b: str) -> bool:
    """Одна ли это фраза с точки зрения Wordstat."""
    return bool(stem_key(a)) and stem_key(a) == stem_key(b)


# ── выбор представителя группы ───────────────────────────────────────────────

#: Окончания, которые бывают только у косвенных форм. «ой», «ей», «и», «ы»
#: сюда не входят: они бывают и именительным, а ошибочный штраф хуже
#: пропущенного.
_OBLIQUE = (
    "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями", "ах", "ях",
    "ую", "юю", "ою", "ею", "ов", "ев",
)
#: Похоже на прилагательное. Фраза, кончающаяся прилагательным, как название
#: запроса читается плохо: «аналитика система сквозной».
_ADJ_TAIL = (
    "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его",
    "ому", "ему", "ым", "им", "ыми", "ими", "ых", "их", "ую", "юю", "ою", "ею",
)


def _oblique_score(phrase: str) -> int:
    ws = words(phrase)
    score = sum(1 for w in ws if w.endswith(_OBLIQUE))
    if ws and len(ws) > 1 and ws[-1].endswith(_ADJ_TAIL):
        score += 1          # хвост-прилагательное: перевёрнутый порядок слов
    return score


def pick_canonical(variants: list[str], prefer: set[str] | None = None) -> str:
    """
    Представитель группы словоформ. Сначала то, что назвал оператор (`prefer` —
    обычно seed-фразы проекта), затем форма с наименьшим числом косвенных
    признаков, затем алфавит — чтобы выбор был воспроизводим.
    """
    if not variants:
        return ""
    prefer = prefer or set()
    return sorted(
        variants,
        key=lambda p: (0 if p in prefer else 1, _oblique_score(p), len(p), p),
    )[0]


# ── приведение к именительному ───────────────────────────────────────────────

#: Прилагательное в косвенном падеже. Проверяется по порядку: длинные раньше.
_ADJ_OBLIQUE = (
    "ого", "его", "ому", "ему", "ыми", "ими", "ых", "их", "ой", "ей",
    "ым", "им", "ую", "юю", "ою", "ею", "ом", "ем",
)
#: Именительные окончания прилагательного по роду головы.
_ADJ_NOM = {
    "f": ("ая", "яя"),
    "m": ("ый", "ий"),
    "n": ("ое", "ее"),
    "p": ("ые", "ие"),
}
#: Мягкая основа: после этих букв ставится мягкий вариант окончания.
_SOFT = frozenset("нщчшжгкх")


def _looks_adjective(word: str) -> bool:
    return len(word) > 4 and word.endswith(_ADJ_TAIL)


def _noun_to_nominative(word: str) -> tuple[str, str | None]:
    """
    Существительное из родительного/винительного в именительный.
    Возвращает (слово, род) — род нужен прилагательному. None = не тронули.

    Работает только по частым и однозначным случаям. Всё, что неоднозначно
    («застройщика» — родительный мужского), остаётся как есть: кривой падеж
    в отчёте лучше выдуманного слова.
    """
    w = word
    if len(w) < 4:
        return w, None

    # «стратегии» -> «стратегия», «серии» -> «серия»
    if w.endswith("ии"):
        return w[:-1] + "я", "f"
    # «стратегию» -> «стратегия», «аудиторию» -> «аудитория».
    # Форма на «-ью» сюда не входит: «статью» -> «статья», но «моделью» ->
    # «модель», и по окончанию эти два случая не различить.
    if w.endswith("ию"):
        return w[:-2] + "ия", "f"
    # «аналитики» -> «аналитика», «методики» -> «методика»: «и» после
    # заднеязычной и шипящей — родительный женского на «-а».
    if w.endswith("и") and len(w) > 2 and w[-2] in "кгхжшчщ":
        return w[:-1] + "а", "f"
    # «системы» -> «система»
    if w.endswith("ы") and len(w) > 2 and w[-2] not in VOWELS:
        return w[:-1] + "а", "f"
    # «аналитику» -> «аналитика» (винительный женского)
    if w.endswith("у") and len(w) > 3 and w[-2] not in VOWELS:
        return w[:-1] + "а", "f"
    # «стратегию» -> «стратегия»
    if w.endswith("ю") and len(w) > 3 and w[-2] not in VOWELS:
        return w[:-1] + "я", "f"
    return w, None


def _adjective_to_nominative(word: str, gender: str) -> str:
    for end in _ADJ_OBLIQUE:
        if word.endswith(end) and len(word) - len(end) >= 3:
            base = word[: -len(end)]
            soft = bool(base) and base[-1] in _SOFT and gender in ("m", "p")
            nom = _ADJ_NOM[gender][1 if soft else 0]
            return base + nom
    return word


def to_nominative(phrase: str) -> tuple[str, bool]:
    """
    Приводит первую именную группу к именительному падежу.

    Правится только голова фразы: прилагательные слева и первое
    существительное. Всё правее головы — зависимое в родительном, оно
    управляется головой и остаётся: «маркетинговой аналитики застройщика»
    -> «маркетинговая аналитика застройщика», а не «... застройщик».

    Возвращает (фраза, поправлено ли). Не уверены — возвращаем как было.
    """
    ws = words(phrase)
    if not ws:
        return phrase, False

    head = next((i for i, w in enumerate(ws) if not _looks_adjective(w)), None)
    if head is None:
        return phrase, False

    noun, gender = _noun_to_nominative(ws[head])
    if gender is None:
        return phrase, False

    out = list(ws)
    out[head] = noun
    for i in range(head):
        out[i] = _adjective_to_nominative(ws[i], gender)

    result = " ".join(out)
    return result, result != " ".join(ws)


# ── самопроверка ─────────────────────────────────────────────────────────────

#: Пары из живого ядра. Частотности у них совпадали, потому что Wordstat
#: считает их одним запросом — на них и проверяем схлопывание.
SELFTEST_SAME = [
    ("сквозная аналитика", "сквозные аналитики"),
    ("система сквозной аналитики", "аналитика система сквозной"),
    ("сквозная аналитика битрикс24", "сквозную аналитику битрикс24"),
    ("сервисы сквозной аналитики", "сквозная аналитика сервисы"),
    ("метрика сквозную аналитику", "сквозная аналитика метрики"),
    ("настройка сквозной аналитики", "сквозная аналитика настройка"),
    ("брендинг жилого комплекса", "брендинги жилых комплексов"),
    ("маркетинг застройщика", "маркетинг застройщиков"),
]
#: Пары, которые схлопывать НЕЛЬЗЯ: разный состав слов и разная частотность.
SELFTEST_DIFFERENT = [
    ("сквозная аналитика битрикс", "сквозная аналитика битрикс24"),
    ("сквозная аналитика битрикс 24", "сквозная аналитика битрикс24"),
    ("сквозная аналитика", "сквозная аналитика метрики"),
    ("брендинг жк", "брендинг жк москва"),
]
SELFTEST_NOMINATIVE = [
    ("маркетинговой аналитики застройщика", "маркетинговая аналитика застройщика"),
    ("сегментации аудитории застройщика", "сегментация аудитории застройщика"),
    ("сквозной аналитики застройщика", "сквозная аналитика застройщика"),
    ("маркетинговую стратегию застройщика", "маркетинговая стратегия застройщика"),
    # Уже именительный — не трогаем.
    ("брендинг жилого комплекса", "брендинг жилого комплекса"),
    ("маркетинг застройщика", "маркетинг застройщика"),
]


def selftest() -> int:
    bad = 0
    for a, b in SELFTEST_SAME:
        if not same_query(a, b):
            bad += 1
            print(f"НЕ схлопнулось: {a!r} [{stem_key(a)}] != {b!r} [{stem_key(b)}]")
    for a, b in SELFTEST_DIFFERENT:
        if same_query(a, b):
            bad += 1
            print(f"Схлопнулось зря: {a!r} == {b!r} [{stem_key(a)}]")
    for src, want in SELFTEST_NOMINATIVE:
        got, _ = to_nominative(src)
        if got != want:
            bad += 1
            print(f"Падеж: {src!r} -> {got!r}, ожидалось {want!r}")

    total = len(SELFTEST_SAME) + len(SELFTEST_DIFFERENT) + len(SELFTEST_NOMINATIVE)
    print(f"\nПроверок {total}, провалено {bad}.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Русская морфология для ядра")
    p.add_argument("--stem", help="показать основы и ключ схлопывания фразы")
    p.add_argument("--nominative", help="привести фразу к именительному")
    p.add_argument("--same", nargs=2, metavar=("A", "B"), help="одна ли это фраза")
    p.add_argument("--selftest", action="store_true", help="прогон на боевых примерах")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.stem:
        for w in words(args.stem):
            print(f"  {w:<28} -> {stem(w)}")
        print(f"\nключ: {stem_key(args.stem)}")
        return 0
    if args.nominative:
        out, changed = to_nominative(args.nominative)
        print(out)
        print(f"({'поправлено' if changed else 'без изменений'})", file=sys.stderr)
        return 0
    if args.same:
        a, b = args.same
        print("одна фраза" if same_query(a, b) else "разные фразы")
        print(f"  {a!r}: {stem_key(a)}")
        print(f"  {b!r}: {stem_key(b)}")
        return 0

    p.error("нужен --stem, --nominative, --same или --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
