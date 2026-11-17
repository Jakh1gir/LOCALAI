"""Создание синтетической базы для разработки и замеров LOCALAI."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from typing import Any

from engine import Actor, EngineError, Tx, open_database, records, structure

BATCH_SIZE = 1000
ACTOR = Actor(None, "Генератор тестовых данных", "system")
INSPECTION_START = date(2025, 1, 1)


@dataclass(frozen=True)
class Equipment:
    category: str
    kind: str
    unit: str
    names: tuple[str, ...]
    model_series: tuple[str, ...]
    ranges: tuple[str, ...]


EQUIPMENT = (
    Equipment(
        "Насосное оборудование", "Центробежный насос", "м³/ч",
        ("Насос питательный ПЭН-1", "Насос питательный ПЭН-2", "Насос конденсатный",
         "Насос сетевой"),
        ("ПЭН", "Кс"),
        ("0–100 м³/ч", "0–250 м³/ч", "0–400 м³/ч", "0–630 м³/ч"),
    ),
    Equipment(
        "Измерительные приборы", "Датчик давления", "МПа",
        ("Датчик давления пара", "Датчик давления воды", "Датчик перепада давления",
         "Преобразователь разрежения"),
        ("Метран", "ДМ"),
        ("0–0,16 МПа", "0–0,6 МПа", "0–1,6 МПа", "0–2,5 МПа"),
    ),
    Equipment(
        "Электрооборудование", "Асинхронный двигатель", "кВт",
        ("Двигатель дымососа", "Двигатель насоса", "Двигатель вентилятора",
         "Двигатель питателя"),
        ("АИР", "АД"),
        ("0–7,5 кВт", "0–22 кВт", "0–55 кВт", "0–110 кВт"),
    ),
    Equipment(
        "Трубопроводная арматура", "Запорная арматура", "мм",
        ("Задвижка питательной воды", "Клапан рециркуляции", "Клапан обратный",
         "Затвор поворотный"),
        ("ЗК", "КР"),
        ("Ду 50–80 мм", "Ду 80–125 мм", "Ду 125–200 мм", "Ду 200–300 мм"),
    ),
    Equipment(
        "АСУ ТП", "Программируемый контроллер", "В",
        ("Контроллер котла", "Контроллер насосной станции", "Контроллер вентиляции",
         "Контроллер шкафа ввода-вывода"),
        ("ПЛК", "S7"),
        ("0–5 В", "0–10 В", "0–24 В", "0–48 В"),
    ),
    Equipment(
        "Вентиляционное оборудование", "Центробежный вентилятор", "Па",
        ("Вентилятор дутьевой", "Дымосос котла", "Вентилятор приточный", "Вентилятор вытяжной"),
        ("ВДН", "ДН"),
        ("0–500 Па", "0–1000 Па", "0–2500 Па", "0–5000 Па"),
    ),
)

MAKERS = {
    "Siemens": ["Сименс"],
    "ABB": ["АББ"],
    "Schneider Electric": ["Шнайдер Электрик", "Schneider"],
    "WIKA": ["ВИКА"],
    "Emerson": ["Эмерсон"],
    "Метран": ["Metran"],
    "ОВЕН": ["OWEN"],
    "ЭМИС": ["EMIS"],
    "Danfoss": ["Данфосс"],
    "KSB": ["КСБ"],
    "Grundfos": ["Грундфос"],
    "Промприбор": ["Prompribor"],
}
MAKER_NAMES = tuple(MAKERS)
PURPOSES = ("Основное оборудование", "Горячий резерв", "Холодный резерв", "Вспомогательный контур")
STATES = (
    "Исправно", "На контроле", "На обслуживании", "В ремонте", "Законсервировано", "На складе",
)
INSTALLATIONS = tuple(
    f"{building}, отметка {level:+d} м, позиция {position:02d}"
    for building in (
        "Котельный цех", "Турбинный цех", "Насосная станция", "Щит управления",
    )
    for level in (0, 6, 12, 18)
    for position in range(1, 26)
)
STORAGE = tuple(
    f"Склад {warehouse}, стеллаж {shelf:02d}"
    for warehouse in range(1, 6)
    for shelf in range(1, 9)
)
DOCUMENTS = tuple(
    f"{kind} № {year}-{number:03d}"
    for kind in ("Паспорт", "Акт осмотра", "Протокол испытаний", "Ведомость обслуживания")
    for year in (2024, 2025)
    for number in range(1, 26)
)
NOTES = (
    "Проверить пломбы, маркировку и комплектность при очередном осмотре.",
    "Результаты осмотра внести в журнал участка после согласования с мастером смены.",
    "Сверить фактическое положение с исполнительной схемой перед испытанием.",
    "При следующей проверке дополнительно осмотреть крепления и состояние клеммной коробки.",
    "После обслуживания проверить под нагрузкой и зафиксировать показания в протоколе.",
)
KKS_GROUPS = (
    ("LAC", 10, "AP"),
    ("HFB", 1, "AN"),
    ("BBA", 1, "GS"),
    ("MKA", 1, "CT"),
    ("LBA", 10, "AA"),
    ("PGB", 2, "CF"),
)


def _create_structure(tx: Tx) -> tuple[str, str]:
    site = structure.create_site(tx, "ТЭЦ-1")
    kind = structure.create_type(tx, "Оборудование")
    type_id = kind["id"]
    structure.add_field(tx, type_id, "ККС", "kks", key="kks", multiple=True, required=True)

    choices = (
        ("category", "Категория", tuple(item.category for item in EQUIPMENT)),
        ("name", "Наименование", tuple(name for item in EQUIPMENT for name in item.names)),
        ("model", "Марка", tuple(
            f"{series}-{grade}"
            for item in EQUIPMENT
            for series in item.model_series
            for grade in range(1, 21)
        )),
        ("manufacturer", "Производитель", MAKER_NAMES),
        ("purpose", "Назначение", PURPOSES),
        ("equipment_type", "Тип", tuple(item.kind for item in EQUIPMENT)),
        ("range", "Диапазон", tuple(value for item in EQUIPMENT for value in item.ranges)),
        ("installation", "Место установки", INSTALLATIONS),
        ("state", "Состояние", STATES),
        ("unit", "Единица измерения", tuple(item.unit for item in EQUIPMENT)),
        ("storage", "Место хранения", STORAGE),
        ("document", "Документ или акт", DOCUMENTS),
    )
    for key, label, values in choices:
        dictionary = structure.create_dictionary(tx, label)
        for value in values:
            synonyms = MAKERS[value] if key == "manufacturer" else None
            structure.add_dictionary_value(tx, dictionary["id"], value, synonyms)
        structure.add_field(
            tx, type_id, label, "choice", key=key, dictionary_id=dictionary["id"],
            input_mode="strict", filterable=key in {"category", "manufacturer", "state"},
        )

    for key, label in (
        ("quantity", "Количество"),
        ("reserve", "В резерве"),
        ("broken", "Неисправно"),
        ("required", "Требуется"),
    ):
        structure.add_field(
            tx, type_id, label, "integer", key=key, filterable=True, sortable=True
        )
    for key, label in (
        ("last_inspection", "Последняя проверка"),
        ("next_inspection", "Следующая проверка"),
    ):
        structure.add_field(
            tx, type_id, label, "date", key=key, filterable=True, sortable=True
        )
    structure.add_field(
        tx, type_id, "Серийный номер", "text", key="serial",
        required=True, filterable=True, sortable=True,
    )
    structure.add_field(tx, type_id, "Примечание", "text", key="note")
    operating = structure.create_dataset(tx, site["id"], type_id, "В эксплуатации")
    warehouse = structure.create_dataset(tx, site["id"], type_id, "Склад")
    return operating["id"], warehouse["id"]


def _kks_code(index: int) -> str:
    number, group_index = divmod(index, len(KKS_GROUPS))
    group, start, kind = KKS_GROUPS[group_index]
    block, number = divmod(number, 99 * 999)
    system, position = divmod(number, 999)
    system = (system + start - 1) % 99 + 1
    return f"{(block + 1) * 10:02d}{group}{system:02d}{kind}{position + 1:03d}"


def _record_values(rng: Random, index: int, codes: list[str], note: bool) -> dict[str, Any]:
    equipment = rng.choice(EQUIPMENT)
    model = f"{rng.choice(equipment.model_series)}-{rng.randint(1, 20)}"
    quantity = rng.randint(1, 30)
    reserve = rng.randint(0, min(quantity, 5))
    last = INSPECTION_START + timedelta(days=rng.randrange(730))
    interval = rng.choice((180, 365))
    values = {
        "kks": codes,
        "category": equipment.category,
        "name": rng.choice(equipment.names),
        "model": model,
        "manufacturer": rng.choice(MAKER_NAMES),
        "purpose": rng.choice(PURPOSES),
        "equipment_type": equipment.kind,
        "range": rng.choice(equipment.ranges),
        "installation": rng.choice(INSTALLATIONS),
        "state": rng.choice(STATES),
        "unit": equipment.unit,
        "storage": rng.choice(STORAGE),
        "document": rng.choice(DOCUMENTS),
        "quantity": quantity,
        "reserve": reserve,
        "broken": rng.randint(0, min(quantity - reserve, 4)),
        "required": rng.randint(0, 8),
        "last_inspection": last.isoformat(),
        "next_inspection": (last + timedelta(days=interval)).isoformat(),
        "serial": f"СН-{index + 1:07d}",
    }
    if note:
        values["note"] = rng.choice(NOTES)
    return values


def _add_history(
    tx: Tx, rng: Random, record_id: str, values: dict[str, Any], archived: bool
) -> None:
    state = STATES.index(values["state"])
    maker = MAKER_NAMES.index(values["manufacturer"])
    edit_count = rng.randint(1, 2 if archived else 3)
    version = 1
    for edit in range(1, edit_count + 1):
        state = (state + rng.randrange(1, len(STATES))) % len(STATES)
        changes = {"state": STATES[state], "quantity": values["quantity"] + edit}
        if rng.randrange(4) == 0:
            maker = (maker + rng.randrange(1, len(MAKER_NAMES))) % len(MAKER_NAMES)
            changes["manufacturer"] = MAKER_NAMES[maker]
        updated = records.update(tx, record_id, changes, expected_version=version)
        version = updated["version"]
    if archived:
        records.set_archived(tx, record_id, True, expected_version=version)


def _fill_dataset(
    tx: Tx, rng: Random, dataset_id: str, start: int, stop: int, record_count: int
) -> None:
    for offset in range(start, stop, BATCH_SIZE):
        indices = range(offset, min(offset + BATCH_SIZE, stop))
        count = len(indices)
        second_codes = set(rng.sample(indices, count * 15 // 100))
        notes = set(rng.sample(indices, count * 30 // 100))
        histories = rng.sample(indices, count * 40 // 100)
        archived = set(rng.sample(histories, count * 3 // 100))
        rows = []
        for index in indices:
            # Первый ККС склада совпадает с последним ККС эксплуатации.
            primary = index - 1 if index == start and start > 0 else index
            if primary > 0 and primary % 200 == 0:
                primary -= 1
            codes = [_kks_code(primary)]
            if index in second_codes:
                codes.append(_kks_code(record_count + index))
            rows.append(_record_values(rng, index, codes, index in notes))

        ids = records.create_many(tx, dataset_id, rows)
        for index in histories:
            position = index - offset
            _add_history(tx, rng, ids[position], rows[position], index in archived)


def _require_new_output(out: Path) -> None:
    if out.exists() or out.is_symlink():
        raise FileExistsError(out)
    for suffix in ("-wal", "-shm", "-journal"):
        path = out.with_name(out.name + suffix)
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)


def _publish(database_path: Path, out: Path) -> None:
    wal = database_path.with_name(database_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise OSError("После закрытия временной базы остался непустой журнал SQLite.")
    _require_new_output(out)
    with database_path.open("rb") as source:
        # Эксклюзивное создание защищает файл и при одновременном запуске.
        target = out.open("xb")
        try:
            with target:
                shutil.copyfileobj(source, target)
        except BaseException:
            out.unlink(missing_ok=True)
            raise


def generate_database(out: Path, record_count: int, seed: int) -> None:
    """Генерирует базу по заданному seed и создаёт новый выходной файл."""
    if record_count < 1:
        raise ValueError("Число записей должно быть положительным.")
    _require_new_output(out)
    rng = Random(seed)
    split = record_count - record_count // 5

    with TemporaryDirectory(prefix="localai-generate-", dir=out.parent) as work:
        database_path = Path(work) / "data.sqlite3"
        db = open_database(str(database_path))
        try:
            with db.write(ACTOR) as tx:
                operating, warehouse = _create_structure(tx)
                _fill_dataset(tx, rng, operating, 0, split, record_count)
                _fill_dataset(tx, rng, warehouse, split, record_count, record_count)
        finally:
            db.close()
        _publish(database_path, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.generate", description="Создать синтетическую базу LOCALAI."
    )
    parser.add_argument("--records", type=int, required=True, help="число записей")
    parser.add_argument("--out", type=Path, required=True, help="путь к новому файлу SQLite")
    parser.add_argument(
        "--seed", type=int, default=1, help="начальное значение генератора, по умолчанию 1"
    )
    args = parser.parse_args(argv)

    if args.records < 1:
        parser.error("--records должно быть положительным целым числом")
    try:
        generate_database(args.out, args.records, args.seed)
    except FileExistsError as error:
        path = error.filename or error.args[0]
        print(f"Файл «{path}» уже существует. Укажите новый путь в --out.", file=sys.stderr)
        return 2
    except (EngineError, OSError, sqlite3.Error) as error:
        print(f"Не удалось создать базу «{args.out}». {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Генерация прервана.", file=sys.stderr)
        return 130
    count = f"{args.records:,}".replace(",", " ")
    print(f"Создана база «{args.out}». Записей {count}, seed {args.seed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
