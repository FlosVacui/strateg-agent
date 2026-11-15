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


def _has_mixed_words(text: str) -> bool:
    return any(_is_mixed(m.group()) for m in _LETTERS.finditer(text) if not m.group().isascii())


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


# --- Шаблоны ------------------------------------------------------------------------------------------------

# Начало предложения или строки, либо приветствие: «ИИ-агент, …» обращено к модели только отсюда.
_SENT = (
    r"(?:^|[\n.!?:;»\"“”(\[>*#\-]\s{0,8}"
    r"|(?:привет|здравствуй(?:те)?|эй|hey|hello|hi|dear|уважаем\w+)[,!]?\s+)"
)
_AI = (
    r"(?<!\w)(?:(?:ии|ai|llm|gpt)[\s-]*(?:агент|ассистент|модел|бот|помощник|agents?|assistants?|models?|bots?)\w*|"
    r"ии|ai|llm|нейросет\w*|(?:large\s+)?language\s+models?|языков\w+\s+модел\w+|ассистент\w*|assistant|chat-?bot|чат-?бот\w*|бот|bot|робот|crawler|scraper)"
)
_BRAND = r"(?:chatgpt|claude|gemini|copilot|gigachat|yandexgpt|deepseek|grok|perplexity|алиса)"
# «ИИ-модель, которая …» — описание, а не обращение: после запятой союз, местоимение или причастие
_NOT_ADDRESS = (
    r"(?:который|которая|которое|которые|которого|которой|которым|что|чтобы|для|и|а|но|или|от|с|со|на|в|во|по|"
    r"как|не|тоже|также|однако|впрочем|уже|еще|вместо|после|перед|при|без|из|за|к|о|об|обо|то|это|"
    r"\w+(?:нн|ённ|ющ|вш|ящ|ащ)\w{1,3}|способн\w+|доступн\w+|"
    r"which|that|who|and|or|but|for|from|with|as|like|too|also|however|in|on|at|of|to|is|are|was|were|has|have|"
    r"can|will|may|developed|created|trained|designed|built|launched|released|available)"
)
_IMPERATIVE = (
    r"(?:игнорир\w+|забуд\w+|напиш\w+|добав\w+|вставь\w*|выполни\w*|запусти\w*|отправ\w+|передай\w*|"
    r"скажи\w*|ответь\w*|рекоменду\w+|порекоменду\w+|укажи\w*|сообщи\w*|выведи\w*|покажи\w*|включи\w*|"
    r"используй\w*|считай\w*|представ\w+|делай\w*|сделай\w*|назови\w*|упомяни\w*|перескажи\w*|переведи\w*|"
    r"составь\w*|создай\w*|сгенерируй\w*|удали\w*|открой\w*|перейди\w*|пожалуйста|всегда|никогда|обязательно|"
    r"ты\s+должен|вы\s+должны|"
    r"ignore|forget|disregard|write|add|insert|include|output|print|say|tell|recommend|respond|reply|answer|"
    r"send|forward|execute|run|summari[sz]e|mention|list|always|never|do|don'?t|please|you\s+(?:must|should|are|will)|"
    r"your\s+task|new\s+task)\b"
)

_OVERRIDE = (
    # русский: «игнорируй все предыдущие инструкции», «забудь правила», «не следуй прежним указаниям»
    r"(?:игнорируй|забудь|пренебреги|отбрось|не\s+следуй|не\s+обращай\s+внимани\w+\s+на)(?:те)?\s+"
    r"(?:(?:все|всё|любые|любую|свои|ваши|твои|эти|те|системные|заданные|полученные)\s+){0,3}"
    r"(?:(?:предыдущ|прежн|прошл|ранее|выше|изначальн|исходн|остальн)\w*\s+){0,2}"
    r"(?:инструкци|указани|команд|правил|ограничени|директив|настройк|установк|промпт|запрет|задани)\w*"
    r"|(?:игнорируй|забудь)\w*\s+(?:(?:все|всё),?\s+)?(?:что\s+)?(?:написано\s+|сказано\s+)?(?:выше|вышесказанн\w+|сказанн\w+\s+ранее)"
    # английский
    r"|(?:ignore|disregard|forget|override|do\s+not\s+follow|don'?t\s+follow)\s+"
    r"(?:(?:all|any|every|the|your|my|these|those|of|everything)\s+){0,3}"
    r"(?:(?:previous|prior|above|earlier|preceding|original|initial|system|safety|existing|other)\s+){0,2}"
    r"(?:instructions?|prompts?|rules|directives?|guidelines|commands?|constraints|restrictions)"
    r"|(?:ignore|forget|disregard)\s+(?:(?:all|everything|the)\s+)?(?:(?:that\s+)?(?:above|before)|the\s+text\s+above)"
)

_ADDRESS = (
    # «если ты ИИ», «если вы — нейросеть»
    rf"если\s+(?:ты|вы|тебя|вас)\s+(?:—\s+|-\s+|это\s+)?(?:\w+\s+)?{_AI}(?![\w-])"
    rf"|if\s+you(?:'?re|\s+are)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?{_AI}(?![\w-])"
    # «ИИ-агент, сделай …» — только с начала предложения, после запятой не пояснение
    rf"|{_SENT}{_AI}\s*,\s+(?!{_NOT_ADDRESS}\b)\w"
    rf"|{_SENT}{_AI}\s*[:!—-]\s+{_IMPERATIVE}"
    rf"|{_SENT}{_BRAND}\s*,\s+{_IMPERATIVE}"
    # «Уважаемый ассистент», «Dear AI»
    rf"|(?:уважаем\w+|dear|hey|hello|attention|внимание|привет)[,:]?\s+(?:{_AI}|{_BRAND})\b"
    # «Памятка для ИИ:» — с двоеточием или тире
    r"|(?:note|message|instructions?|attention|notice|reminder|важно|внимание|обращение|сообщение|инструкци\w+|"
    r"примечание|заметка)\s+(?:to|for|для|к)\s+(?:the\s+|any\s+)?"
    rf"(?:{_AI}|language\s+models?|ai\s+agents?)\s*[:\-—]"
    # «ИИ, читающий эту страницу», «assistants reading this page»
    rf"|{_AI}\s*,?\s*(?:reading|processing|summari[sz]ing|scraping|crawling|parsing|analy[sz]ing|читающ\w+|"
    r"обрабатывающ\w+|анализирующ\w+|парсящ\w+|пересказывающ\w+)\s+"
    r"(?:this|the|эт\w+)\s+(?:page|text|document|site|website|content|article|страниц\w+|текст\w*|документ\w*|"
    r"сайт\w*|материал\w*|стать\w+)"
    r"|(?:when|while|if)\s+(?:you\s+)?(?:summari[sz]\w+|read\w*|process\w*|analy[sz]\w*)\s+(?:this|the)\s+"
    r"(?:page|text|document|article|site|website)[,:]?\s+(?:you\s+)?(?:must|should|always|never|please|do\s+not|"
    r"don'?t|make\s+sure|be\s+sure|remember|mention|add|include|say|tell|recommend|write|insert)"
    r"|(?:при|во\s+время)\s+(?:пересказ\w+|анализ\w+|обработк\w+|суммаризаци\w+|чтени\w+)\s+эт\w+\s+"
    r"(?:страниц\w+|текст\w*|документ\w*|статьи|сайта)[,:]?\s+(?:ты\s+|вы\s+)?(?:должен|должны|обязательно|"
    r"всегда|никогда|добавь|упомяни|не\s+|сообщи|скажи|рекомендуй|пиши|вставь)"
)

_ROLE = (
    # разметка чата и служебные теги
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>|\[\s*/?\s*(?:system|inst|sys)\s*\]|<<\s*/?\s*sys\s*>>"
    r"|<\|(?:im_start|im_end|system|endoftext)\|>"
    r"|^[ \t]{0,8}(?:#{1,4}[ \t]{0,3})?(?:system|assistant|системное\s+сообщение)\s*:\s*\S"
    r"|system\s+(?:prompt|message|instructions?)\s*:\s*(?:you|ignore|always|never|do|don'?t|from|the\s+assistant|"
    r"ты|вы|всегда|никогда)"
    r"|системн\w+\s+(?:промпт|инструкци\w+|сообщени\w+)\s*:\s*(?:ты|вы|всегда|никогда|игнорируй|не)\b"
    r"|new\s+system\s+prompt|новый\s+системный\s+промпт"
    # просьба раскрыть настройки
    r"|(?:reveal|show|print|repeat|output|display|leak|disclose|share|tell\s+me|give\s+me|write\s+out|send)\s+"
    r"(?:me\s+)?(?:your|the|its)\s+(?:(?:full|complete|hidden|initial|original|secret)\s+)?"
    r"(?:system\s+(?:prompt|instructions?|message)|hidden\s+(?:prompt|instructions?)|initial\s+(?:prompt|instructions?))"
    r"|(?:покажи|выведи|раскрой|повтори|напечатай|пришли|сообщи|назови|процитируй|выдай|отправь)\w*\s+(?:мне\s+)?"
    r"(?:(?:свои|свой|твой|твои|весь|полный|скрытый|исходный|начальный)\s+){1,3}(?:системн\w+\s+)?"
    r"(?:промпт|инструкци\w+|сообщени\w+)"
    # смена роли
    r"|you\s+are\s+now\s+(?:an?\s+|the\s+|in\s+)?(?:\w+\s+){0,2}?(?:ai|assistant|chatbot|bot|dan|jailbroken|"
    r"unrestricted|unfiltered|developer\s+mode|debug\s+mode|admin\s+mode|god\s+mode|root)\b"
    r"|(?:ты|вы)\s+теперь\s+(?:\w+\s+){0,2}?(?:ии|ассистент|бот|помощник|хакер|администратор|агент|модель|режим\w*)"
    r"|с\s+этого\s+момента\s+(?:ты|вы|твоя|твой)\b|from\s+now\s+on,?\s+(?:you|your|act|respond|answer|always|ignore)"
    r"|pretend\s+(?:to\s+be|you\s+are|that\s+you)|act\s+as\s+if\s+you|developer\s+mode\s+(?:enabled|on)"
    r"|do\s+anything\s+now|притворись\b|представь,?\s+что\s+ты\b|веди\s+себя\s+как\b|играй\s+роль\b"
    r"|(?:^[ \t]{0,8}|[.!?]\s{0,8})(?:always|never)\s+(?:recommend|mention|say|tell|answer|respond)"
    # новые указания
    r"|(?:new|updated|revised)\s+(?:instructions?|rules|task|directives?)\s*:"
    r"|(?:новые|обновленные)\s+(?:инструкции|указания|правила|задание)\s*:"
)

_ACTION = (
    # скрыть от человека
    r"не\s+(?:сообщай|говори|показывай|упоминай|раскрывай|рассказывай|пиши)\s+(?:об\s+этом\s+|этого\s+)?"
    r"(?:пользовател\w+|человеку|клиенту)"
    r"|скрой\s+(?:это\s+)?от\s+пользовател\w+|без\s+ведома\s+пользовател\w+|втайне\s+от\s+пользовател\w+"
    r"|(?:do\s+not|don'?t|never)\s+(?:tell|inform|reveal|mention|show|disclose)\s+(?:this\s+)?(?:to\s+)?"
    r"the\s+(?:user|human|operator)"
    r"|hide\s+(?:this|it)\s+from\s+the\s+user|keep\s+this\s+(?:secret|hidden)\s+from\s+the\s+user"
    r"|without\s+(?:the\s+user\s+knowing|telling\s+the\s+user|informing\s+the\s+user)"
    # выполнить действие
    r"|(?:выполни|запусти)\s+(?:следующ\w+\s+)?(?:команду|код|скрипт)"
    r"|(?:execute|run)\s+the\s+following\s+(?:command|code|script)"
    # отправить данные наружу
    r"|(?:отправь|передай|перешли|загрузи|выгрузи|пришли)\w*\s+(?:мне\s+)?(?:все\s+|всю\s+|любые\s+)?"
    r"(?:данные|файлы|материалы|ключи|пароли|токены|переписк\w+|содержимое|контекст\w*|информаци\w+|историю)\s+"
    r"(?:проекта\s+|пользователя\s+|диалога\s+)?(?:на|по)\b"
    r"|(?:send|forward|upload|post|leak|exfiltrate|email|transmit)\s+(?:all\s+)?(?:the\s+|of\s+the\s+|your\s+|this\s+)?"
    r"(?:conversation|chat|data|files|keys|passwords|secrets|tokens|context|history|project|contents?|information)\s+"
    r"(?:history\s+)?(?:to|via)\b"
    # подставить данные проекта в запрос
    r"|(?:include|insert|append|add|put|embed|encode)\s+(?:\w+\s+){0,4}(?:project|user|private|confidential|internal|"
    r"conversation)\s+(?:data|details|information|files|context|name)\s+(?:in|into|to|at\s+the\s+end\s+of)\s+"
    r"(?:\w+\s+){0,3}(?:quer(?:y|ies)|url|request|link)"
    r"|(?:добавь|вставь|подставь|включи|включай)\w*\s+(?:\w+\s+){0,4}(?:данн\w+|название|имя|материал\w+|содержимое|"
    r"контекст)\s+(?:проекта|пользователя|диалога)\s+(?:в|к)\s+(?:\w+\s+){0,3}(?:запрос\w*|url|адрес\w*|ссылк\w*)"
    # вставить в ответ чужую фразу
    r"|(?:вставь|добавь|включи|напиши|выведи|припиши|повтори)\w*\s+(?:\w+\s+){0,3}(?:в|на)\s+(?:\w+\s+){0,2}"
    r"(?:отчет\w*|ответ\w*|вывод\w*|резюме|выжимк\w*)\s+(?:фраз\w+|слова|строк\w+|текст|сообщени\w+|ссылк\w+)"
    r"|(?:insert|add|include|output|write|append|say|repeat)\s+(?:the\s+)?(?:following\s+)?"
    r"(?:phrase|words?|string|sentence|text|line)\b[^.\n]{0,60}\b(?:in|into|to|at\s+the\s+(?:start|end)\s+of)\s+"
    r"(?:your|the)\s+(?:report|response|answer|reply|summary|output|message)"
)

# (признак для сообщения, шаблон). В сообщения попадает только название признака, не сам текст.
INSTRUCTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("игнорировать прежние указания", re.compile(_OVERRIDE, re.MULTILINE)),
    ("обращение к ИИ или ассистенту", re.compile(_ADDRESS, re.MULTILINE)),
    ("смена роли или системная инструкция", re.compile(_ROLE, re.MULTILINE)),
    ("требование скрыть от пользователя или выполнить действие", re.compile(_ACTION, re.MULTILINE)),
)

# Те же главные фразы, склеенные без пробелов: для слов, разряженных по одной букве через пробел.
_SQUEEZED = (
    (
        INSTRUCTION_RULES[0][0],
        re.compile(
            r"(?:ignore|disregard|forget|override)(?:all|any|the|your|every)*(?:previous|prior|above|earlier|system)*"
            r"(?:instructions?|prompts?|rules)|(?:игнорируй|забудь)(?:все|любые)*(?:предыдущие|прежние|прошлые)*"
            r"(?:инструкции|указания|правила|команды)"
        ),
    ),
    (
        INSTRUCTION_RULES[2][0],
        re.compile(
            r"(?:reveal|show|print|repeat)(?:me)?(?:your|the)(?:system|hidden)(?:prompt|instructions)|youarenow"
        ),
    ),
)


def find_signs(text: str) -> list[str]:
    """Признаки обращения к ИИ в тексте, в порядке правил. Пустой список — обращений не видно."""
    variants, runs = normalize(text)
    found: set[str] = set()
    for variant in variants:
        found |= {name for name, pattern in INSTRUCTION_RULES if pattern.search(variant)}
        leet = _LEET_WORD.sub(_leet, variant)
        if leet != variant:
            found |= {
                name for name, pattern in INSTRUCTION_RULES if name not in found and pattern.search(leet)
            }
    for run in runs:
        found |= {name for name, pattern in _SQUEEZED if pattern.search(run)}
    order = [name for name, _ in INSTRUCTION_RULES]
    return [name for name in order if name in found]
