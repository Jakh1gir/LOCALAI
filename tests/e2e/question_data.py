"""Малый стенд по модели C-104 для тестов вопросов, не генератор нагрузки C-104."""

from contextlib import closing
from pathlib import Path

from engine import SYSTEM, open_database, records, structure


def add_signals(path: Path) -> None:
    with closing(open_database(str(path))) as db, db.write(SYSTEM) as tx:
        sites = structure.list_sites(tx.conn)
        site = sites[0] if sites else structure.create_site(tx, "Стенд вопросов")
        cable = structure.create_type(tx, "Кабель")
        structure.add_field(
            tx, cable["id"], "Номер кабеля", "text", key="cable_number", unique=True
        )
        structure.add_field(
            tx, cable["id"], "Откуда", "kks", key="source_kks", kks_role="route_start"
        )
        structure.add_field(tx, cable["id"], "Куда", "kks", key="target_kks", kks_role="route_end")
        signal = structure.create_type(tx, "Сигнал")
        fields = (
            ("Код сигнала", "text", "signal_code", {"unique": True}),
            ("Аппарат", "kks", "device_kks", {"kks_role": "object", "required": True}),
            ("Тип сигнала", "text", "signal_kind", {}),
            ("Кабель", "record", "cable_record", {"target_type_id": cable["id"]}),
            ("Шкаф ПТК", "kks", "ptk", {"kks_role": "route_end"}),
            ("Клеммы ПТК", "text", "contacts", {"contact_for": "ptk"}),
            ("Шкаф I/O", "kks", "io", {"kks_role": "related"}),
        )
        for name, kind, key, options in fields:
            structure.add_field(tx, signal["id"], name, kind, key=key, **options)
        cables = structure.create_dataset(tx, site["id"], cable["id"], "Кабели")
        signals = structure.create_dataset(tx, site["id"], signal["id"], "Сигналы")
        for number in range(1, 4):
            device = f"10LAC10AP{number:03d}"
            cable_number = f"К-{number:04d}"
            records.create(
                tx,
                cables["id"],
                {"cable_number": cable_number, "source_kks": device, "target_kks": "00CVA01GH001"},
            )
            for suffix, kind in (("XB01", "DI"), ("XB62", "DO"), ("XQ01", "AI")):
                records.create(
                    tx,
                    signals["id"],
                    {
                        "signal_code": device + suffix,
                        "device_kks": device,
                        "signal_kind": kind,
                        "cable_record": cable_number,
                        "ptk": "00CVA01GH001",
                        "contacts": f"{number}:1-2",
                        "io": "00CVA01GH002",
                    },
                )
