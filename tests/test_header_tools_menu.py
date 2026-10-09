"""Фронтенд-контрактный тест выпадающего меню Header «Больше инструментов».

Когда в верхней панели слишком много иконок, низкочастотные / относящиеся к
рабочей области пункты убираются в выпадающее меню headerToolsMenu.
Эти проверки фиксируют: структура меню существует, id убранных кнопок не
потеряны (иначе JS handler не привяжется), обе версии index.html совпадают,
тексты меню i18n эквивалентны в двух языках.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGED_INDEX = ROOT / "claude_web" / "static" / "index.html"
ROOT_INDEX = ROOT / "static" / "index.html"
RU = ROOT / "claude_web" / "static" / "i18n" / "ru.json"
EN = ROOT / "claude_web" / "static" / "i18n" / "en.json"

# Кнопки, убранные в меню «Больше инструментов» —— id должны сохраниться,
# frontend handler привязывается по id
MENU_ITEM_IDS = [
    "cwProjectMapBtn",
    "cwOpenCodeFileBtn",
    "roundtableBtn",
    "scheduledTasksBtn",
    "configCenterBtn",
    "headerMoreBtn",
]
# Высокочастотные кнопки, остающиеся на верхней панели
EXPOSED_IDS = ["connectorBtn", "agentTemplateBtn", "helpBtn", "exportBtn"]


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        nk = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, nk))
        else:
            out[nk] = v
    return out


def _menu_block(html):
    """Точно извлекает область содержимого headerToolsMenu (до совпадающего
    закрывающего тега).

    Использует сопоставление глубины вложенности <div>, а не фиксированное
    окно, чтобы не захватить внешние кнопки после меню.
    """
    open_tag = 'id="headerToolsMenu"'
    start = html.index(open_tag)
    # Отступаем к '<' этого div
    div_start = html.rindex("<", 0, start)
    depth = 0
    i = div_start
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
            i += 4
        elif html.startswith("</div>", i):
            depth -= 1
            i += 6
            if depth == 0:
                return html[div_start:i]
        else:
            i += 1
    raise AssertionError("закрывающий </div> для headerToolsMenu не найден")


class HeaderToolsMenuTest(unittest.TestCase):
    def setUp(self):
        self.html = PACKAGED_INDEX.read_text(encoding="utf-8")

    def test_menu_trigger_and_container_exist(self):
        self.assertEqual(self.html.count('id="headerToolsBtn"'), 1)
        self.assertEqual(self.html.count('id="headerToolsMenu"'), 1)
        # Меню по умолчанию свёрнуто и является доступным меню (role=menu)
        self.assertIn('id="headerToolsMenu" class="cw-header-tools-menu hidden" role="menu"', self.html)
        self.assertIn('aria-haspopup="menu"', self.html)

    def test_menu_items_keep_original_ids(self):
        # id каждой убранной кнопки уникален глобально (привязка handler без неоднозначности/потерь)
        for item_id in MENU_ITEM_IDS:
            self.assertEqual(
                self.html.count(f'id="{item_id}"'), 1, f"{item_id} должен встречаться ровно один раз"
            )

    def test_menu_items_live_inside_menu(self):
        menu_block = _menu_block(self.html)
        for item_id in MENU_ITEM_IDS:
            self.assertIn(f'id="{item_id}"', menu_block, f"{item_id} должен находиться внутри контейнера меню")

    def test_exposed_buttons_stay_out_of_menu(self):
        menu_block = _menu_block(self.html)
        for exposed_id in EXPOSED_IDS:
            self.assertNotIn(
                f'id="{exposed_id}"', menu_block, f"{exposed_id} должен быть снаружи и не попадать в меню"
            )

    def test_workspace_group_is_code_only(self):
        # Группа рабочей области помечена cw-code-only, в режиме Chat вся группа скрыта
        self.assertIn('<div class="cw-more-tool-group cw-code-only">', self.html)

    def test_two_index_html_identical(self):
        self.assertEqual(
            PACKAGED_INDEX.read_text(encoding="utf-8"),
            ROOT_INDEX.read_text(encoding="utf-8"),
            "packaged и root index.html должны совпадать побайтово",
        )

    def test_menu_i18n_keys_resolve_both_langs(self):
        ru = _flatten(json.loads(RU.read_text(encoding="utf-8")))
        en = _flatten(json.loads(EN.read_text(encoding="utf-8")))
        menu_block = _menu_block(self.html)
        keys = set(re.findall(r'data-i18n="([^"]+)"', menu_block))
        self.assertIn("html.tools_group_workspace", keys)
        for key in keys:
            self.assertIn(key, ru, f"{key} отсутствует перевод ru")
            self.assertIn(key, en, f"{key} отсутствует перевод en")


if __name__ == "__main__":
    unittest.main()
