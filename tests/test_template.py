"""Шаблон ZFS: «нет данных» ждёт триггер агента, каждое состояние пула даёт один триггер, языки отличаются только текстом."""
import csv
import re
import unittest
from pathlib import Path

import yaml

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "template"
VERSIONS = {"ru": "linux_zfs.yaml", "en": "linux_zfs_en.yaml"}
AGENT_TRIGGER = "Linux: Zabbix agent is not available"
AGENT_NODATA_TIMEOUT = 30 * 60  # {$AGENT.NODATA_TIMEOUT} шаблона Linux by Zabbix agent active
# Строка AllowKey в конфиге агента: system.run пропускает только ключ, совпавший с ней байт в байт.
ZPOOL_STATUS_KEY = "system.run[/usr/sbin/zpool status -x]"
# spa_state_to_name, OpenZFS 2.2.10 module/zfs/spa_misc.c
POOL_STATES = ["ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "REMOVED", "UNAVAIL",
               "SUSPENDED", "SPLIT", "TRANSITIONING", "UNKNOWN"]


def _seconds(value: str) -> int:
    number, unit = re.fullmatch(r"(\d+)([smhd]?)", value).groups()  # type: ignore[union-attr]
    return int(number) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _template(lang: str) -> dict:
    return yaml.safe_load((TEMPLATE_DIR / VERSIONS[lang]).read_text(encoding="utf-8"))["zabbix_export"]["templates"][0]


def _items(tpl: dict) -> list[tuple[dict, list[dict]]]:
    """(элемент, его триггеры) — и статические, и прототипы LLD."""
    rows = [(item, item.get("triggers", [])) for item in tpl.get("items", [])]
    rows += [(item, item.get("trigger_prototypes", []))
             for rule in tpl.get("discovery_rules", []) for item in rule["item_prototypes"]]
    return rows


def _not_collected_marker(item: dict) -> str | None:
    """Значение, которое шаг «Check for not supported value» пишет вместо ошибки сбора."""
    steps = item.get("preprocessing", [])
    if steps and steps[0]["type"] == "CHECK_NOT_SUPPORTED" and steps[0].get("error_handler") == "CUSTOM_VALUE":
        return steps[0]["error_handler_params"]
    return None


def _zpool_item(tpl: dict) -> dict:
    (item,) = [item for item, _ in _items(tpl) if item["key"] == ZPOOL_STATUS_KEY]
    return item


def _key_params(key: str) -> list[str]:
    return next(csv.reader([key[key.index("[") + 1:-1]]))


def _without_texts(node):
    """Экспорт без переводимых текстов: описаний и имён элементов, правил и триггеров. Имена шаблона, групп и зависимостей остаются."""
    if isinstance(node, dict):
        translated = "key" in node or "priority" in node
        return {k: _without_texts(v) for k, v in node.items()
                if k != "description" and not (k == "name" and translated)}
    if isinstance(node, list):
        return [_without_texts(v) for v in node]
    return node


def _export(lang: str) -> dict:
    return yaml.safe_load((TEMPLATE_DIR / VERSIONS[lang]).read_text(encoding="utf-8"))["zabbix_export"]


class LanguageTests(unittest.TestCase):
    def test_versions_differ_only_in_texts(self):
        # Общие UUID, ключи, группы и выражения: импорт любой версии обновляет установленный шаблон на месте.
        self.assertEqual(_without_texts(_export("ru")), _without_texts(_export("en")))
        self.assertNotEqual(_template("ru")["description"], _template("en")["description"])

    def test_russian_version_names_are_russian(self):
        # Тексты русской версии на русском: имена элементов, правил обнаружения и триггеров.
        names = [n["name"] for item, trigs in _items(_template("ru")) for n in [item, *trigs]]
        names += [rule["name"] for rule in _template("ru")["discovery_rules"]]
        self.assertFalse([n for n in names if not re.search("[а-яё]", n, re.I)])

    def test_english_version_has_no_russian_text(self):
        en = (TEMPLATE_DIR / VERSIONS["en"]).read_text(encoding="utf-8")
        self.assertFalse([line for line in en.splitlines() if re.search("[а-яё]", line, re.I) and "Author:" not in line])


class BehaviourTests(unittest.TestCase):
    def test_nodata_waits_for_agent_trigger(self):
        for lang in VERSIONS:
            with self.subTest(lang=lang):
                tpl = _template(lang)
                window = _seconds({m["macro"]: m["value"] for m in tpl["macros"]}["{$ZFS.NODATA}"])
                nodata = [(item, trig) for item, trigs in _items(tpl) for trig in trigs if trig["expression"].startswith("nodata(")]
                self.assertEqual(len(nodata), 1)
                item, trig = nodata[0]
                self.assertEqual([d["name"] for d in trig.get("dependencies", [])], [AGENT_TRIGGER])
                self.assertGreater(window, AGENT_NODATA_TIMEOUT + _seconds(item["delay"]))
                self.assertFalse([p for p in item.get("preprocessing", []) if p["type"].startswith("DISCARD_UNCHANGED")],
                                 "отброшенный повтор — тоже «нет данных»: nodata открылся бы на здоровом элементе")

    def test_discovery_skips_tryimport_pool(self):
        (rule,) = _template("ru")["discovery_rules"]
        exclude = _key_params(rule["key"])[2]
        self.assertTrue(re.search(exclude, "$import"))
        self.assertFalse(re.search(exclude, "tank"))

    def test_each_pool_state_fires_one_trigger_of_its_severity(self):
        (state,) = [(item, trigs) for item, trigs in _items(_template("ru")) if item["key"].endswith("/state]")]
        rules = []
        for trig in state[1]:
            if "nodata(" in trig["expression"]:
                continue
            op, pattern, expected = re.search(r',#2,"(eq|regexp)","([^"]+)"\)=(\d)$', trig["expression"]).groups()  # type: ignore[union-attr]
            rules.append((op, pattern, int(expected), trig["priority"]))

        def fired(value: str) -> list[str]:
            hits = []
            for op, pattern, expected, priority in rules:
                match = value == pattern if op == "eq" else bool(re.search(pattern, value))
                if (2 if match else 0) == expected:
                    hits.append(priority)
            return hits

        for value in POOL_STATES:
            with self.subTest(state=value):
                self.assertEqual(fired(value), {"ONLINE": [], "DEGRADED": ["AVERAGE"]}.get(value, ["HIGH"]))

    def test_zpool_status_trigger_fires_only_on_errors_of_online_pool(self):
        # -x выводит и DEGRADED/FAULTED пулы (их ведут триггеры состояния), и ONLINE-пулы без ошибок:
        # resilver, ashift, errata. Застрявший на них триггер скрыл бы следующую настоящую ошибку.
        tpl = _template("ru")
        self.assertIn(ZPOOL_STATUS_KEY, [item["key"] for item, _ in _items(tpl)])
        (trig,) = [t for item, trigs in _items(tpl) if item["key"] == ZPOOL_STATUS_KEY for t in trigs
                   if "One or more devices" in t["expression"]]
        needles = re.findall(r',,"like","([^"]+)"\)=1', trig["expression"])
        self.assertEqual(len(needles), trig["expression"].count("find("))
        self.assertEqual(trig["expression"].count(" and "), len(needles) - 1)
        errors = "One or more devices has experienced an unrecoverable error.  An\n\tattempt was made"
        corrupt = "One or more devices has experienced an error resulting in data\n\tcorruption."
        outputs = {
            "all pools are healthy": False,
            "ZFS_NOT_COLLECTED": False,
            f"  pool: p\n state: ONLINE\nstatus: {errors}": True,
            f"  pool: p\n state: ONLINE\nstatus: {corrupt}": True,
            "  pool: p\n state: ONLINE\nstatus: One or more devices is currently being resilvered.": False,
            "  pool: p\n state: ONLINE\nstatus: One or more devices are configured to use a non-native block size.": False,
            f"  pool: p\n state: DEGRADED\nstatus: {errors}": False,
            "  pool: p\n state: FAULTED\nstatus: One or more devices are faulted.": False,
        }
        for output, fires in outputs.items():
            with self.subTest(output=output.splitlines()[-1][:60]):
                self.assertEqual(all(n in output for n in needles), fires)

    def test_every_triggered_item_reports_lost_collection(self):
        # У unsupported-элемента find() и count() уходят в Unknown, и алерта нет. Потерю сбора видно
        # либо по триггеру «нет данных», либо по маркеру из шага «Check for not supported value».
        for item, trigs in _items(_template("ru")):
            if not trigs:
                continue
            with self.subTest(key=item["key"]):
                marker = _not_collected_marker(item)
                self.assertTrue(any(t["expression"].startswith("nodata(") for t in trigs)
                                or (marker is not None and any(f')="{marker}"' in t["expression"] for t in trigs)))

    def test_not_collected_trigger_waits_for_agent_trigger(self):
        item = _zpool_item(_template("ru"))
        marker = _not_collected_marker(item)
        # Значение «ZBX_NOTSUPPORTED» сервер считает ошибкой сбора, и элемент с таким маркером становится unsupported.
        self.assertNotIn(marker, (None, "ZBX_NOTSUPPORTED"))
        (trig,) = [t for t in item["triggers"] if f'"{marker}"' in t["expression"]]
        self.assertEqual([d["name"] for d in trig.get("dependencies", [])], [AGENT_TRIGGER])
        # Повторы маркера отбрасываются, поэтому «держится 10 минут» — это nodata, а не count(#2).
        self.assertIn("nodata(", trig["expression"])

    def test_unexpected_zpool_output_becomes_marker(self):
        # system.run отдаёт вывод, а не код выхода: «zpool: not found» иначе сохранился бы как обычный текст.
        item = _zpool_item(_template("ru"))
        marker = _not_collected_marker(item)
        (check,) = [s for s in item["preprocessing"] if s["type"] == "MATCHES_REGEX"]
        self.assertEqual((check.get("error_handler"), check.get("error_handler_params")), ("CUSTOM_VALUE", marker))
        outputs = {
            "all pools are healthy": True,
            "no pools available": True,
            "  pool: p\n state: ONLINE\nstatus: One or more devices has experienced an unrecoverable error.": True,
            "sh: 1: /usr/sbin/zpool: not found": False,
            "sh: 1: zpool: not found\n  pool: p": False,
            "": False,
        }
        for output, valid in outputs.items():
            with self.subTest(output=output[:40]):
                self.assertEqual(bool(re.search(check["parameters"][0], output, re.M)), valid)  # Zabbix: PCRE2_MULTILINE

    def test_marker_keeps_problems_open(self):
        # Маркер повторно сохраняется heartbeat'ом и после рестарта сервера: без выражения восстановления
        # «не собирается» закрывался бы и открывался заново, а «ошибки на пуле» закрывался бы ложно.
        item = _zpool_item(_template("ru"))
        marker = _not_collected_marker(item)
        for trig in item["triggers"]:
            with self.subTest(trigger=trig["expression"][:40]):
                self.assertEqual(trig.get("recovery_mode"), "RECOVERY_EXPRESSION")
                self.assertIn(f')<>"{marker}"', trig["recovery_expression"])

    def test_no_filesystem_space_items(self):
        self.assertFalse([item["key"] for item, _ in _items(_template("ru")) if item["key"].startswith("vfs.fs.")],
                         "место датасетов уже ведёт Linux by Zabbix agent active")


if __name__ == "__main__":
    unittest.main()
