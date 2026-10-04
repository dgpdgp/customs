"""Переводы интерфейса: русский (по умолчанию), английский, грузинский.

Язык выбирается переключателем в шапке и хранится в cookie `lang`. Для текущего
запроса (и фоновой обработки задачи) язык лежит в contextvar, поэтому сообщения
об ошибках и замечания валидатора можно переводить в любом месте кода через t().

Переводятся только надписи интерфейса и сообщения системы. Данные из документов
(наименования, адреса, описания товаров) остаются как есть.

ВНИМАНИЕ: грузинские тексты подготовлены без участия носителя языка —
их нужно проверить, особенно таможенную терминологию.
"""

from contextvars import ContextVar, Token

# Порядок словаря = порядок кнопок переключателя в шапке
LANGUAGES = {"ka": "ქართული", "en": "English", "ru": "Русский"}
LANGUAGE_SHORT = {"ka": "KA", "en": "EN", "ru": "RU"}
DEFAULT_LANG = "ru"  # запасной язык для текстов; язык сайта по умолчанию — настройка DEFAULT_LANGUAGE
COOKIE_NAME = "lang"

_current: ContextVar[str] = ContextVar("lang", default=DEFAULT_LANG)


def default_language() -> str:
    from app.config import get_settings  # локальный импорт: config не должен зависеть от i18n

    code = get_settings().default_language
    return code if code in LANGUAGES else DEFAULT_LANG


def normalize(code: str | None) -> str:
    """Код языка из cookie/настроек; неизвестный или пустой — язык сайта по умолчанию."""
    code = (code or "").strip().lower()[:2]
    return code if code in LANGUAGES else default_language()


def get_lang() -> str:
    return _current.get()


def set_lang(code: str | None) -> Token:
    return _current.set(normalize(code))


def reset_lang(token: Token) -> None:
    _current.reset(token)


def t(key: str, lang: str | None = None, **params: object) -> str:
    """Перевод по ключу; неизвестный язык → русский, неизвестный ключ → сам ключ."""
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    text = entry.get(lang or get_lang()) or entry[DEFAULT_LANG]
    return text.format(**params) if params else text


# Ключи, которые нужны JavaScript (экран сравнения и страница задачи)
_JS_PREFIXES = ("js.", "field.", "party.", "pfield.", "status.", "ui.rev.")


def js_messages(lang: str) -> dict[str, str]:
    return {key: t(key, lang) for key in MESSAGES if key.startswith(_JS_PREFIXES)}


# Подсказка модели, на каком языке писать поле message в замечаниях
LLM_ISSUE_LANGUAGE = {
    "ru": "Поле message в issues пиши на русском языке.",
    "en": "Поле message в issues пиши на английском языке (English).",
    "ka": "Поле message в issues пиши на грузинском языке (ქართული).",
}


# key: (русский, английский, грузинский)
_RAW: dict[str, tuple[str, str, str]] = {
    # ---------- Статусы задач ----------
    "status.processing": ("обработка", "processing", "მუშავდება"),
    "status.review": ("на проверке", "in review", "შემოწმებაზე"),
    "status.approved": ("утверждено", "approved", "დამტკიცებულია"),
    "status.exported": ("выгружено", "exported", "ექსპორტირებულია"),
    "status.failed": ("ошибка", "failed", "შეცდომა"),

    # ---------- Поля декларации ----------
    "field.declaration_type": ("Тип декларации", "Declaration type", "დეკლარაციის ტიპი"),
    "field.customs_office": ("Таможенный пост", "Customs office", "საბაჟო ორგანო"),
    "party.exporter": ("Отправитель", "Exporter", "გამგზავნი"),
    "party.importer": ("Получатель", "Importer", "მიმღები"),
    "party.declarant": ("Декларант / брокер", "Declarant / broker", "დეკლარანტი / ბროკერი"),
    "pfield.name": ("наименование", "name", "დასახელება"),
    "pfield.address": ("адрес", "address", "მისამართი"),
    "pfield.country": ("страна", "country", "ქვეყანა"),
    "pfield.tax_id": ("ИНН / TIN", "Tax ID", "საიდენტიფიკაციო კოდი"),
    "field.contract_number": ("Контракт №", "Contract No.", "კონტრაქტის №"),
    "field.contract_date": ("Дата контракта", "Contract date", "კონტრაქტის თარიღი"),
    "field.delivery_terms": ("Инкотермс", "Incoterms", "ინკოტერმსი"),
    "field.delivery_place": ("Пункт поставки", "Delivery place", "მიწოდების ადგილი"),
    "field.currency": ("Валюта", "Currency", "ვალუტა"),
    "field.country_of_dispatch": ("Страна отправления", "Country of dispatch", "გაგზავნის ქვეყანა"),
    "field.country_of_destination": ("Страна назначения", "Country of destination", "დანიშნულების ქვეყანა"),
    "field.transport_mode": ("Вид транспорта", "Transport mode", "ტრანსპორტის სახე"),
    "field.invoice_numbers": ("Инвойсы", "Invoices", "ინვოისები"),
    "field.invoice_date": ("Дата инвойса", "Invoice date", "ინვოისის თარიღი"),
    "field.transport_document": ("Транспортный документ", "Transport document", "სატრანსპორტო დოკუმენტი"),
    "field.vehicle_id": ("Номер ТС", "Vehicle No.", "სატრანსპორტო საშუალების №"),
    "field.total_invoice_value": ("Сумма по инвойсу", "Invoice total", "ინვოისის ჯამი"),
    "field.total_packages": ("Мест всего", "Total packages", "ადგილები სულ"),
    "field.total_gross_weight_kg": ("Брутто итого, кг", "Total gross weight, kg", "ბრუტო წონა სულ, კგ"),
    "field.total_net_weight_kg": ("Нетто итого, кг", "Total net weight, kg", "ნეტო წონა სულ, კგ"),
    "field.description": ("Описание", "Description", "აღწერა"),
    "field.article": ("Артикул", "Article No.", "არტიკული"),
    "field.hs_code": ("Код ТН ВЭД", "HS code", "სასაქონლო კოდი (HS)"),
    "field.country_of_origin": ("Страна происхождения", "Country of origin", "წარმოშობის ქვეყანა"),
    "field.quantity": ("Количество", "Quantity", "რაოდენობა"),
    "field.unit": ("Единица", "Unit", "ერთეული"),
    "field.packages": ("Мест", "Packages", "ადგილები"),
    "field.gross_weight_kg": ("Брутто, кг", "Gross weight, kg", "ბრუტო, კგ"),
    "field.net_weight_kg": ("Нетто, кг", "Net weight, kg", "ნეტო, კგ"),
    "field.unit_price": ("Цена за ед.", "Unit price", "ერთეულის ფასი"),
    "field.total_value": ("Стоимость", "Value", "ღირებულება"),

    # ---------- Шапка, вход, регистрация ----------
    "ui.logout": ("Выйти", "Log out", "გასვლა"),
    "ui.language": ("Язык", "Language", "ენა"),
    "ui.email": ("Email", "Email", "ელფოსტა"),
    "ui.password": ("Пароль", "Password", "პაროლი"),
    "ui.login.title": ("Вход", "Sign in", "შესვლა"),
    "ui.login.submit": ("Войти", "Sign in", "შესვლა"),
    "ui.login.no_account": ("Нет аккаунта?", "No account?", "არ გაქვთ ანგარიში?"),
    "ui.register.title": ("Регистрация", "Registration", "რეგისტრაცია"),
    "ui.register.link": ("Регистрация", "Register", "რეგისტრაცია"),
    "ui.register.invite": ("Код приглашения", "Invitation code", "მოწვევის კოდი"),
    "ui.register.invite_hint": ("Код выдаёт администратор сайта", "The code is provided by the site administrator",
                                "კოდს გასცემს საიტის ადმინისტრატორი"),
    "ui.register.full_name": ("ФИО (необязательно)", "Full name (optional)", "სახელი და გვარი (არასავალდებულო)"),
    "ui.register.email": ("Email — будет вашим логином", "Email — this will be your login",
                          "ელფოსტა — ეს იქნება თქვენი ლოგინი"),
    "ui.register.password": ("Пароль (минимум 8 символов)", "Password (at least 8 characters)",
                             "პაროლი (მინიმუმ 8 სიმბოლო)"),
    "ui.register.password2": ("Повторите пароль", "Repeat password", "გაიმეორეთ პაროლი"),
    "ui.register.submit": ("Создать аккаунт", "Create account", "ანგარიშის შექმნა"),
    "ui.register.have_account": ("Уже есть аккаунт?", "Already have an account?", "უკვე გაქვთ ანგარიში?"),

    # ---------- Рабочая область ----------
    "ui.dash.title": ("Рабочая область", "Workspace", "სამუშაო სივრცე"),
    "ui.dash.new": ("Новая декларация", "New declaration", "ახალი დეკლარაცია"),
    "ui.dash.intro": (
        "Реквизиты, условия поставки и брокер берутся из эталонной декларации, товарные позиции — из новых "
        "инвойсов и упаковочных листов. Максимум {mb} МБ на файл.",
        "Company details, delivery terms and the broker are taken from the reference declaration; goods items "
        "come from the new invoices and packing lists. Maximum {mb} MB per file.",
        "რეკვიზიტები, მიწოდების პირობები და ბროკერი აიღება ეტალონური დეკლარაციიდან, სასაქონლო პოზიციები — "
        "ახალი ინვოისებიდან და შეფუთვის ფურცლებიდან. მაქსიმუმ {mb} მბ თითო ფაილზე.",
    ),
    "ui.dash.ref_label": ("1. Эталонная (правильная) декларация", "1. Reference (correct) declaration",
                          "1. ეტალონური (სწორი) დეკლარაცია"),
    "ui.dash.ref_hint": ("Ранее принятая декларация этого импортёра: PDF, XML или Excel",
                         "A previously accepted declaration of this importer: PDF, XML or Excel",
                         "ამ იმპორტიორის ადრე მიღებული დეკლარაცია: PDF, XML ან Excel"),
    "ui.dash.com_label": ("2. Новые коммерческие документы", "2. New commercial documents",
                          "2. ახალი კომერციული დოკუმენტები"),
    "ui.dash.com_hint": ("Инвойсы и упаковочные листы: PDF или Excel, можно несколько файлов",
                         "Invoices and packing lists: PDF or Excel, several files allowed",
                         "ინვოისები და შეფუთვის ფურცლები: PDF ან Excel, შეიძლება რამდენიმე ფაილი"),
    "ui.dash.tpl_label": ("3. Шаблон результата", "3. Output template", "3. შედეგის შაბლონი"),
    "ui.dash.optional": ("(необязательно)", "(optional)", "(არასავალდებულო)"),
    "ui.dash.tpl_hint_a": ("XML или Excel с плейсхолдерами", "XML or Excel with placeholders",
                           "XML ან Excel ჩასანაცვლებელი ველებით"),
    "ui.dash.tpl_hint_b": ("Без шаблона — стандартный XML. Примеры — в папке",
                           "Without a template you get the standard XML. Examples are in the folder",
                           "შაბლონის გარეშე — სტანდარტული XML. მაგალითები საქაღალდეშია:"),
    "ui.dash.group": ("Объединять позиции с одинаковым кодом ТН ВЭД и страной происхождения",
                      "Merge items with the same HS code and country of origin",
                      "გაერთიანდეს პოზიციები ერთნაირი სასაქონლო კოდით და წარმოშობის ქვეყნით"),
    "ui.dash.group_hint": ("(суммы считает сервер, а не ИИ)", "(totals are calculated by the server, not the AI)",
                           "(ჯამებს ითვლის სერვერი და არა AI)"),
    "ui.dash.submit": ("Обработать документы", "Process documents", "დოკუმენტების დამუშავება"),
    "ui.dash.uploading": ("Загрузка…", "Uploading…", "იტვირთება…"),
    "ui.dash.recent": ("Последние задачи", "Recent tasks", "ბოლო დავალებები"),
    "ui.dash.task": ("Задача №{id}", "Task No. {id}", "დავალება №{id}"),
    "ui.dash.no_tasks": ("Пока нет задач.", "No tasks yet.", "დავალებები ჯერ არ არის."),

    # ---------- Страница задачи ----------
    "ui.rev.back": ("← Рабочая область", "← Workspace", "← სამუშაო სივრცე"),
    "ui.rev.role.reference": ("Эталон", "Reference", "ეტალონი"),
    "ui.rev.role.commercial": ("Документ", "Document", "დოკუმენტი"),
    "ui.rev.role.template": ("Шаблон", "Template", "შაბლონი"),
    "ui.rev.processing": ("Документы обрабатываются", "Documents are being processed", "დოკუმენტები მუშავდება"),
    "ui.rev.processing_hint": (
        "Разбор файлов и извлечение данных моделью может занять несколько минут — страница обновится автоматически.",
        "Reading the files and extracting the data may take a few minutes — the page will update automatically.",
        "ფაილების დამუშავებას და მონაცემების ამოღებას შეიძლება რამდენიმე წუთი დასჭირდეს — "
        "გვერდი ავტომატურად განახლდება.",
    ),
    "ui.rev.failed": ("Обработка завершилась с ошибкой", "Processing failed", "დამუშავება შეცდომით დასრულდა"),
    "ui.rev.retry": ("Повторить обработку", "Retry processing", "დამუშავების გამეორება"),
    "ui.rev.only_changes": ("Показывать только изменения", "Show changes only", "მხოლოდ ცვლილებების ჩვენება"),
    "ui.rev.legend.changed": ("изменено", "changed", "შეცვლილია"),
    "ui.rev.legend.added": ("добавлено", "added", "დამატებულია"),
    "ui.rev.legend.removed": ("удалено", "removed", "წაშლილია"),
    "ui.rev.legend.unexpected": ("изменён постоянный реквизит", "constant detail changed",
                                 "შეიცვალა მუდმივი რეკვიზიტი"),
    "ui.rev.approve": ("Подтвердить данные", "Confirm data", "მონაცემების დადასტურება"),
    "ui.rev.approve_hint": (
        "Проверьте подсвеченные поля. Значения справа можно исправить перед подтверждением.",
        "Check the highlighted fields. Values on the right can be corrected before confirming.",
        "შეამოწმეთ გამოკვეთილი ველები. მარჯვენა მნიშვნელობების შესწორება დადასტურებამდე შეიძლება.",
    ),
    "ui.rev.approved": ("Данные утверждены", "Data approved", "მონაცემები დამტკიცებულია"),
    "ui.rev.download": ("Скачать по шаблону", "Download using template", "შაბლონით ჩამოტვირთვა"),
    "ui.rev.download_json": ("Скачать JSON", "Download JSON", "JSON-ის ჩამოტვირთვა"),

    # ---------- JavaScript: экран сравнения ----------
    "js.col.field": ("Поле", "Field", "ველი"),
    "js.col.old": ("Эталон (старая декларация)", "Reference (old declaration)", "ეტალონი (ძველი დეკლარაცია)"),
    "js.col.new": ("Новая декларация", "New declaration", "ახალი დეკლარაცია"),
    "js.sec.header": ("Реквизиты и условия поставки", "Company details and delivery terms",
                      "რეკვიზიტები და მიწოდების პირობები"),
    "js.sec.header_sub": ("Переносятся из эталона и должны совпадать полностью",
                          "Copied from the reference and must match exactly",
                          "გადმოდის ეტალონიდან და სრულად უნდა ემთხვეოდეს"),
    "js.sec.shipment": ("Данные поставки", "Shipment data", "მიწოდების მონაცემები"),
    "js.sec.shipment_sub": ("Берутся из новых инвойсов и упаковочных листов",
                            "Taken from the new invoices and packing lists",
                            "აიღება ახალი ინვოისებიდან და შეფუთვის ფურცლებიდან"),
    "js.sec.items": ("Товарные позиции", "Goods items", "სასაქონლო პოზიციები"),
    "js.sec.items_sub": ("Слева — сопоставленная позиция эталона (тот же товар), справа — новая позиция",
                         "Left: the matching reference item (same goods); right: the new item",
                         "მარცხნივ — ეტალონის შესაბამისი პოზიცია (იგივე საქონელი), მარჯვნივ — ახალი პოზიცია"),
    "js.sec.general": ("Общие замечания", "General remarks", "ზოგადი შენიშვნები"),
    "js.all_same": ("Изменений нет — все значения совпадают с эталоном", "No changes — all values match the reference",
                    "ცვლილებები არ არის — ყველა მნიშვნელობა ემთხვევა ეტალონს"),
    "js.item.removed": ("Позиция эталона №{ref} — нет в новой поставке",
                        "Reference item No. {ref} — not in the new shipment",
                        "ეტალონის პოზიცია №{ref} — ახალ მიწოდებაში არ არის"),
    "js.item.matched": ("Позиция №{cur} ↔ позиция эталона №{ref}", "Item No. {cur} ↔ reference item No. {ref}",
                        "პოზიცია №{cur} ↔ ეტალონის პოზიცია №{ref}"),
    "js.item.new": ("Позиция №{cur} — новый товар, аналога в эталоне нет",
                    "Item No. {cur} — new goods, no match in the reference",
                    "პოზიცია №{cur} — ახალი საქონელი, ეტალონში ანალოგი არ არის"),
    "js.item_no": ("позиция №{n}", "item No. {n}", "პოზიცია №{n}"),
    "js.source": ("Источник", "Source", "წყარო"),
    "js.basis.document": ("код из документа", "code from document", "კოდი დოკუმენტიდან"),
    "js.basis.reference_match": ("код по эталону", "code from reference", "კოდი ეტალონიდან"),
    "js.basis.not_found": ("код не найден", "code not found", "კოდი ვერ მოიძებნა"),
    "js.basis.manual": ("введён вручную", "entered manually", "შეყვანილია ხელით"),
    "js.chip.header_ok": ("Реквизиты совпадают с эталоном", "Company details match the reference",
                          "რეკვიზიტები ემთხვევა ეტალონს"),
    "js.chip.header_changed": ("Изменено реквизитов: {n}", "Details changed: {n}", "შეცვლილი რეკვიზიტები: {n}"),
    "js.chip.shipment": ("Изменено полей поставки: {n}", "Shipment fields changed: {n}",
                         "მიწოდების შეცვლილი ველები: {n}"),
    "js.chip.matched": ("Позиций сопоставлено с эталоном: {n}", "Items matched to the reference: {n}",
                        "ეტალონთან შედარებული პოზიციები: {n}"),
    "js.chip.new": ("Новых позиций без аналога в эталоне: {n}", "New items without a reference match: {n}",
                    "ახალი პოზიციები ეტალონში ანალოგის გარეშე: {n}"),
    "js.chip.removed": ("Позиций эталона нет в поставке: {n}", "Reference items missing from the shipment: {n}",
                        "ეტალონის პოზიციები, რომლებიც მიწოდებაში არ არის: {n}"),
    "js.chip.errors": ("Ошибок: {n}", "Errors: {n}", "შეცდომები: {n}"),
    "js.chip.warnings": ("Предупреждений: {n}", "Warnings: {n}", "გაფრთხილებები: {n}"),
    "js.invalid_numbers": ("Исправьте некорректные числовые значения (выделены красной рамкой).",
                           "Fix invalid numbers (outlined in red).",
                           "შეასწორეთ არასწორი რიცხვები (მონიშნულია წითელი ჩარჩოთი)."),
    "js.unknown_error": ("Неизвестная ошибка", "Unknown error", "უცნობი შეცდომა"),
    "js.confirm_errors": ("Замечаний с уровнем «ошибка»: {n}. Вы проверили их и подтверждаете данные?",
                          "Issues with level “error”: {n}. Have you checked them and want to confirm the data?",
                          "„შეცდომის“ დონის შენიშვნები: {n}. შეამოწმეთ ისინი და ადასტურებთ მონაცემებს?"),
    "js.save_failed": ("Не удалось сохранить: {error}", "Could not save: {error}", "შენახვა ვერ მოხერხდა: {error}"),
    "js.error_status": ("Ошибка {status}", "Error {status}", "შეცდომა {status}"),

    # ---------- Сообщения сервера ----------
    "reg.closed": ("Регистрация закрыта. Учётную запись создаёт администратор сервера.",
                   "Registration is closed. Accounts are created by the server administrator.",
                   "რეგისტრაცია დახურულია. ანგარიშს ქმნის სერვერის ადმინისტრატორი."),
    "reg.bad_invite": ("Неверный код приглашения. Узнайте его у администратора сайта.",
                       "Invalid invitation code. Ask the site administrator for it.",
                       "მოწვევის კოდი არასწორია. გაიგეთ ის საიტის ადმინისტრატორისგან."),
    "reg.pw_mismatch": ("Пароли не совпадают", "Passwords do not match", "პაროლები არ ემთხვევა"),
    "reg.bad_email": ("Некорректный email: введите адрес почты целиком, например name@gmail.com",
                      "Invalid email: enter the full address, e.g. name@gmail.com",
                      "არასწორი ელფოსტა: შეიყვანეთ სრული მისამართი, მაგ. name@gmail.com"),
    "login.failed": ("Неверный email или пароль", "Wrong email or password", "არასწორი ელფოსტა ან პაროლი"),
    "auth.pw_short": ("Пароль должен быть не короче 8 символов", "Password must be at least 8 characters",
                      "პაროლი უნდა შედგებოდეს მინიმუმ 8 სიმბოლოსგან"),
    "auth.pw_long": ("Пароль слишком длинный (максимум 72 байта)", "Password is too long (max 72 bytes)",
                     "პაროლი ზედმეტად გრძელია (მაქსიმუმ 72 ბაიტი)"),
    "auth.email_taken": ("Пользователь с таким email уже зарегистрирован",
                         "A user with this email is already registered",
                         "ამ ელფოსტით მომხმარებელი უკვე რეგისტრირებულია"),
    "auth.required": ("Требуется авторизация", "Authentication required", "საჭიროა ავტორიზაცია"),
    "limit.login": ("Слишком много неудачных попыток входа. Повторите через {wait} мин.",
                    "Too many failed login attempts. Try again in {wait} min.",
                    "შესვლის ზედმეტად ბევრი წარუმატებელი მცდელობა. სცადეთ {wait} წუთში."),
    "limit.user_quota": ("Достигнут лимит: {n} обработок за 24 часа для вашей учётной записи.",
                         "Limit reached: {n} processings per 24 hours for your account.",
                         "ლიმიტი ამოიწურა: თქვენი ანგარიშისთვის 24 საათში {n} დამუშავება."),
    "limit.total_quota": ("Достигнут общий суточный лимит обработок на сервере. Повторите позже.",
                          "The server's daily processing limit has been reached. Try again later.",
                          "სერვერის დღიური დამუშავების ლიმიტი ამოიწურა. სცადეთ მოგვიანებით."),
    "job.not_found": ("Задача не найдена", "Task not found", "დავალება ვერ მოიძებნა"),
    "job.approve_first": ("Сначала проверьте и утвердите данные", "Review and approve the data first",
                          "ჯერ შეამოწმეთ და დაადასტურეთ მონაცემები"),
    "job.not_ready": ("Задача ещё не готова к утверждению", "The task is not ready for approval yet",
                      "დავალება ჯერ არ არის მზად დასადასტურებლად"),
    "job.retry_only_failed": ("Повторить можно только задачу с ошибкой", "Only a failed task can be retried",
                              "გამეორება შესაძლებელია მხოლოდ შეცდომით დასრულებული დავალებისთვის"),
    "upload.no_reference": ("Загрузите эталонную декларацию", "Upload the reference declaration",
                            "ატვირთეთ ეტალონური დეკლარაცია"),
    "upload.no_commercial": ("Загрузите хотя бы один инвойс или упаковочный лист",
                             "Upload at least one invoice or packing list",
                             "ატვირთეთ მინიმუმ ერთი ინვოისი ან შეფუთვის ფურცელი"),
    "upload.too_many": ("Не больше {n} коммерческих документов", "No more than {n} commercial documents",
                        "არაუმეტეს {n} კომერციული დოკუმენტისა"),
    "upload.bad_ext": ("Файл «{name}»: формат {ext} не подходит. Допустимо: {allowed}",
                       "File “{name}”: format {ext} is not allowed. Allowed: {allowed}",
                       "ფაილი „{name}“: ფორმატი {ext} დაუშვებელია. დასაშვებია: {allowed}"),
    "upload.no_ext": ("без расширения", "no extension", "გაფართოების გარეშე"),
    "upload.too_big": ("Файл «{name}» больше {mb} МБ", "File “{name}” is larger than {mb} MB",
                       "ფაილი „{name}“ {mb} მბ-ზე დიდია"),
    "upload.empty": ("Файл «{name}» пустой", "File “{name}” is empty", "ფაილი „{name}“ ცარიელია"),
    "parse.read_failed": ("Не удалось прочитать файл «{name}»: {error}", "Could not read file “{name}”: {error}",
                          "ფაილის „{name}“ წაკითხვა ვერ მოხერხდა: {error}"),
    "parse.unsupported": ("Формат файла «{name}» не поддерживается", "File format of “{name}” is not supported",
                          "ფაილის „{name}“ ფორმატი მხარდაჭერილი არ არის"),
    "parse.bad_xml": ("Файл «{name}» не является корректным XML: {error}", "File “{name}” is not valid XML: {error}",
                      "ფაილი „{name}“ არ არის სწორი XML: {error}"),
    "export.template_error": ("Ошибка в шаблоне: {error}", "Template error: {error}", "შაბლონის შეცდომა: {error}"),
    "export.unsupported": ("Шаблоны формата {ext} не поддерживаются (нужен .xml, .txt, .csv или .xlsx)",
                           "{ext} templates are not supported (use .xml, .txt, .csv or .xlsx)",
                           "{ext} ფორმატის შაბლონები მხარდაჭერილი არ არის (საჭიროა .xml, .txt, .csv ან .xlsx)"),
    "pipe.interrupted": ("Обработка прервана перезапуском сервера. Нажмите «Повторить обработку».",
                         "Processing was interrupted by a server restart. Click “Retry processing”.",
                         "დამუშავება შეწყდა სერვერის გადატვირთვის გამო. დააჭირეთ „დამუშავების გამეორებას“."),
    "pipe.internal": ("Внутренняя ошибка обработки. Подробности в логе сервера.",
                      "Internal processing error. Details are in the server log.",
                      "დამუშავების შიდა შეცდომა. დეტალები სერვერის ჟურნალშია."),

    # ---------- Ошибки обращения к ИИ ----------
    "llm.scan_too_big": ("Скан «{name}» слишком большой для отправки в ИИ (лимит 32 МБ)",
                         "Scan “{name}” is too large to send to the AI (limit 32 MB)",
                         "სკანი „{name}“ ზედმეტად დიდია AI-ში გასაგზავნად (ლიმიტი 32 მბ)"),
    "llm.input_too_big": ("Документы слишком большие ({size} символов, лимит {limit}). Разделите поставку на "
                          "несколько задач или увеличьте LLM_MAX_INPUT_CHARS.",
                          "Documents are too large ({size} characters, limit {limit}). Split the shipment into "
                          "several tasks or increase LLM_MAX_INPUT_CHARS.",
                          "დოკუმენტები ზედმეტად დიდია ({size} სიმბოლო, ლიმიტი {limit}). დაყავით მიწოდება "
                          "რამდენიმე დავალებად ან გაზარდეთ LLM_MAX_INPUT_CHARS."),
    "llm.unknown_provider": ("Неизвестный LLM_PROVIDER «{provider}»: допустимо anthropic или openai",
                             "Unknown LLM_PROVIDER “{provider}”: use anthropic or openai",
                             "უცნობი LLM_PROVIDER „{provider}“: დასაშვებია anthropic ან openai"),
    "llm.bad_format": ("Модель вернула данные в неожиданном формате, повторите обработку",
                       "The model returned data in an unexpected format, please retry",
                       "მოდელმა მონაცემები მოულოდნელ ფორმატში დააბრუნა, გაიმეორეთ დამუშავება"),
    "llm.no_key": ("На сервере не задан ключ {var}. Обратитесь к администратору.",
                   "The {var} key is not set on the server. Contact the administrator.",
                   "სერვერზე {var} გასაღები არ არის მითითებული. მიმართეთ ადმინისტრატორს."),
    "llm.bad_key": ("Неверный ключ {var}", "Invalid {var} key", "არასწორი {var} გასაღები"),
    "llm.no_access": ("У ключа API нет доступа к модели {model}", "The API key has no access to model {model}",
                      "API გასაღებს არ აქვს წვდომა მოდელზე {model}"),
    "llm.rate_limit": ("Превышен лимит запросов к {provider}, повторите позже",
                       "{provider} rate limit exceeded, try again later",
                       "გადაჭარბებულია {provider}-ის მოთხოვნების ლიმიტი, სცადეთ მოგვიანებით"),
    "llm.quota": ("Превышен лимит запросов или закончились деньги на балансе провайдера",
                  "Rate limit exceeded or the provider balance is empty",
                  "გადაჭარბებულია ლიმიტი ან პროვაიდერის ბალანსი ამოიწურა"),
    "llm.rejected": ("Запрос отклонён API: {detail}", "Request rejected by the API: {detail}",
                     "API-მ მოთხოვნა უარყო: {detail}"),
    "llm.api_error": ("Ошибка API {provider} ({status}), повторите позже",
                      "{provider} API error ({status}), try again later",
                      "{provider} API-ის შეცდომა ({status}), სცადეთ მოგვიანებით"),
    "llm.no_connection": ("Нет соединения с API {provider}", "No connection to the {provider} API",
                          "{provider} API-სთან კავშირი არ არის"),
    "llm.refusal": ("Модель отказалась обрабатывать документы. Проверьте содержимое файлов.",
                    "The model refused to process the documents. Check the file contents.",
                    "მოდელმა დოკუმენტების დამუშავებაზე უარი თქვა. შეამოწმეთ ფაილების შინაარსი."),
    "llm.truncated": ("Ответ модели обрезан: документов слишком много. Увеличьте {var} или разделите поставку.",
                      "The model's answer was cut off: too many documents. Increase {var} or split the shipment.",
                      "მოდელის პასუხი შემოკლდა: დოკუმენტები ზედმეტად ბევრია. გაზარდეთ {var} ან დაყავით "
                      "მიწოდება."),
    "llm.no_model": ("Не задана модель OPENAI_MODEL. Обратитесь к администратору.",
                     "OPENAI_MODEL is not set. Contact the administrator.",
                     "მოდელი OPENAI_MODEL მითითებული არ არის. მიმართეთ ადმინისტრატორს."),
    "llm.scan_unsupported": ("Файл «{name}» — скан без текста. Сканы обрабатываются только через Claude "
                             "(LLM_PROVIDER=anthropic); либо загрузите PDF с текстовым слоем или Excel.",
                             "File “{name}” is a scan without text. Scans are processed only with Claude "
                             "(LLM_PROVIDER=anthropic); otherwise upload a PDF with a text layer or Excel.",
                             "ფაილი „{name}“ ტექსტის გარეშე სკანია. სკანები მუშავდება მხოლოდ Claude-ით "
                             "(LLM_PROVIDER=anthropic); სხვა შემთხვევაში ატვირთეთ ტექსტური PDF ან Excel."),
    "llm.model_not_found": ("Модель «{model}» не найдена: проверьте OPENAI_MODEL и OPENAI_BASE_URL",
                            "Model “{model}” not found: check OPENAI_MODEL and OPENAI_BASE_URL",
                            "მოდელი „{model}“ ვერ მოიძებნა: შეამოწმეთ OPENAI_MODEL და OPENAI_BASE_URL"),
    "llm.empty": ("Провайдер вернул пустой ответ, повторите обработку",
                  "The provider returned an empty answer, please retry",
                  "პროვაიდერმა ცარიელი პასუხი დააბრუნა, გაიმეორეთ დამუშავება"),

    # ---------- Замечания проверки ----------
    "val.source_doc_missing": ("Документ-источник «{doc}» не найден среди загруженных",
                               "Source document “{doc}” is not among the uploaded files",
                               "წყარო დოკუმენტი „{doc}“ ატვირთულ ფაილებს შორის არ არის"),
    "val.source_is_scan": ("Источник «{doc}» — скан: сверьте позицию с документом вручную",
                           "Source “{doc}” is a scan: check this item against the document manually",
                           "წყარო „{doc}“ სკანია: პოზიცია დოკუმენტს ხელით შეადარეთ"),
    "val.quote_missing": ("Не указана цитата-источник: происхождение позиции не подтверждено",
                          "No source quote: the origin of this item is not confirmed",
                          "წყაროს ციტატა მითითებული არ არის: პოზიციის წარმომავლობა დადასტურებული არ არის"),
    "val.quote_not_found": ("Цитата «{quote}» не найдена в документах — позиция могла быть искажена или выдумана",
                            "Quote “{quote}” was not found in the documents — the item may be distorted or invented",
                            "ციტატა „{quote}“ დოკუმენტებში ვერ მოიძებნა — პოზიცია შესაძლოა დამახინჯებული ან "
                            "გამოგონილი იყოს"),
    "val.quote_other_doc": ("Цитата найдена не в «{doc}», а в другом документе",
                            "The quote was found in another document, not in “{doc}”",
                            "ციტატა ნაპოვნია არა „{doc}“-ში, არამედ სხვა დოკუმენტში"),
    "val.number_not_in_docs": ("Значение «{label}» {value} не встречается в тексте документов",
                               "Value “{label}” {value} does not appear in the document text",
                               "მნიშვნელობა „{label}“ {value} დოკუმენტების ტექსტში არ გვხვდება"),
    "val.ref_item_missing": ("Позиция эталона №{no} не существует", "Reference item No. {no} does not exist",
                             "ეტალონის პოზიცია №{no} არ არსებობს"),
    "val.hs_missing": ("Код ТН ВЭД не определён — укажите вручную", "HS code not determined — enter it manually",
                       "სასაქონლო კოდი ვერ დადგინდა — მიუთითეთ ხელით"),
    "val.hs_format": ("Код ТН ВЭД «{code}» должен состоять из {n} цифр", "HS code “{code}” must have {n} digits",
                      "სასაქონლო კოდი „{code}“ უნდა შედგებოდეს {n} ციფრისგან"),
    "val.hs_ref_mismatch": ("Код {code} указан как взятый из эталона, но в позиции эталона №{no} код {ref_code}",
                            "Code {code} is marked as taken from the reference, but reference item No. {no} has "
                            "code {ref_code}",
                            "კოდი {code} მითითებულია როგორც ეტალონიდან აღებული, მაგრამ ეტალონის პოზიცია №{no}-ში "
                            "კოდია {ref_code}"),
    "val.hs_ref_absent": ("отсутствует", "missing", "არ არის"),
    "val.hs_not_in_doc": ("Код {code} указан как взятый из документа, но в тексте документа его нет",
                          "Code {code} is marked as taken from the document, but it is not in the document text",
                          "კოდი {code} მითითებულია როგორც დოკუმენტიდან აღებული, მაგრამ დოკუმენტის ტექსტში არ არის"),
    "val.hs_basis_conflict": ("Модель указала, что код не найден, но заполнила его — проверьте",
                              "The model said the code was not found but filled it in — please check",
                              "მოდელმა მიუთითა, რომ კოდი ვერ იპოვა, მაგრამ შეავსო — შეამოწმეთ"),
    "val.total_missing": ("Итог «{label}» в документах не указан — сверка невозможна",
                          "Total “{label}” is not stated in the documents — cannot reconcile",
                          "ჯამი „{label}“ დოკუმენტებში მითითებული არ არის — შედარება შეუძლებელია"),
    "val.total_incomplete": ("Не у всех позиций заполнено поле «{label}» — сумма не сверена",
                             "Not all items have “{label}” filled in — total not reconciled",
                             "ყველა პოზიციას არ აქვს შევსებული „{label}“ — ჯამი არ შემოწმდა"),
    "val.total_mismatch": ("Сумма по позициям ({label}) {sum} не совпадает с итогом в документах {total}",
                           "Sum of items ({label}) {sum} does not match the document total {total}",
                           "პოზიციების ჯამი ({label}) {sum} არ ემთხვევა დოკუმენტებში მითითებულ ჯამს {total}"),
    "val.net_gt_gross": ("Вес нетто {net} больше брутто {gross}", "Net weight {net} exceeds gross weight {gross}",
                         "ნეტო წონა {net} აღემატება ბრუტოს {gross}"),
    "val.required_empty": ("Не заполнено: {label}", "Not filled in: {label}", "არ არის შევსებული: {label}"),
    "val.ref_party_missing": ("В эталонной декларации не найден реквизит: {label}",
                              "Not found in the reference declaration: {label}",
                              "ეტალონურ დეკლარაციაში ვერ მოიძებნა: {label}"),
    "val.ref_no_items": ("В эталонной декларации не найдено ни одной товарной позиции — коды ТН ВЭД не с чем "
                         "сопоставить",
                         "No goods items found in the reference declaration — nothing to match HS codes against",
                         "ეტალონურ დეკლარაციაში სასაქონლო პოზიციები ვერ მოიძებნა — სასაქონლო კოდების "
                         "შესადარებელი არაფერია"),
    "val.header_changed": ("Постоянные реквизиты изменены вручную относительно эталона",
                           "Constant details were changed manually compared to the reference",
                           "მუდმივი რეკვიზიტები ხელით შეიცვალა ეტალონთან შედარებით"),
    "merge.units_differ": ("Объединены позиции с разными единицами ({units}) — количество не суммировано, "
                           "укажите вручную",
                           "Merged items have different units ({units}) — quantity not summed, enter it manually",
                           "გაერთიანდა სხვადასხვა ერთეულის პოზიციები ({units}) — რაოდენობა არ შეჯამდა, "
                           "მიუთითეთ ხელით"),
    "merge.merged": ("Объединены исходные позиции №{nos}", "Merged original items No. {nos}",
                     "გაერთიანდა საწყისი პოზიციები №{nos}"),
    "wire.bad_number": ("Не удалось распознать число «{value}» ({where})", "Could not read the number “{value}” "
                        "({where})", "რიცხვის „{value}“ ამოცნობა ვერ მოხერხდა ({where})"),
    "wire.not_integer": ("Ожидалось целое число, получено «{value}» ({where})",
                         "Expected a whole number, got “{value}” ({where})",
                         "მოსალოდნელი იყო მთელი რიცხვი, მიღებულია „{value}“ ({where})"),
    "wire.where_ref": ("позиция эталона №{no}", "reference item No. {no}", "ეტალონის პოზიცია №{no}"),
    "wire.where_item": ("позиция №{no}", "item No. {no}", "პოზიცია №{no}"),
}

MESSAGES: dict[str, dict[str, str]] = {
    key: dict(zip(("ru", "en", "ka"), texts, strict=True)) for key, texts in _RAW.items()
}
