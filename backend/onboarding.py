TEST_CATALOG = [
    {"id": "fatigue_basic", "name": "«Энергия и бодрость» — базовый", "default_name": "Хроническая усталость – базовый", "price": 3500,
     "includes": "Ферритин, железо, ТТГ, Витамин Д",
     "description": "Узнайте, почему пропадают силы. Проверка самых частых скрытых причин усталости — дефицита железа, витамина D и нарушений работы щитовидной железы. Быстрый способ понять, откуда «взять энергию»."},
    {"id": "fatigue_extended", "name": "«Энергия и бодрость» — расширенный", "default_name": "Хроническая усталость – расширенный", "price": 6500,
     "includes": "Ферритин, железо, ТТГ, витамин Д, АЛТ, АСТ, общий белок, кортизол, св Т3, св Т4, С-РБ. Ж: эстрадиол. М: тестостерон",
     "description": "Полное обследование при упадке сил и хронической усталости. Помимо базовых показателей — работа печени, уровень стресса (кортизол), щитовидная железа и половые гормоны. Максимально точный ответ на вопрос «почему я так устал(а)»."},
    {"id": "weight_basic", "name": "«Контроль веса» — базовый", "default_name": "Избыточный вес - базовый", "price": 4000,
     "includes": "ТТГ, св Т3, св Т4, АЛТ, АСТ, триглицериды, ЛПВП, ЛПНП, креатинин",
     "description": "Ищем медицинские причины лишнего веса. Проверка гормонов щитовидной железы, обмена жиров и работы печени и почек — то, что мешает похудеть, даже если вы стараетесь."},
    {"id": "weight_extended", "name": "«Контроль веса» — расширенный", "default_name": "Избыточный вес - расширенный", "price": 6500,
     "includes": "ТТГ, св Т3, св Т4, АЛТ, АСТ, триглицериды, ЛПВП, ЛПНП, креатинин, вит Д, кортизол. Ж: тестостерон, эстрадиол. М: тестостерон",
     "description": "Углублённая диагностика причин набора веса: щитовидная железа, обмен жиров, печень, почки, витамин D, гормон стресса кортизол и половые гормоны. Комплексный взгляд на то, что тормозит снижение веса."},
    {"id": "hair_loss", "name": "«Здоровые волосы и кожа»", "default_name": "Выпадение волос", "price": 3000,
     "includes": "Ферритин, железо, ТТГ, общий белок, тестостерон",
     "description": "Найдите истинную причину выпадения волос. Проверка железа, ферритина, щитовидной железы и гормонального фона — главных факторов, влияющих на густоту и рост волос."},
    {"id": "lipids", "name": "«Здоровье сердца и сосудов»", "default_name": "Липидный обмен", "price": 1500,
     "includes": "Триглицериды, ЛПВП, ЛПНП",
     "description": "Быстрая проверка «плохого» и «хорошего» холестерина. Оцените риск атеросклероза и сердечно-сосудистых заболеваний всего по трём ключевым показателям."},
    {"id": "liver_basic", "name": "«Здоровье печени и поджелудочной железы» — базовый", "default_name": "Печень и поджелудочная железа – базовый", "price": 2000,
     "includes": "АЛТ, АСТ, Билирубин общ, Билирубин пр, альфа-амилаза",
     "description": "Контроль работы главных пищеварительных органов. Своевременно выявите нагрузку на печень и поджелудочную железу — до появления симптомов."},
    {"id": "liver_extended", "name": "«Здоровье печени и поджелудочной железы» — расширенный", "default_name": "Печень и поджелудочная железа – расширенный", "price": 2500,
     "includes": "АЛТ, АСТ, Билирубин общ, Билирубин пр, Альфа-амилаза, щелочная фосфатаза, общий белок",
     "description": "Расширенная диагностика печени и поджелудочной железы с дополнительными маркерами застоя желчи и белкового обмена. Для тех, кто хочет полную картину состояния органов пищеварения."},
    {"id": "iron", "name": "«Профилактика анемии»", "default_name": "Железо и анемия", "price": 1000,
     "includes": "Общий железо, ферритин",
     "description": "Простой способ проверить, хватает ли организму железа. Быстрая диагностика скрытого дефицита железа — частой причины слабости, бледности и снижения работоспособности."},
    {"id": "kidneys", "name": "«Здоровье почек»", "default_name": "Почки", "price": 1000,
     "includes": "Общий белок, мочевина, креатинин",
     "description": "Оцените, как работают ваши почки. Базовый набор показателей для раннего выявления нарушений почечной функции."},
    {"id": "protein", "name": "«Баланс белка»", "default_name": "Белковый обмен", "price": 500,
     "includes": "Общий белок, альбумин",
     "description": "Проверка достаточности белка в организме — важного строительного материала для мышц, иммунитета и восстановления после нагрузок."},
    {"id": "joints", "name": "«Здоровье суставов»", "default_name": "Боли в суставах", "price": 1000,
     "includes": "Мочевая кислота, С-реактивный белок (СРБ), ревматоидный фактор (РФ)",
     "description": "Разберитесь в причине боли в суставах. Проверка на подагру и воспалительные ревматические заболевания по трём информативным показателям."},
    {"id": "inflammation", "name": "«Диагностика воспаления»", "default_name": "Воспаления", "price": 500,
     "includes": "С-реактивный белок",
     "description": "Быстрый и точный тест для выявления воспалительного процесса в организме, когда причина недомогания ещё не ясна."},
    {"id": "thyroid", "name": "«Здоровье щитовидной железы»", "default_name": "Щитовидная железа", "price": 3800,
     "includes": "ТТГ, Т3 свободный, Т4 свободный, антитела к тиреоидной пероксидазе, антитела к тиреоглобулину",
     "description": "Полная оценка функции щитовидной железы, включая аутоиммунные маркеры. Помогает выявить как гормональные нарушения, так и аутоиммунный тиреоидит на ранней стадии."},
    {"id": "female_hormones", "name": "«Женское гормональное здоровье»", "default_name": "Женские половые гормоны", "price": 3500,
     "includes": "ФСГ, ЛГ, Эстрадиол, Пролактин, Прогестерон",
     "description": "Полная картина гормонального фона женщины. Важно при планировании беременности, нарушениях цикла, снижении либидо и подготовке к менопаузе."},
    {"id": "male_health", "name": "«Мужское здоровье и сила»", "default_name": "Мужское здоровье", "price": 2500,
     "includes": "Тестостерон, ПСА общий, ПСА свободный",
     "description": "Контроль главного мужского гормона и ранняя диагностика заболеваний предстательной железы. Забота о мужском здоровье и долголетии."},
    {"id": "vitamin_d", "name": "«Витамин D и иммунитет»", "default_name": "Витамины", "price": 2500,
     "includes": "Витамин Д",
     "description": "Проверьте уровень «солнечного витамина», который отвечает за крепкий иммунитет, здоровье костей и хорошее настроение."},
    {"id": "ca125", "name": "«Женский онкоскрининг: яичники и шейка матки»", "default_name": "Онкомаркер СА 125 (яичники и шейка матки)", "price": 1300,
     "includes": "СА 125",
     "description": "Ранняя онконастороженность для женщин. Простой анализ для дополнительного контроля здоровья репродуктивной системы."},
    {"id": "ca153", "name": "«Онкоскрининг молочной железы»", "default_name": "Онкомаркер СА 15-3 (молочная железа)", "price": 1300,
     "includes": "СА 15-3",
     "description": "Дополнительный инструмент контроля здоровья молочной железы. Рекомендуется в комплексе с регулярными профилактическими осмотрами."},
    {"id": "ca199", "name": "«Онкоскрининг ЖКТ и поджелудочной железы»", "default_name": "Онкомаркер СА 19-9 (ЖКТ, поджелудочная железа)", "price": 1300,
     "includes": "СА 19-9",
     "description": "Дополнительная диагностика для контроля здоровья органов пищеварения. Рекомендуется при факторах риска и в рамках регулярного чек-апа."},
    {"id": "cortisol", "name": "«Диагностика стресса»", "default_name": "Стресс - кортизол", "price": 1000,
     "includes": "Кортизол",
     "description": "Узнайте, как стресс влияет на организм. Анализ уровня гормона стресса кортизола — причины бессонницы, тревожности и упадка сил."},
]


# Stable catalog IDs keep these relationships valid even when an administrator
# changes the user-facing names, descriptions, or prices.
EXAMINATION_UPGRADE_PAIRS = {
    "fatigue_basic": "fatigue_extended",
    "weight_basic": "weight_extended",
    "liver_basic": "liver_extended",
}


def normalize_examination_selection(selected_ids) -> list[str]:
    """Make extended complexes replace their corresponding basic complexes."""
    selected = list(dict.fromkeys(str(item) for item in (selected_ids or [])))
    selected_set = set(selected)
    blocked_basics = {
        basic_id for basic_id, extended_id in EXAMINATION_UPGRADE_PAIRS.items()
        if extended_id in selected_set
    }
    return [item_id for item_id in selected if item_id not in blocked_basics]


def recommend_test_ids(profile: dict) -> list[str]:
    result: list[str] = []
    height = profile.get("height_cm") or 0
    weight = profile.get("weight_kg") or 0
    bmi = weight / ((height / 100) ** 2) if height else 0
    if profile.get("fatigue") == "yes":
        result.extend(["fatigue_basic", "iron"])
    if bmi >= 25:
        result.extend(["weight_basic", "lipids"])
    if profile.get("blood_pressure") in {"high", "unstable"}:
        result.append("lipids")
    if profile.get("joint_pain") == "yes":
        result.append("joints")
    if not result:
        gender_fallback = {
            "female": "female_hormones",
            "male": "male_health",
        }.get(profile.get("sex"))
        if gender_fallback:
            result.append(gender_fallback)
    recommendations = list(dict.fromkeys(result))
    # An extended complex covers the same questionnaire indication as its
    # basic counterpart, so both cards should be marked as suitable. This does
    # not preselect either complex and does not change their mutual exclusion.
    for basic_id, extended_id in EXAMINATION_UPGRADE_PAIRS.items():
        if basic_id in recommendations and extended_id not in recommendations:
            basic_index = recommendations.index(basic_id)
            recommendations.insert(basic_index + 1, extended_id)
    return recommendations


def featured_test_ids(profile: dict) -> list[str]:
    """Build the first three catalog positions for the questionnaire scenario.

    With a relevant complaint the order is: direct match, adjacent check-up,
    universal check-up. Without complaints it is: gender check-up and two
    simple universal options. The complete catalog remains available below.
    """
    notes = str(profile.get("notes") or "").strip().lower()
    no_complaint_answers = {
        "нет", "нет жалоб", "жалоб нет", "не беспокоит", "ничего",
    }
    has_free_text_complaint = bool(notes and notes not in no_complaint_answers)
    height = profile.get("height_cm") or 0
    weight = profile.get("weight_kg") or 0
    bmi = weight / ((height / 100) ** 2) if height else 0

    keyword_routes = (
        (("устал", "слабост", "сонлив", "нет сил", "упадок сил"), "fatigue_basic", "iron"),
        (("сустав", "колен", "локт", "скован", "отёк", "отек"), "joints", "inflammation"),
        (("волос", "кож", "сып", "ломк"), "hair_loss", "iron"),
        (("вес", "похуд", "ожир", "набор веса"), "weight_basic", "thyroid"),
        (("серд", "давлен", "пульс", "одыш"), "lipids", "kidneys"),
        (("печен", "печён", "живот", "поджелуд", "желч"), "liver_basic", "liver_extended"),
        (("почк", "моч", "отёк", "отек"), "kidneys", "protein"),
        (("щитовид", "тиреоид"), "thyroid", "vitamin_d"),
        (("стресс", "тревог", "бессон", "сон"), "cortisol", "vitamin_d"),
    )
    direct = adjacent = ""
    for keywords, direct_id, adjacent_id in keyword_routes:
        if any(keyword in notes for keyword in keywords):
            direct, adjacent = direct_id, adjacent_id
            break
    if not direct and profile.get("fatigue") == "yes":
        direct, adjacent = "fatigue_basic", "iron"
    elif not direct and profile.get("joint_pain") == "yes":
        direct, adjacent = "joints", "inflammation"
    elif not direct and bmi >= 25:
        direct, adjacent = "weight_basic", "lipids"
    elif not direct and profile.get("blood_pressure") in {"high", "unstable"}:
        direct, adjacent = "lipids", "kidneys"

    result: list[str] = []
    if direct:
        result.extend((direct, adjacent))
        result.append(next(item for item in ("lipids", "vitamin_d", "iron") if item not in result))
    elif has_free_text_complaint:
        # The complaint cannot be matched safely by a deterministic rule.
        # Keep useful low-threshold options and avoid pretending to diagnose it.
        result.extend(("inflammation", "lipids", "vitamin_d"))
    else:
        gender_test = {"female": "female_hormones", "male": "male_health"}.get(profile.get("sex"))
        if gender_test:
            result.append(gender_test)
        result.extend(("lipids", "vitamin_d"))
    return list(dict.fromkeys(result))[:3]


def examination_recommendation_copy(profile: dict) -> dict:
    featured = featured_test_ids(profile)
    recommendations = set(recommend_test_ids(profile))
    complaint_based = bool(featured and featured[0] in recommendations and featured[0] not in {"female_hormones", "male_health"})
    if complaint_based or str(profile.get("notes") or "").strip().lower() not in {"", "нет", "нет жалоб", "жалоб нет", "не беспокоит", "ничего"}:
        descriptions = {
            "fatigue_basic": "Вы отметили упадок сил или усталость — ниже обследования, которые чаще всего помогают разобраться в возможной причине.",
            "joints": "Вы отметили дискомфорт в суставах — ниже обследования, которые могут помочь уточнить возможные причины боли и воспаления.",
            "hair_loss": "Вы отметили жалобы на волосы или кожу — ниже обследования для проверки частых дефицитов и обменных причин.",
            "weight_basic": "Мы учли данные о весе — ниже обследования, которые помогают оценить возможные обменные и гормональные факторы.",
            "lipids": "Мы учли ваши ответы о давлении или работе сердца — ниже обследования для дополнительной оценки сердечно-сосудистых факторов.",
            "liver_basic": "Вы отметили жалобы со стороны пищеварения — ниже обследования для дополнительной оценки печени и поджелудочной железы.",
            "kidneys": "Вы отметили жалобы, которые могут быть связаны с работой почек — ниже подходящие дополнительные обследования.",
            "thyroid": "Вы отметили возможные признаки изменений работы щитовидной железы — ниже обследования для её дополнительной оценки.",
            "cortisol": "Вы отметили стресс, тревогу или проблемы со сном — ниже обследования, которые могут дополнить оценку состояния.",
        }
        return {
            "title": "Персональные рекомендации по итогам анкеты",
            "description": descriptions.get(
                featured[0] if featured else "",
                "Мы учли ваши ответы — ниже обследования, которые могут помочь уточнить возможные причины жалоб.",
            ),
        }
    return {
        "title": "Дополнительные обследования для вас",
        "description": "Явных жалоб вы не отметили — ниже показаны три обследования, которые часто выбирают в дополнение к медосмотру.",
    }


def effective_examination_price(examination: dict) -> int:
    """Return the actual price; comparison prices are presentation-only."""
    return max(0, int(examination.get("price") or 0))


def public_onboarding(
    state: dict, profile: dict, tests: list[dict] | None = None,
) -> dict:
    catalog = TEST_CATALOG if tests is None else tests
    available_ids = {item["id"] for item in catalog}
    recommended_ids = [
        item for item in recommend_test_ids(profile) if item in available_ids
    ]
    recommended_set = set(recommended_ids)
    public_catalog = []
    for examination in catalog:
        item = dict(examination)
        # The legacy/default name is an integration detail for Bitrix and must
        # not replace or leak into the user-facing catalog.
        item.pop("default_name", None)
        item["effective_price"] = effective_examination_price(item)
        item["discount_applied"] = bool(
            item["id"] in recommended_set
            and int(item.get("price_without_discount") or 0) > item["effective_price"]
        )
        public_catalog.append(item)
    return {
        **state,
        "selected_tests": normalize_examination_selection(state.get("selected_tests", [])),
        "profile": profile,
        "tests": public_catalog,
        "recommended_test_ids": recommended_ids,
        "featured_test_ids": [item for item in featured_test_ids(profile) if item in available_ids],
        "examination_recommendation_copy": examination_recommendation_copy(profile),
    }
