TEST_CATALOG = [
    {"id": "fatigue_basic", "name": "«Энергия и бодрость» — базовый", "price": 3500,
     "includes": "Ферритин, железо, ТТГ, Витамин Д",
     "description": "Узнайте, почему пропадают силы. Проверка самых частых скрытых причин усталости — дефицита железа, витамина D и нарушений работы щитовидной железы. Быстрый способ понять, откуда «взять энергию»."},
    {"id": "fatigue_extended", "name": "«Энергия и бодрость» — расширенный", "price": 6500,
     "includes": "Ферритин, железо, ТТГ, витамин Д, АЛТ, АСТ, общий белок, кортизол, св Т3, св Т4, С-РБ. Ж: эстрадиол. М: тестостерон",
     "description": "Полное обследование при упадке сил и хронической усталости. Помимо базовых показателей — работа печени, уровень стресса (кортизол), щитовидная железа и половые гормоны. Максимально точный ответ на вопрос «почему я так устал(а)»."},
    {"id": "weight_basic", "name": "«Контроль веса» — базовый", "price": 4000,
     "includes": "ТТГ, св Т3, св Т4, АЛТ, АСТ, триглицериды, ЛПВП, ЛПНП, креатинин",
     "description": "Ищем медицинские причины лишнего веса. Проверка гормонов щитовидной железы, обмена жиров и работы печени и почек — то, что мешает похудеть, даже если вы стараетесь."},
    {"id": "weight_extended", "name": "«Контроль веса» — расширенный", "price": 6500,
     "includes": "ТТГ, св Т3, св Т4, АЛТ, АСТ, триглицериды, ЛПВП, ЛПНП, креатинин, вит Д, кортизол. Ж: тестостерон, эстрадиол. М: тестостерон",
     "description": "Углублённая диагностика причин набора веса: щитовидная железа, обмен жиров, печень, почки, витамин D, гормон стресса кортизол и половые гормоны. Комплексный взгляд на то, что тормозит снижение веса."},
    {"id": "hair_loss", "name": "«Здоровые волосы и кожа»", "price": 3000,
     "includes": "Ферритин, железо, ТТГ, общий белок, тестостерон",
     "description": "Найдите истинную причину выпадения волос. Проверка железа, ферритина, щитовидной железы и гормонального фона — главных факторов, влияющих на густоту и рост волос."},
    {"id": "lipids", "name": "«Здоровье сердца и сосудов»", "price": 1500,
     "includes": "Триглицериды, ЛПВП, ЛПНП",
     "description": "Быстрая проверка «плохого» и «хорошего» холестерина. Оцените риск атеросклероза и сердечно-сосудистых заболеваний всего по трём ключевым показателям."},
    {"id": "liver_basic", "name": "«Здоровье печени и поджелудочной железы» — базовый", "price": 2000,
     "includes": "АЛТ, АСТ, Билирубин общ, Билирубин пр, альфа-амилаза",
     "description": "Контроль работы главных пищеварительных органов. Своевременно выявите нагрузку на печень и поджелудочную железу — до появления симптомов."},
    {"id": "liver_extended", "name": "«Здоровье печени и поджелудочной железы» — расширенный", "price": 2500,
     "includes": "АЛТ, АСТ, Билирубин общ, Билирубин пр, Альфа-амилаза, щелочная фосфатаза, общий белок",
     "description": "Расширенная диагностика печени и поджелудочной железы с дополнительными маркерами застоя желчи и белкового обмена. Для тех, кто хочет полную картину состояния органов пищеварения."},
    {"id": "iron", "name": "«Профилактика анемии»", "price": 1000,
     "includes": "Общий железо, ферритин",
     "description": "Простой способ проверить, хватает ли организму железа. Быстрая диагностика скрытого дефицита железа — частой причины слабости, бледности и снижения работоспособности."},
    {"id": "kidneys", "name": "«Здоровье почек»", "price": 1000,
     "includes": "Общий белок, мочевина, креатинин",
     "description": "Оцените, как работают ваши почки. Базовый набор показателей для раннего выявления нарушений почечной функции."},
    {"id": "protein", "name": "«Баланс белка»", "price": 500,
     "includes": "Общий белок, альбумин",
     "description": "Проверка достаточности белка в организме — важного строительного материала для мышц, иммунитета и восстановления после нагрузок."},
    {"id": "joints", "name": "«Здоровье суставов»", "price": 1000,
     "includes": "Мочевая кислота, С-реактивный белок (СРБ), ревматоидный фактор (РФ)",
     "description": "Разберитесь в причине боли в суставах. Проверка на подагру и воспалительные ревматические заболевания по трём информативным показателям."},
    {"id": "inflammation", "name": "«Диагностика воспаления»", "price": 500,
     "includes": "С-реактивный белок",
     "description": "Быстрый и точный тест для выявления воспалительного процесса в организме, когда причина недомогания ещё не ясна."},
    {"id": "thyroid", "name": "«Здоровье щитовидной железы»", "price": 3800,
     "includes": "ТТГ, Т3 свободный, Т4 свободный, антитела к тиреоидной пероксидазе, антитела к тиреоглобулину",
     "description": "Полная оценка функции щитовидной железы, включая аутоиммунные маркеры. Помогает выявить как гормональные нарушения, так и аутоиммунный тиреоидит на ранней стадии."},
    {"id": "female_hormones", "name": "«Женское гормональное здоровье»", "price": 3500,
     "includes": "ФСГ, ЛГ, Эстрадиол, Пролактин, Прогестерон",
     "description": "Полная картина гормонального фона женщины. Важно при планировании беременности, нарушениях цикла, снижении либидо и подготовке к менопаузе."},
    {"id": "male_health", "name": "«Мужское здоровье и сила»", "price": 2500,
     "includes": "Тестостерон, ПСА общий, ПСА свободный",
     "description": "Контроль главного мужского гормона и ранняя диагностика заболеваний предстательной железы. Забота о мужском здоровье и долголетии."},
    {"id": "vitamin_d", "name": "«Витамин D и иммунитет»", "price": 2500,
     "includes": "Витамин Д",
     "description": "Проверьте уровень «солнечного витамина», который отвечает за крепкий иммунитет, здоровье костей и хорошее настроение."},
    {"id": "ca125", "name": "«Женский онкоскрининг: яичники и шейка матки»", "price": 1300,
     "includes": "СА 125",
     "description": "Ранняя онконастороженность для женщин. Простой анализ для дополнительного контроля здоровья репродуктивной системы."},
    {"id": "ca153", "name": "«Онкоскрининг молочной железы»", "price": 1300,
     "includes": "СА 15-3",
     "description": "Дополнительный инструмент контроля здоровья молочной железы. Рекомендуется в комплексе с регулярными профилактическими осмотрами."},
    {"id": "ca199", "name": "«Онкоскрининг ЖКТ и поджелудочной железы»", "price": 1300,
     "includes": "СА 19-9",
     "description": "Дополнительная диагностика для контроля здоровья органов пищеварения. Рекомендуется при факторах риска и в рамках регулярного чек-апа."},
    {"id": "cortisol", "name": "«Диагностика стресса»", "price": 1000,
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
    }
