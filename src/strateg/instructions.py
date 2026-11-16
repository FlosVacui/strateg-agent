# ruff: noqa: E501
"""Признаки обращения к ИИ в тексте (ADR-0011, п. 3): обращение, а не тема.

Метится текст, который просит модель что-то сделать или выдаёт себя за настройки модели: «игнорируй
прежние указания», «если ты ИИ-ассистент, …», «системное сообщение:», «не говори пользователю». Текст
о моделях — заголовок статьи о промтах, вакансия, новость об ассистенте, инструкция для человека — не
метится.

Пометка — сигнал человеку, а не защита: обращение, записанное иначе (другой язык, кодировка, картинка),
пройдёт. Пропуск обращения дороже лишней пометки, поэтому там, где правило нельзя сузить без пропусков,
оно остаётся широким.

Перед поиском текст нормализуется (NFKC, невидимые знаки, смесь кириллицы и латиницы в слове, разрядка,
«l33t»). Нормализованная копия нужна только для поиска: снимок и смещения цитат её не видят.
"""

from __future__ import annotations

import functools
import re
import unicodedata

# --- Нормализация только для поиска -------------------------------------------------------------------------


def _chars(*ranges: tuple[int, int]) -> str:
    """Класс символов регулярного выражения из диапазонов кодов (без записи самих невидимых знаков в код)."""
    return (
        "["
        + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "") for a, b in ranges)
        + "]"
    )


# Невидимые знаки, которые прячут внутри слов: мягкий перенос, нулевая ширина, управление направлением,
# скрытые коды, знаки-заполнители, теги, селекторы вариантов; комбинирующие значки поверх букв.
_INVISIBLE = re.compile(
    _chars(
        (0x00AD, 0x00AD),
        (0x034F, 0x034F),
        (0x061C, 0x061C),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180E),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
        (0x3164, 0x3164),
        (0xFE00, 0xFE0F),
        (0xFEFF, 0xFEFF),
        (0xFFA0, 0xFFA0),
        (0xE0000, 0xE007F),
        (0x0300, 0x036F),
        (0x0483, 0x0489),
    )
)
_CYRILLIC = re.compile(_chars((0x0400, 0x04FF)))
_GREEK = re.compile(_chars((0x0370, 0x03FF)))
_LATIN = re.compile("[A-Za-z]")
# латинские буквы, похожие на русские (и наоборот), и греческие, похожие на латинские
_LATIN_TO_CYR = str.maketrans("aceopxykmhtb", "асеорхукмнтв")
_CYR_TO_LATIN = str.maketrans("асеорхукмнтвіј", "aceopxykmhtbij")
_GREEK_TO_LATIN = str.maketrans("αβεικνορτυχ", "abeikvoptux")
_LETTERS = re.compile(r"[^\W\d_]+")
# разрядка: три и больше одиночных буквы через пробел, точку, дефис, подчёркивание, звёздочку, среднюю точку
_SPACED = re.compile(r"(?<![^\W\d_])(?:[^\W\d_][ \xa0.\-_*\xb7]){2,}[^\W\d_](?![^\W\d_])")
_SPACED_SEP = re.compile(r"[ \xa0.\-_*\xb7]")
_HSPACE = re.compile(r"[^\S\n]+")
# слово с цифрами или знаками вместо букв: «1gn0re», «pr3vious»; ограничена длина просмотра вперёд
_LEET_WORD = re.compile(
    r"(?<![A-Za-z0-9@$])(?=[A-Za-z0-9@$]{0,19}[013457@$])(?=[A-Za-z0-9@$]{0,19}[A-Za-z])[A-Za-z0-9@$]{3,20}"
    r"(?![A-Za-z0-9@$])"
)
# «l33t» читается только для слов, которые есть в шаблонах: прочие слова с цифрами («mp4», «b2b») не трогаем
_LEET_KNOWN = frozenset(
    "ignore disregard forget override previous prior above earlier instructions instruction prompt prompts "
    "rules "
    "system assistant reveal user human operator secret hidden now are you".split()
)
_LEET = str.maketrans("013457@$", "oieastas")
_EMPHASIS = str.maketrans("", "", "*~")
_MAX_RUNS = 50


def _is_mixed(word: str) -> bool:
    return (
        not word.isascii()
        and bool(_LATIN.search(word))
        and bool(_CYRILLIC.search(word) or _GREEK.search(word))
    )


def _to_cyrillic(m: re.Match[str]) -> str:
    word = m.group()
    return word.translate(_LATIN_TO_CYR) if _is_mixed(word) else word


def _to_latin(m: re.Match[str]) -> str:
    word = m.group()
    return word.translate(_CYR_TO_LATIN).translate(_GREEK_TO_LATIN) if _is_mixed(word) else word


def _leet(m: re.Match[str]) -> str:
    word = m.group()
    mapped = word.translate(_LEET)
    return mapped if mapped in _LEET_KNOWN else word


# в слове из букв двух алфавитов всегда есть соседние буквы разных алфавитов: поиск такой пары линеен и быстр
_MIX_ADJACENT = re.compile(
    "[A-Za-z][" + chr(0x370) + "-" + chr(0x4FF) + "]|[" + chr(0x370) + "-" + chr(0x4FF) + "][A-Za-z]"
)


def _has_mixed_words(text: str) -> bool:
    return bool(_MIX_ADJACENT.search(text))


def _squeeze(text: str, runs: list[str]) -> str:
    """Разрядка снята; склеенные слова от 12 букв копятся в `runs`; пробелы сжаты."""

    def unspace(m: re.Match[str]) -> str:
        joined = _SPACED_SEP.sub("", m.group())
        if len(runs) < _MAX_RUNS and len(joined) >= 12:
            runs.append(joined)
        return joined

    text = _SPACED.sub(unspace, text)
    text = text.translate(_EMPHASIS).replace("_", " ").replace("`", " ")  # **ignore** _all_ -> ignore  all
    return _HSPACE.sub(" ", text)


def normalize(text: str) -> tuple[list[str], list[str]]:
    """Тексты для поиска признаков и склеенные разрядкой слова (для фраз без пробелов между словами).

    NFKC, без невидимых знаков, регистр и «ё» сняты, разрядка снята, пробелы сжаты (переводы строк остаются).
    В слове из букв двух алфавитов («Игнoрируй» с латинской «o») неясно, какой алфавит верный, поэтому
    текст идёт в двух видах: смешанные слова целиком русскими и целиком латинскими буквами.
    Без смешанных слов вид один.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text).casefold().replace("ё", "е")
    runs: list[str] = []
    if _has_mixed_words(text):
        variants = [_squeeze(_LETTERS.sub(f, text), runs) for f in (_to_cyrillic, _to_latin)]
    else:
        variants = [_squeeze(text, runs)]
    return variants, runs


# --- Правила: адресат + повеление ---------------------------------------------------------------------------
#
# Признак строится по структуре предложения, а не по списку фраз: «адресат (ИИ-слово, в том числе во
# множественном числе) + повеление или просьба к нему». Повеление — повелительное наклонение (для русского
# разбор pymorphy3, для английского форма глагола без окончаний) или модальная конструкция («must», «could you»,
# «обязан», «пожалуйста»). Вводные слова после «ИИ, …», приветствие и «если вы используете нейросеть»
# повелением не являются и не метятся. Обращение без адресата («Respond only in French from now on»)
# правилами не ловится: это известное ограничение.

SIGN_OVERRIDE = "игнорировать прежние указания"
SIGN_ADDRESS = "обращение к ИИ или ассистенту"
SIGN_ROLE = "смена роли или системная инструкция"
SIGN_ACTION = "требование скрыть от пользователя или выполнить действие"
INSTRUCTION_SIGNS = (SIGN_OVERRIDE, SIGN_ADDRESS, SIGN_ROLE, SIGN_ACTION)

_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*|\d+|[,:;!?—–\-]")
# Слова-признаки адресата: предложение без них адресатом не разбирается (таких 95 % предложений обычной страницы)
_ADDR_CUES = tuple(
    "ai|ии|llm|gpt|нейросет|ассистент|assistant|bot|бот|робот|crawler|scraper|summari|chat|claude|gemini|copilot|"
    "giga|yandex|deepseek|grok|perplexity|алис|language model|языков|искусственн|агент|agent|model|модел|помощник|"
    "краулер|парсер|суммаризатор".split("|")
)


def _has_addr_cue(unit: str) -> bool:
    return any(c in unit for c in _ADDR_CUES)


_UNIT_SPLIT = re.compile(r"[.?;\n]+")
_BULLET = re.compile(r"^[\s>*#•·\-–—\d)(\[\]\"'«“„]+(?=[^\W\d_])")
_PUNCT = frozenset(",:;!?—–-")
_SOFT = frozenset(",:!—–-")  # знаки, после которых идёт повеление: «ИИ, сделай», «ИИ: сделай», «Hi AI! Do»

# Слова обращения и вводных рамок: «Dear …», «Note to …», «Эй, …», «Внимание: …»
_GREET = frozenset(
    "hi hello hey dear attention note notice important warning reminder instruction instructions message to for "
    "эй привет здравствуй здравствуйте уважаемый уважаемая уважаемые внимание важно обращение памятка "
    "инструкция сообщение примечание заметка для к ps".split()
)
_DET = frozenset("any all every each the любой любые все всем".split())
_ADDR_HEAD_EN = frozenset(
    "ai ais llm llms gpt assistant assistants chatbot chatbots chat-bot bot bots crawler crawlers scraper scrapers "
    "summarizer summarizers summariser summarisers".split()
)
_ADDR_BRANDS = frozenset(
    "chatgpt claude gemini copilot gigachat yandexgpt deepseek grok perplexity алиса алисе ии".split()
)
_ADDR_PREFIX = frozenset("ai llm ии automated machine language large web chat gpt".split())
_ADDR_NOUN = frozenset("agent agents model models system systems assistant assistants bot bots".split())
_ADDR_RU = re.compile(
    r"^(?:нейросет\w*|ассистент\w*|чат-?бот\w*|бот(?:ы|а|у|ом|е|ам|ов)?|робот\w*|краулер\w*|парсер\w*|"
    r"суммаризатор\w*)$"
)
# Читатели-машины: заголовок «Attention bots and scrapers:» адресует их и без повеления
_MACHINE_READERS = frozenset(
    "bot bots crawler crawlers scraper scrapers summarizer summarizers summariser summarisers llm llms".split()
)
_READING = frozenset(
    "reading processing summarizing summarising scraping crawling parsing analyzing analysing "
    "читающий читающая читающие читающую обрабатывающий обрабатывающая обрабатывающие анализирующий "
    "анализирующая анализирующие парсящий пересказывающий".split()
)

_HELPER_MODIFIERS = ("автоматическ", "виртуальн", "цифров", "умн", "голосов")
# повеление, с которым достаточно слова «agent» или «model» в начале предложения: «Agent, ignore …»
_STRONG = frozenset("ignore disregard forget override reveal obey comply stop abandon drop skip".split())
_MODAL_EN = frozenset("must should shall".split())
_MODAL_RU = re.compile(r"^(?:обязан\w*|должн\w*|следует|необходимо|нужно|надо|пожалуйста|будь|будьте)$")
_FILLERS = frozenset(
    "please kindly just now also then and so only instead actually first simply immediately from on again "
    "пожалуйста лучше теперь тогда и а потом просто ты вы".split()
)
# Не повеление: слова, с которых начинаются вводные, пояснения и описания («AI, such as GPT-4, is …»)
_NOT_DIRECTIVE = frozenset(
    """a an the this that these those my our your their its his her he she it we they i you who which what when
    where why how whose whom whether of in on at by for from with without about as like such than between among
    through during before after over under into onto upon per via according because since although though while
    whereas if unless until and or but nor yet so however therefore thus hence also too still already ever
    even just only both either neither some any all each every no not none many much more most less least few
    several various other another one two three first second third last next once twice here there then now today
    tomorrow yesterday often usually sometimes always never maybe perhaps indeed otherwise instead meanwhile
    moreover furthermore namely together unlike despite beyond against along around across within toward towards
    is are was were be been being am has have had do does did can could may might will would shall
    welcome thank thanks enjoy join meet subscribe read see check learn stay discover explore find look watch
    try visit follow click sign register download share like comment review rate remember note consider
    imagine hello hi hey dear greetings
    i.e. e.g. etc vs
    это этот эта эти тот те то как что чтобы для от с со на в во по к о об из за при без над под про между
    через после перед до или но а и да нет не ни же ли бы только даже уже ещё еще тоже также однако впрочем
    например согласно кстати конечно безусловно скорее вероятно возможно очевидно обычно часто иногда
    сегодня завтра вчера здесь там тогда теперь который которая которое которые которого которой которым
    пока вместо кроме""".split()
)
# Глаголы-обращения к человеку: «Dear AI enthusiasts, welcome …» — не повеление модели
_CLAUSE_START = frozenset("and then please just now also kindly first instead but so or and, to".split()) - {
    "to"
}
_OUTPUT_TARGET = re.compile(
    r"\b(?:the user|your (?:answer|reply|response|summary|output|message|report|reports)|every "
    r"(?:answer|response|reply|summary)|each (?:answer|response|reply|summary)|human)\b"
    r"|пользовател\w+|человек\w*|в (?:каждом )?(?:ответе|отчёте|отчете|выводе|резюме|пересказе)|выдать|выдай"
)


def _addr_len(tokens: list[str], i: int) -> int:
    """Сколько слов занимает адресат («ai assistants», «языковая модель», «claude») с позиции i, или 0."""
    n = len(tokens)
    start = i
    if i < n and tokens[i] in _DET:
        i += 1
    if i >= n:
        return 0
    t = tokens[i]
    if t.startswith(("ии-", "ai-", "llm-", "gpt-")):
        return i + 1 - start
    if t in _ADDR_PREFIX and i + 1 < n and (tokens[i + 1] in _ADDR_NOUN or tokens[i + 1] in _ADDR_HEAD_EN):
        return i + 2 - start
    if t.startswith("языков") and i + 1 < n and tokens[i + 1].startswith("модел"):
        return i + 2 - start
    if t.startswith("искусственн") and i + 1 < n and tokens[i + 1].startswith("интеллект"):
        return i + 2 - start
    if t in _ADDR_HEAD_EN or t in _ADDR_BRANDS or _ADDR_RU.match(t):
        return i + 1 - start
    if t in _ADDR_NOUN and i + 1 < n and tokens[i + 1] in _READING:  # «models reading this page, …»
        return i + 1 - start
    if t.startswith(_HELPER_MODIFIERS) and i + 1 < n and tokens[i + 1].startswith("помощник"):
        return i + 2 - start
    return 0


_IMPERATIVE_ENDING = re.compile(r"[йьи](?:те)?(?:сь|ся)?$|[йьи]тесь$")


@functools.lru_cache(maxsize=4096)
def _ru_imperative(word: str) -> bool:
    """Повелительное наклонение русского глагола по словарю pymorphy3 (загружается при первом обращении)."""
    if not _IMPERATIVE_ENDING.search(word):  # словарь не нужен, если окончание не повелительное
        return False
    from strateg import safety

    parses = safety._analyzer().parse(word)[:2]
    return any(p.tag.mood == "impr" and p.score >= 0.5 for p in parses)


def _cyrillic(word: str) -> bool:
    return bool(_CYRILLIC.search(word))


# за глаголом повеления идёт дополнение, предлог или конец предложения; «manager of …» и имена так не идут
_AFTER_VERB = frozenset(
    """the a an your this that these those all any every each my our their its his her to me us him them it you
    only everything anything what whatever how why instead now and with in on at from for about as into onto
    over under up out off down against than until while when before after if because""".split()
)


def _en_base_verb(tokens: list[str], i: int) -> bool:
    """Слово похоже на глагол в начальной форме: не служебное и без окончаний -ed, -ing, -ly, -s."""
    w = tokens[i]
    if not (w.isascii() and w.isalpha()) or len(w) < 3 or w in _NOT_DIRECTIVE:
        return False
    if w.endswith(("ed", "ing", "ly", "ous", "ful", "less", "ness", "ity")):
        return False
    if len(w) >= 8 and w.endswith("tion") or len(w) >= 10 and w.endswith("ment"):
        return False  # «automation», «management»; но «mention», «implement» — глаголы
    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
    if nxt and nxt not in _AFTER_VERB and nxt not in _PUNCT:
        return False
    if w.endswith("s") and not w.endswith(("ss", "us")):
        return False
    return not (i + 1 < len(tokens) and tokens[i + 1] == "by")  # «run by a startup» — причастие


def _directive_at(tokens: list[str], j: int) -> bool:
    """С позиции j начинается повеление или просьба: «tell the user», «could you …», «поставь …», «обязан …»."""
    n = len(tokens)
    steps = 0
    while j < n and (tokens[j] in _FILLERS or tokens[j] in _PUNCT) and steps < 5:
        j += 1
        steps += 1
    if j >= n:
        return False
    w = tokens[j]
    nxt = tokens[j + 1] if j + 1 < n else ""
    if w in ("could", "would", "can", "will") and nxt == "you":
        return True
    if w == "you" and (nxt in _MODAL_EN or nxt in ("need", "have", "are", "will", "may")):
        return True
    if w in _MODAL_EN or w in ("kindly", "please"):
        return True
    if w in ("do", "don't", "never", "always") or (w == "do" and nxt in ("not", "n't")):
        return True
    if w in ("need", "needs") and nxt == "to":
        return True
    if _cyrillic(w):
        if _MODAL_RU.match(w):
            return True
        if w == "не" and j + 1 < n and _cyrillic(tokens[j + 1]) and _ru_imperative(tokens[j + 1]):
            return True
        # русский повелительный ищется в окне: «в выводе укажи, что …»
        for k in range(j, min(n, j + 8)):
            if tokens[k] in _PUNCT:
                break
            if _cyrillic(tokens[k]) and _ru_imperative(tokens[k]):
                return True
        return False
    return not _addr_len(tokens, j) and _en_base_verb(tokens, j)


def _participle(word: str) -> bool:
    return (
        word in _READING
        or word in ("when", "while", "if", "whenever", "когда", "если", "при")
        or bool(re.match(r"^[а-я]+(?:ющ|вш|ящ|ащ|нн|енн)\w*$", word))
        or word.endswith("ing")
    )


def _vocative(tokens: list[str]) -> bool:
    """«ИИ, сделай …», «Hi AI! Could you …», «Эй, ChatGPT, поставь …», «Нейросеть, прочитавшая это: сообщи …»."""
    n = len(tokens)
    i = 0
    skipped = 0
    while i < n and (tokens[i] in _GREET or tokens[i] in _PUNCT) and skipped < 6:
        i += 1
        skipped += 1
    if i + 2 < n and tokens[i] in ("agent", "agents", "model", "models") and tokens[i + 1] in _SOFT:
        return tokens[i + 2] in _STRONG
    k = _addr_len(tokens, i)
    if not k:
        return False
    if i > 0 and tokens[i - 1] in ("«", "“"):
        return False
    after = i + k
    # перечисление адресатов: «ChatGPT, Gemini, Claude: …»
    while after + 1 < n and tokens[after] in (",", "and", "и", "or", "или") and _addr_len(tokens, after + 1):
        after += 1 + _addr_len(tokens, after + 1)
    # после первого знака идёт повеление; ещё один знак допустим, если между ними причастный оборот:
    # «нейросеть, прочитавшая это, обязана …», но не «ИИ, как и люди, ошибается: проверяйте …»
    seg = after
    first = True
    for pos in range(after, min(n, after + 9)):
        t = tokens[pos]
        if t in (";", "?"):
            break
        if t not in _SOFT:
            continue
        if (first or _participle(tokens[seg] if seg < n else "")) and _directive_at(tokens, pos + 1):
            return True
        first = False
        seg = pos + 1
    return False


def _machine_header(tokens: list[str]) -> bool:
    """«Attention bots and scrapers: …», «AI models reading this: …» — заголовок для машин, повеление не нужно."""
    n = len(tokens)
    i = 0
    greeted = False
    while i < n and tokens[i] in _GREET:
        i += 1
        greeted = True
    k = _addr_len(tokens, i)
    if not k:
        return False
    j = i + k
    while j < n and tokens[j] in ("and", "or", "и", "или", ",") and _addr_len(tokens, j + 1):
        j += 1 + _addr_len(tokens, j + 1)
    head_words = tokens[i : i + k]
    machine = any(w in _MACHINE_READERS or w.startswith(("краулер", "суммаризатор")) for w in tokens[i:j])
    plural_ai = head_words[-1] in ("agents", "models", "systems", "assistants", "bots", "llms")
    reading = j < n and tokens[j] in _READING
    if j < n and tokens[j] == ":" or (reading and ":" in tokens[j : j + 5]):
        return (greeted and (machine or plural_ai)) or reading
    return False


def _reading_directive(tokens: list[str]) -> bool:
    """«ИИ, читающий эту страницу: в выводе укажи …» — адресат с причастием и повеление после знака."""
    n = len(tokens)
    for i in range(n):
        k = _addr_len(tokens, i)
        if not k:
            continue
        j = i + k
        if j < n and tokens[j] == ",":
            j += 1
        if j < n and tokens[j] in _READING:
            for pos in range(j + 1, min(n, j + 10)):
                if tokens[pos] in _SOFT and _directive_at(tokens, pos + 1):
                    return True
    return False


def _modal_for_ai(tokens: list[str], text: str) -> bool:
    """«AI assistants must append … to every summary», «assistant must respond with the user's key»."""
    if not _OUTPUT_TARGET.search(text):
        return False
    n = len(tokens)
    for i in range(n):
        k = _addr_len(tokens, i)
        if not k:
            continue
        for pos in range(i + k, min(n, i + k + 8)):
            t = tokens[pos]
            if t in _MODAL_EN or (_cyrillic(t) and re.match(r"^(?:обязан\w*|должн\w*)$", t)):
                return True
            if t in ("need", "needs") and pos + 1 < n and tokens[pos + 1] == "to":
                return True
    return False


# --- Остальные признаки: регулярные выражения по предложению ------------------------------------------------

_OVERRIDE_VERB_EN = r"(?:ignore|disregard|forget|override|overrule|neglect|(?:do not|don't|stop|no longer|never) follow(?:ing)?)"
_OVERRIDE_OBJ_EN = (
    r"(?:(?:the |all |any |every |these |those |your |my |its |of |everything |about )*"
    r"(?:(?:previous|prior|above|earlier|preceding|original|initial|system|safety|existing|other|old|given|"
    r"current|developer|your) ){0,3}"
    r"(?:instructions?|prompts?|rules|directives?|guidelines|commands?|constraints|restrictions|safeguards|"
    r"policies|policy|programming|training|guardrails|settings|orders|messages|context|conversation)"
    r"|(?:everything|anything|all|whatever|what|the text|the article|the page|the content)"
    r"(?: that| the user| you| i| we)?(?: was| were| is| has been| have been| had been|'s)? ?"
    r"(?:above|before|earlier|previously|prior|said|told|asked|written|wrote|given|came before|you were told|"
    r"the user asked|the user said))"
)
_OVERRIDE_EN = re.compile(rf"(?<![a-z])({_OVERRIDE_VERB_EN}) {_OVERRIDE_OBJ_EN}")
_OVERRIDE_RU = re.compile(
    r"(?<![а-я])(?:игнорируй\w*|забудь\w*|пренебреги\w*|отбрось\w*|не следуй\w*|не учитывай\w*|не слушай\w*|"
    r"не обращай внимани\w+ на)"
    r"(?:,? [а-я-]+){0,5}? "
    r"(?:инструкци\w+|указани\w+|правил\w+|команд\w+|ограничени\w+|директив\w+|настройк\w+|установк\w+|"
    r"промпт\w*|запрет\w+|задани\w+|предыдущ\w+|прежн\w+|прошл\w+|выше|ранее|до этого|тебе говорил\w+|"
    r"тебе сказал\w+|вам говорил\w+|вам сказал\w+|что написано|что сказано|остальн\w+ текст\w*)"
)
# транслит: «ignoriruy vse predydushchie instruktsii»
_TRANSLIT = {
    "ignoriruy": "игнорируй",
    "ignoriruj": "игнорируй",
    "ignoriruyte": "игнорируйте",
    "ignoriruite": "игнорируйте",
    "zabud": "забудь",
    "zabudte": "забудьте",
    "vse": "все",
    "predydushchie": "предыдущие",
    "predydushie": "предыдущие",
    "predydushchiye": "предыдущие",
    "predyduschie": "предыдущие",
    "instruktsii": "инструкции",
    "instrukcii": "инструкции",
    "ukazaniya": "указания",
    "pravila": "правила",
}
# опечатки: слова из правил, в которых ошиблись на одну букву («ignor previous instrucitons»)
_TYPO_VOCAB = (
    "ignore disregard forget override previous instructions instruction prompt prompts system reveal rules "
    "guidelines directives earlier"
).split()
_TYPO_VERBS = frozenset("ignore disregard forget override".split())


def _deletions(word: str) -> set[str]:
    return {word[:i] + word[i + 1 :] for i in range(len(word))}


_TYPO_INDEX: dict[str, str] = {}
for _w in _TYPO_VOCAB:
    for _d in _deletions(_w) | {_w}:
        _TYPO_INDEX.setdefault(_d, _w)
_TOKEN_EN = re.compile(r"[a-z]{5,13}")


def _fix_typo(m: re.Match[str]) -> str:
    t = m.group()
    if t in _TYPO_INDEX and _TYPO_INDEX[t] == t:
        return t
    for cand in {t} | _deletions(t):
        word = _TYPO_INDEX.get(cand)
        if word and word != t and abs(len(word) - len(t)) <= 1 and t[0] == word[0]:
            if word in _TYPO_VERBS and t.endswith(("ed", "ing", "es", "s")):
                return t
            return word
    return t


def _canon(text: str) -> str:
    """Транслит и опечатки приведены к словам правил: только для поиска, как и остальная нормализация."""
    if "а" <= text[:1] <= "я" and text.isascii() is False and not _LATIN.search(text):
        return text
    text = _TOKEN_EN.sub(_fix_typo, text)
    if any(w in text for w in ("ignoriru", "zabud", "predyd", "instrukt", "instrukc")):
        text = re.sub(r"[a-z]+", lambda m: _TRANSLIT.get(m.group(), m.group()), text)
    return text


_TAGS = re.compile(
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>"
    r"|\[\s*/?\s*(?:system|inst|sys)\s*\]"
    r"|<<\s*/?\s*sys\s*>>"
    r"|<\|\s*(?:im[ _]start|im[ _]end|system|assistant|user|endoftext|eot[ _]id|start[ _]header[ _]id)\s*\|>"
    r"|^[ \t]{0,8}#{2,4}[ \t]{0,3}(?:system|instructions?)\b",
    re.MULTILINE,
)
_ROLE_LINE = re.compile(
    r"^[ \t]{0,8}(?:#{1,4}[ \t]{0,3})?(?:system|assistant|системное сообщение)[ \t]{0,3}:[ \t]{0,3}"
    r"(?=(?:you|ты|вы|new|новые|ignore|игнорируй|forget|забудь|from now|do not|don't|never|always|всегда|"
    r"никогда|answer|respond|отвечай|from this)\b)",
    re.MULTILINE,
)
_ROLE_HEADER = re.compile(
    r"(?<![a-z])(?:admin|administrator|system|developer|root|sudo|operator|maintenance)\s+"
    r"(?:override|command|mode|instruction|message|notice|directive)s?\s*:"
)
_ROLE_PHRASES = re.compile(
    r"(?<![a-z])(?:you are now (?:dan|free|unrestricted|unfiltered|jailbroken|in )|from now on,? you|you(?:'re| are) (?:now )?(?:in|entering) "
    r"(?:developer|debug|admin|god|maintenance|dan|jailbreak)|"
    r"(?:you (?:must|should|will|shall|can) )?(?:now )?(?:act|behave|respond|answer|play|function|operate|roleplay) as\b|"
    r"pretend (?:to be|you(?:'re| are)|that you)|act as if you|do anything now|"
    r"(?:enter|switch to|activate|enable|turn on) (?:the )?(?:maintenance|developer|debug|admin|god|sudo|root|dan|"
    r"unrestricted) mode|developer mode (?:enabled|on)|jailbreak(?:ed)? mode|"
    r"i am your (?:developer|creator|admin|administrator|owner|operator|programmer)|"
    r"(?:reveal|show|print|repeat|output|display|leak|disclose|dump|give me|tell me) (?:me )?(?:your|the|its) "
    r"(?:(?:full|complete|hidden|initial|original|secret|current) )?(?:system (?:prompt|instructions?|message)|"
    r"hidden (?:prompt|instructions?)|initial (?:prompt|instructions?)|configuration|config|settings|instructions|"
    r"prompt|rules))"
    r"|притворись|представь,? что (?:ты|вы)|веди себя как|играй роль|с этого момента (?:ты|вы|твоя|твой)|"
    r"(?:ты|вы) теперь (?:[а-я-]+ ){0,2}?(?:ии|ассистент|бот|помощник|хакер|администратор|агент|модель|режим\w*)|"
    r"(?:покажи|выведи|раскрой|повтори|напечатай|пришли|сообщи|назови|процитируй|выдай|отправь)\w* (?:мне )?"
    r"(?:(?:свои|свой|твой|твои|весь|полный|скрытый|исходный|начальный) ){1,3}(?:системн\w+ )?"
    r"(?:промпт|инструкци\w+|сообщени\w+)"
)
_ROLE_IDENTITY_EN = re.compile(
    r"(?<![a-z])you(?:'re| are)(?: now)?(?: (?:an?|the))?(?: [a-z-]+){0,2}? "
    r"(?:ai|assistant|chatbot|language model|llm|bot|dan|agent)"
    r"(?= *(?:[,:;!]|$| (?:that|who|which|and|with|without|named|called|designed|made)\b))"
)
_ROLE_IDENTITY_RU = re.compile(
    r"(?<![а-я])(?:ты|вы)(?: —| -|,)?(?: (?:теперь|сейчас))?(?: [а-я-]+(?:ый|ий|ой|ая|яя|ое|ее|ые|ие))? "
    r"(?:(?:ии|ai|llm|gpt)-(?:ассистент|агент|бот|модел|помощник|assistant|agent|bot|model)\w*|ассистент\w*|нейросеть|нейросетью|ии|бот|помощник|модель ии|языковая модель|модель)"
    r"(?= *(?:[,:;!—]|$| (?:который|которая|которое|и|с|без|обязан\w*|должен|должна)\b))"
)
_SYSTEM_COLON = re.compile(
    r"(?<![a-z])system (?:prompt|message|instructions?)\s*:\s*(?=(?:you|ignore|always|never|do|don't|from|the assistant)\b)"
    r"|системн\w+ (?:промпт|инструкци\w+|сообщени\w+)\s*:\s*(?=(?:ты|вы|всегда|никогда|игнорируй|не)\b)"
)
_NEW_RULES = re.compile(
    r"(?<![a-z])(?:new|updated|revised) (?:instructions?|rules|task|directives?)\s*:"
    r"|(?:новые|обновленные) (?:инструкции|указания|правила|задание)\s*:"
)
_HIDE = re.compile(
    r"(?<![a-z])(?:do not|don't|never|do n't|не)\s+(?:[a-zа-я]+\s+){0,2}?"
    r"(?:tell|inform|reveal|mention|show|disclose|say|let|expose|сообщай|говори|показывай|упоминай|"
    r"раскрывай|рассказывай|пиши)\b[^.!?\n]{0,60}?"
    r"\b(?:user|human|operator|person|reader|пользовател\w+|человек\w*|клиент\w*)"
    r"|скрой (?:это )?от пользовател\w+|без ведома пользовател\w+|втайне от пользовател\w+"
    r"|hide (?:this|it) from the (?:user|human)|keep this (?:secret|hidden) from the (?:user|human)"
    r"|without (?:the user knowing|telling the user|informing the user)"
)
_EXEC = re.compile(
    r"(?:выполни|запусти)\w* (?:следующ\w+ )?(?:команду|код|скрипт)|(?:execute|run) the following (?:command|code|script)"
)
_EXFIL = re.compile(
    r"(?:отправь|передай|перешли|загрузи|выгрузи|пришли)\w* (?:мне )?(?:все |всю |любые )?"
    r"(?:данные|файлы|материалы|ключи|пароли|токены|переписк\w+|содержимое|контекст\w*|информаци\w+|историю) "
    r"(?:проекта |пользователя |диалога )?(?:на|по)\b"
    r"|(?:send|forward|upload|post|leak|exfiltrate|email|transmit)\b[^.!?\n]{0,30}?"
    r"(?:conversation|chat|data|files|keys|passwords|secrets|tokens|context|history|project|contents?|information)"
    r"[^.!?\n]{0,30}?\b(?:to|via)\b"
    r"|(?:include|insert|append|add|put|embed|encode) (?:[a-z]+ ){0,4}(?:project|user|private|confidential|internal|"
    r"conversation) (?:data|details|information|files|context|name) (?:in|into|to|at the end of) (?:[a-z]+ ){0,3}"
    r"(?:quer(?:y|ies)|url|request|link)"
    r"|(?:добавь|вставь|подставь|включи|включай)\w* (?:[а-я]+ ){0,4}(?:данн\w+|название|имя|материал\w+|содержимое|"
    r"контекст) (?:проекта|пользователя|диалога) (?:в|к) (?:[а-я]+ ){0,3}(?:запрос\w*|url|адрес\w*|ссылк\w*)"
)
_INJECT = re.compile(
    r"(?:вставь|добавь|включи|напиши|выведи|припиши|повтори)\w* (?:[а-я]+ ){0,3}(?:в|на) (?:[а-я]+ ){0,2}"
    r"(?:отчет\w*|ответ\w*|вывод\w*|резюме|выжимк\w*) (?:фраз\w+|слова|строк\w+|текст|сообщени\w+|ссылк\w+)"
    r"|(?:insert|add|include|output|write|append|say|repeat)\s(?:the )?(?:following )?"
    r"(?:phrase|words?|string|sentence|text|line)\b[^.\n]{0,60}\b(?:in|into|to|at the (?:start|end) of) "
    r"(?:your|the) (?:report|response|answer|reply|summary|output|message)"
    r"|(?:begin|start|open|end|finish|prefix|append|add)\s+(?:your|the|every|each)\s+(?:summary|answer|response|reply|"
    r"output|message)\b[^.\n]{0,20}\bwith\b"
)
_ALWAYS = re.compile(
    r"(?:^[ \t]{0,8}|[.!?]\s{0,8})(?:always|never) (?:recommend|mention|say|tell|answer|respond)"
)
_READ_COND = re.compile(
    r"(?<![a-z])(?:when|while|if|whenever) you (?:summari[sz]e|paraphrase|analy[sz]e|process|parse|scrape|crawl)\b"
    r"[^,:.]{0,40}[,:]"
    r"|(?:при|во время|когда|если) (?:ты |вы )?(?:пересказыва\w+|суммариз\w+|анализиру\w+|обрабатыва\w+|парсишь|"
    r"парсите)[^,:.]{0,40}[,:]"
    r"|if you(?:'re| are)? (?:reading|processing|summari[sz]ing|scraping|analy[sz]ing|parsing) (?:this|it)"
    r"(?: (?:page|text|document))? (?:as|being) (?:an? )?(?:ai|llm|language model|assistant|bot|model)"
    r"|(?:если|когда) (?:ты |вы )?(?:читаешь|читаете|обрабатываешь|обрабатываете) (?:это|эту страницу|этот текст) "
    r"(?:как|в качестве) (?:ии|нейросет\w*|языковая модель|ассистент|бот|модель)"
)


def _tokens(unit: str) -> list[str]:
    return _TOKEN.findall(unit)


def _units(text: str) -> list[tuple[str, bool]]:
    """Предложения текста; второй член — предложение открыто кавычкой (цитата команды, а не обращение)."""
    out = []
    for raw in _UNIT_SPLIT.split(text):
        raw = raw.strip()
        u = raw if raw[:1].isalpha() else _BULLET.sub("", raw)
        if u:
            out.append((u, raw[:1] in "\u00ab\u201c\u201e\"'"))
    return out


def _clause_start(text: str, pos: int) -> bool:
    """Глагол стоит в начале предложения или оборота: «Ignore …», «и забудь …», «please ignore …»."""
    before = text[:pos].rstrip(" ")
    if not before or before[-1] in ".!?;\n,:\u2014\u2013-\u00ab\u201c\"'(*#>":
        return True
    last = before.rsplit(None, 1)[-1]
    return last in _CLAUSE_START or last in ("и", "а", "но", "потом", "пожалуйста", "будь", "добр", "тогда")


def _override(variant: str) -> bool:
    variant = variant.replace("\n", " ")
    for m in _OVERRIDE_EN.finditer(variant):
        if _clause_start(variant, m.start(1)):
            return True
    for m in _OVERRIDE_RU.finditer(variant):
        if _clause_start(variant, m.start()):
            return True
    return False


_CONDITIONAL = re.compile(r"(?:\bif|если|\bwhen|когда)\s*$")


def _hit(pattern: re.Pattern[str], text: str, needles: tuple[str, ...]) -> bool:
    """Шаблон запускается, только если в тексте есть одно из слов, без которых он сработать не может."""
    return any(n in text for n in needles) and pattern.search(text) is not None


_N_YOU = ("you", "pretend", "act as", "mode", "jailbreak", "anything", "developer", "your")
_N_RU_ROLE = (
    "притвор", "представь", "роль", "момента", "теперь", "покажи", "выведи", "раскрой", "повтори", "напечатай",
    "пришли", "сообщи", "назови", "процитируй", "выдай", "отправь",
)  # fmt: skip
_N_SEND = (
    "send", "forward", "upload", "post", "leak", "exfiltrate", "email", "transmit", "include", "insert", "append",
    "add", "put", "embed", "encode", "отправь", "передай", "перешли", "загрузи", "выгрузи", "пришли", "добавь",
    "вставь", "подставь", "включи", "включай",
)  # fmt: skip
_N_INJECT = (
    "вставь", "добавь", "включи", "напиши", "выведи", "припиши", "повтори", "insert", "add", "include", "output",
    "write", "append", "say", "repeat", "begin", "start", "open", "end", "finish", "prefix",
)  # fmt: skip
_N_HIDE = (
    "user", "human", "operator", "person", "reader", "пользовател", "человек", "клиент", "hide", "secret",
    "without", "втайне", "без ведома", "скрой",
)  # fmt: skip
_N_READ = (
    "summari", "paraphrase", "analy", "process", "parse", "scrape", "crawl", "reading", "пересказ", "суммариз",
    "анализир", "обрабатыва", "парсишь", "парсите", "читаешь", "читаете",
)  # fmt: skip


def _signs_of(text: str) -> set[str]:
    """Признаки в одном виде нормализованного текста."""
    found: set[str] = set()
    text = _canon(text)
    if _override(text):
        found.add(SIGN_OVERRIDE)
    if (
        _hit(_TAGS, text, ("<", "[", "##"))
        or _hit(_ROLE_LINE, text, ("system", "assistant", "системное"))
        or _hit(
            _ROLE_HEADER,
            text,
            ("override", "command", "mode", "instruction", "message", "notice", "directive"),
        )
        or _hit(_ROLE_PHRASES, text, _N_YOU + _N_RU_ROLE)
        or _hit(_SYSTEM_COLON, text, ("system", "системн"))
    ):
        found.add(SIGN_ROLE)
    for pattern, needles in (
        (_ROLE_IDENTITY_EN, ("you're", "you are")),
        (_ROLE_IDENTITY_RU, ("ты ", "вы ", "ты,", "вы,")),
    ):
        if any(n in text for n in needles):
            for m in pattern.finditer(text):
                # «если ты ИИ» — обращение к модели; «ты — ассистент, который обязан …» — назначение роли
                conditional = _CONDITIONAL.search(text[max(0, m.start() - 12) : m.start()])
                found.add(SIGN_ADDRESS if conditional else SIGN_ROLE)
    if (
        _hit(_HIDE, text, _N_HIDE)
        or _hit(_EXEC, text, ("выполни", "запусти", "execute", "run the"))
        or _hit(_EXFIL, text, _N_SEND)
        or _hit(_INJECT, text, _N_INJECT)
    ):
        found.add(SIGN_ACTION)
    if _hit(_ALWAYS, text, ("always", "never")) or _hit(_READ_COND, text, _N_READ):
        found.add(SIGN_ADDRESS)
    if any(n in text for n in ("new ", "updated", "revised", "новые", "обновленные")):
        for m in _NEW_RULES.finditer(text):
            if _directive_at(_tokens(text[m.end() : m.end() + 120]), 0):
                found.add(SIGN_ROLE)
    units = _units(text)
    for idx, (unit, quoted) in enumerate(units):
        if not _has_addr_cue(unit):
            continue
        tokens = _tokens(unit)
        if len(tokens) < 2:
            continue
        if (
            (not quoted and _vocative(tokens))
            or _machine_header(tokens)
            or _reading_directive(tokens)
            or _modal_for_ai(tokens, unit)
        ):
            found.add(SIGN_ADDRESS)
            continue
        # «Hi AI!» и следующее предложение — повеление
        if idx + 1 < len(units) and len(tokens) <= 6 and tokens[-1] in _SOFT:
            if _addr_len(tokens, 0) or (tokens[0] in _GREET and _addr_len(tokens, 1)):
                if _directive_at(_tokens(units[idx + 1][0]), 0):
                    found.add(SIGN_ADDRESS)
    return found


# Те же главные фразы, склеенные без пробелов: для слов, разряженных по одной букве через пробел.
_SQUEEZED = (
    (
        SIGN_OVERRIDE,
        re.compile(
            r"(?:ignore|disregard|forget|override)(?:all|any|the|your|every)*(?:previous|prior|above|earlier|system)*"
            r"(?:instructions?|prompts?|rules)|(?:игнорируй|забудь)(?:все|любые)*(?:предыдущие|прежние|прошлые)*"
            r"(?:инструкции|указания|правила|команды)"
        ),
    ),
    (
        SIGN_ROLE,
        re.compile(
            r"(?:reveal|show|print|repeat)(?:me)?(?:your|the)(?:system|hidden)(?:prompt|instructions)|youarenow"
        ),
    ),
)

# Для тестов: названия признаков и их порядок.
INSTRUCTION_RULES = tuple((name, None) for name in INSTRUCTION_SIGNS)


def find_signs(text: str) -> list[str]:
    """Признаки обращения к ИИ в тексте, в порядке правил. Пустой список — обращений не видно."""
    variants, runs = normalize(text)
    found: set[str] = set()
    for variant in variants:
        found |= _signs_of(variant)
        leet = _LEET_WORD.sub(_leet, variant)
        if leet != variant:
            found |= _signs_of(leet)
    for run in runs:
        found |= {name for name, pattern in _SQUEEZED if pattern.search(run)}
    return [name for name in INSTRUCTION_SIGNS if name in found]
