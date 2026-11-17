"""Слой запросов: фильтры, поиск, сортировка, курсор, индексы (API-03…05, DATA-03)."""

from __future__ import annotations

import pytest

from engine import EngineError, query, records, structure

from .conftest import ENGINEER

ROWS = [
    ("10LAC10AP001", "Насос питательный ПЭН-1", 500, "Siemens", "2024-03-15", ["резерв"]),
    ("10LAC20AP001", "Насос питательный ПЭН-2", 500, "ABB", "2024-05-01", []),
    ("10HFB10AN001", "Вентилятор дутьевой", 250.5, "Сименс", None, ["осмотр", "резерв"]),
    ("10BBA01", "Ячейка 6 кВ", None, None, "2023-12-31", []),
    ("10MAA10", "Турбина ёлочная", 60000, "ABB", None, []),
]


@pytest.fixture
def filled(db, plant):
    keys = plant["keys"]
    rows = [
        {keys["kks"]: kks, keys["name"]: name, keys["power"]: power, keys["maker"]: maker,
         keys["checked"]: checked, keys["tags"]: tags}
        for kks, name, power, maker, checked, tags in ROWS
    ]  # fmt: skip
    with db.write(ENGINEER) as tx:
        records.create_many(tx, plant["dataset"]["id"], rows)
    return plant


def _names(db, plant, request):
    with db.read() as conn:
        page = query.list_records(conn, plant["dataset"]["id"], request)
    return [item["doc"].get(plant["keys"]["name"]) for item in page["items"]]


def _where(plant, name, op, value=None):
    return {"where": [{"field": plant["keys"][name], "op": op, "value": value}]}


@pytest.mark.parametrize(
    ("name", "op", "value", "expected"),
    [
        ("power", "eq", 500, 2),
        ("power", "gte", "250,5", 4),
        ("power", "empty", None, 1),
        ("power", "ne", 500, 3),
        ("power", "in", [250.5, 60000], 2),
        ("checked", "lt", "01.01.2024", 1),
        ("name", "contains", "НАСОС", 2),
        ("name", "starts", "турбина", 1),
        ("maker", "eq", "сименс", 2),  # синоним справочника
        ("maker", "in", ["ABB", "нет такого"], 2),
        ("maker", "contains", "sie", 2),
        ("maker", "ne", "ABB", 3),
        ("kks", "eq", "10lac10ap001", 1),
        ("kks", "starts", "10LAC", 2),
        ("kks", "eq", "НЕТ-ТАКОГО", 0),
        ("tags", "eq", "резерв", 2),
        ("tags", "ne", "резерв", 3),
        ("tags", "contains", "осм", 1),
    ],
)
def test_filters(db, filled, name, op, value, expected):
    request = _where(filled, name, op, value)
    assert len(_names(db, filled, request)) == expected
    with db.read() as conn:
        assert query.count_records(conn, filled["dataset"]["id"], request) == expected


def test_any_group_and_combined_conditions(db, filled):
    keys = filled["keys"]
    request = {
        "where": [
            {"any": [
                {"field": keys["power"], "op": "gt", "value": 1000},
                {"field": keys["maker"], "op": "eq", "value": "Siemens"},
            ]},
            {"field": keys["checked"], "op": "empty"},
        ]
    }  # fmt: skip
    assert sorted(_names(db, filled, request)) == ["Вентилятор дутьевой", "Турбина ёлочная"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("питательный", 2),
        ("ПЭН-1", 1),
        ("насос siemens", 1),  # слова ищутся в разных полях
        ("lac10", 1),  # часть кода ККС
        ("елочная", 1),  # «ё» приравнена к «е»
        ("15.03.2024", 1),
        ("6 кв", 1),  # короткие слова ищутся без индекса
        ("ab", 2),  # два символа: название из справочника
        ("нигде", 0),
    ],
)
def test_search(db, filled, text, expected):
    assert len(_names(db, filled, {"search": text})) == expected


def test_search_follows_renames(db, filled):
    with db.read() as conn:
        abb = next(
            value for value in structure.list_dictionary_values(conn, filled["makers"]["id"])
            if value["value"] == "ABB"
        )  # fmt: skip
    with db.write(ENGINEER) as tx:
        structure.update_dictionary_value(tx, abb["id"], value="Hitachi Energy")
    assert len(_names(db, filled, {"search": "hitachi"})) == 2
    assert _names(db, filled, {"search": "abb"}) == []


def test_sort_with_nulls_and_cursor_pages(db, filled):
    keys = filled["keys"]
    request = {"sort": [{"field": keys["power"], "dir": "asc"}], "limit": 2}
    seen, cursor = [], None
    with db.read() as conn:
        while True:
            page = query.list_records(conn, filled["dataset"]["id"], {**request, "cursor": cursor})
            seen.extend(item["doc"].get(keys["power"]) for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert seen == [None, 250.5, 500, 500, 60000]  # пустые значения идут первыми
    request["sort"][0]["dir"] = "desc"
    request["limit"] = 200
    assert _names(db, filled, request)[0] == "Турбина ёлочная"
    assert _names(db, filled, request)[-1] == "Ячейка 6 кВ"


def test_cursor_is_bound_to_sort(db, filled):
    keys = filled["keys"]
    with db.read() as conn:
        page = query.list_records(conn, filled["dataset"]["id"], {"limit": 2})
        with pytest.raises(EngineError) as caught:
            query.list_records(
                conn, filled["dataset"]["id"],
                {"limit": 2, "cursor": page["next_cursor"], "sort": {"field": keys["name"]}},
            )  # fmt: skip
    assert caught.value.code == "query_invalid"


def test_sort_by_dictionary_name_and_refs(db, filled):
    keys = filled["keys"]
    with db.read() as conn:
        page = query.list_records(
            conn, filled["dataset"]["id"], {"sort": {"field": keys["maker"], "dir": "desc"}}
        )
    first = page["items"][0]["doc"]
    assert page["refs"]["choices"][first[keys["maker"]]] == "Siemens"
    assert page["refs"]["kks"][first[keys["kks"]]] in {"10LAC10AP001", "10HFB10AN001"}


def test_archived_records_are_hidden_by_default(db, filled):
    with db.read() as conn:
        record = query.list_records(conn, filled["dataset"]["id"], {"limit": 1})["items"][0]
    with db.write(ENGINEER) as tx:
        records.set_archived(tx, record["id"], True, 1)
    assert len(_names(db, filled, {})) == 4
    assert len(_names(db, filled, {"archived": True})) == 1
    assert len(_names(db, filled, {"archived": "any"})) == 5


def test_filter_and_sort_use_expression_index(db, filled):
    keys = filled["keys"]
    expr = f"json_extract(doc, '$.{keys['power']}')"
    base = "SELECT seq FROM records WHERE dataset_seq = ? AND archived_at IS NULL"
    plans = {}
    with db.read() as conn:
        dataset_seq = structure.load_dataset(conn, filled["dataset"]["id"])["seq"]
        for name, tail, params in (
            ("filter", f" AND {expr} = ?", [500]),
            ("sort", f" ORDER BY {expr}, seq LIMIT 50", []),
            ("page", f" AND ({expr} >= ? AND ({expr} > ? OR seq > ?)) ORDER BY {expr}, seq LIMIT 50",
             [500, 500, 1]),
        ):  # fmt: skip
            rows = conn.execute(
                f"EXPLAIN QUERY PLAN {base}{tail}", [dataset_seq, *params]
            ).fetchall()
            plans[name] = " ".join(row["detail"] for row in rows)
    index = structure.index_name(keys["power"])
    assert f"{index} (dataset_seq=? AND <expr>=?)" in plans["filter"]
    assert index in plans["sort"] and "TEMP B-TREE" not in plans["sort"]  # без досортировки
    assert f"{index} (dataset_seq=? AND <expr>>?)" in plans["page"]  # курсор встаёт по индексу
    assert "TEMP B-TREE" not in plans["page"]
    with db.write(ENGINEER) as tx:
        structure.update_field(tx, filled["fields"]["power"]["id"], filterable=False, sortable=False)
    with db.read() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
            (structure.index_name(keys["power"]),),
        ).fetchone()[0]
    assert left == 0


@pytest.mark.parametrize(
    "request_body",
    [
        {"where": [{"field": "нет", "op": "eq", "value": 1}]},
        {"where": [{"field": "$id", "op": "похоже", "value": 1}]},
        {"limit": 201},
        {"sort": [{"field": "$id"}, {"field": "$version"}]},
        {"cursor": "мусор"},
        {"лишнее": 1},
    ],
)
def test_bad_queries_are_rejected(db, filled, request_body):
    with db.read() as conn:
        with pytest.raises(EngineError) as caught:
            query.list_records(conn, filled["dataset"]["id"], request_body)
    assert caught.value.code == "query_invalid"
