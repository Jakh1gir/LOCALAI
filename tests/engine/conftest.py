from __future__ import annotations

import pytest

from engine import Actor, Database, open_database, structure

ENGINEER = Actor("11111111-1111-4111-8111-111111111111", "Инженер Петров")


@pytest.fixture
def db(tmp_path) -> Database:
    database = open_database(str(tmp_path / "localai.sqlite3"))
    yield database
    database.close()


@pytest.fixture
def plant(db):
    """Площадка, справочник, тип «Оборудование» и реестр — общий стенд тестов."""
    with db.write(ENGINEER) as tx:
        site = structure.create_site(tx, "ТЭЦ-1")
        makers = structure.create_dictionary(tx, "Изготовитель")
        siemens = structure.add_dictionary_value(tx, makers["id"], "Siemens", ["Сименс"])
        kind = structure.create_type(tx, "Оборудование")
        add = lambda name, k, **kw: structure.add_field(tx, kind["id"], name, k, **kw)  # noqa: E731
        fields = {
            "kks": add("ККС", "kks", required=True),
            "name": add("Наименование", "text", sortable=True),
            "power": add("Мощность, кВт", "number", filterable=True, sortable=True),
            "maker": add("Изготовитель", "choice", dictionary_id=makers["id"],
                         input_mode="suggest", filterable=True),
            "checked": add("Дата проверки", "date", filterable=True),
            "spare": add("Резерв", "boolean"),
            "tags": add("Метки", "text", multiple=True),
        }  # fmt: skip
        dataset = structure.create_dataset(tx, site["id"], kind["id"], "Эксплуатация")
    keys = {name: field["key"] for name, field in fields.items()}
    return {
        "site": site, "type": kind, "dataset": dataset, "fields": fields, "keys": keys,
        "makers": makers, "siemens": siemens,
    }  # fmt: skip
