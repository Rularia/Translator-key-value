from __future__ import annotations

import fnmatch
import hashlib
import json
import queue
import re
import threading
from pathlib import Path
from typing import Any
from urllib import error, request

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut, QTextDocument, QTextOption, QAbstractTextDocumentLayout
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QSplitter,
    QStatusBar,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from translator_tool.json_mapper import (
    MODE_AUTO,
    MODE_EQUALS,
    MODE_JSON_VALUE,
    MODE_XML,
    apply_translations,
    available_source_modes,
    classify_text,
    entries_to_dict_rows,
    extract_translation_entries,
    load_source_file,
    rows_to_entries,
    save_source_file,
    source_mode_label,
)


PROJECT_SUFFIX = ".tzproj.json"
AUTOSAVE_NAME = "translator_autosave.tzproj.json"
AUTOSAVE_DIR_NAME = "autosaves"
API_PROFILES_NAME = "api_profiles.json"


class WrapTextDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(wrap)
        doc.setPlainText(opt.text)
        widget = option.widget
        width = opt.rect.width()
        if widget is not None and hasattr(widget, "columnWidth"):
            width = widget.columnWidth(index.column()) - 12
        width = max(width, 80)
        doc.setTextWidth(width)
        return QSize(int(doc.idealWidth()) + 8, int(doc.size().height()) + 8)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(wrap)
        doc.setPlainText(text)
        doc.setTextWidth(max(opt.rect.width() - 8, 80))
        context = QAbstractTextDocumentLayout.PaintContext()
        if opt.state & QStyle.State_Selected:
            context.palette.setColor(QPalette.Text, QColor("#102010"))
        else:
            context.palette.setColor(QPalette.Text, QColor("#1f2a1f"))
        painter.save()
        painter.translate(opt.rect.left() + 4, opt.rect.top() + 4)
        doc.documentLayout().draw(painter, context)
        painter.restore()


class CopyableTableWidget(QTableWidget):
    def __init__(self) -> None:
        super().__init__(0, 2)
        self.setHorizontalHeaderLabels(["Source", "Translation"])
        self.setWordWrap(True)
        self.setTextElideMode(Qt.ElideNone)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(32)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.setItemDelegate(WrapTextDelegate(self))

    def copy_rows(self, mode: str = "both") -> str:
        selected_indexes = self.selectionModel().selectedRows()
        if not selected_indexes:
            return ""

        lines: list[str] = []
        for model_index in sorted(selected_indexes, key=lambda item: item.row()):
            row = model_index.row()
            source = self.item(row, 0).text() if self.item(row, 0) else ""
            translation = self.item(row, 1).text() if self.item(row, 1) else ""
            if mode == "source":
                lines.append(source)
            elif mode == "translation":
                lines.append(translation)
            else:
                lines.append(f"{source}\t{translation}")

        text = "\n".join(lines)
        QApplication.clipboard().setText(text)
        return text

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.Copy):
            self.copy_rows("source")
            event.accept()
            return
        super().keyPressEvent(event)


class TranslatorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Translator JSON Tool")
        self.resize(1480, 860)
        self.setMinimumSize(860, 520)

        self.source_data: Any = None
        self.source_path: str = ""
        self.project_path: Path | None = None
        self.source_mode = MODE_AUTO
        self.source_encoding = "utf-8"
        self.source_documents: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.rows_by_pointer: dict[str, dict[str, Any]] = {}
        self.current_pointer: str | None = None
        self.is_syncing_editor = False
        self.last_find_pointer: str | None = None
        self.api_request_active = False
        self.api_request_thread: threading.Thread | None = None
        self.api_result_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.is_closing = False
        self.last_auto_scope_pointers: list[str] = []
        self.last_project_dir: Path | None = None

        self.default_source_path = Path("sample_input.json")
        self.autosave_path = Path(__file__).with_name(AUTOSAVE_NAME)
        self.autosave_root = Path(__file__).with_name(AUTOSAVE_DIR_NAME)
        self.projects_root = Path(__file__).with_name("projects")
        self.settings_path = Path(__file__).with_name("ui_state.json")
        self.api_profiles_path = Path(__file__).with_name(API_PROFILES_NAME)
        self.api_poll_timer = QTimer(self)
        self.api_poll_timer.setInterval(120)
        self.api_poll_timer.timeout.connect(self._poll_api_results)

        self._build_ui()
        self._apply_theme()
        self._restore_ui_state()
        self._load_startup_state()

    def _selected_source_mode(self) -> str:
        data = self.source_mode_combo.currentData() if hasattr(self, "source_mode_combo") else MODE_AUTO
        return str(data or MODE_AUTO)

    def _set_source_mode_combo(self, mode: str) -> None:
        if not hasattr(self, "source_mode_combo"):
            return
        index = self.source_mode_combo.findData(mode)
        self.source_mode_combo.setCurrentIndex(max(index, 0))

    def _update_source_mode_label(self, *_args: Any) -> None:
        if not hasattr(self, "source_mode_label"):
            return
        if self.source_data is None:
            self.source_mode_label.setText(f"Mode: {source_mode_label(self._selected_source_mode())}")
        else:
            self.source_mode_label.setText(f"Mode: {source_mode_label(self.source_mode)}")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        file_row = QHBoxLayout()
        root.addLayout(file_row)
        file_row.addWidget(QLabel("Source"))
        self.path_input = QLineEdit()
        self.path_input.returnPressed.connect(self.load_from_current_path)
        file_row.addWidget(self.path_input, 1)
        file_row.addWidget(QLabel("Mode"))
        self.source_mode_combo = QComboBox()
        for mode, label in available_source_modes():
            self.source_mode_combo.addItem(label, mode)
        self.source_mode_combo.currentIndexChanged.connect(self._update_source_mode_label)
        file_row.addWidget(self.source_mode_combo)
        self.source_mode_label = QLabel("Mode: Auto Detect")
        file_row.addWidget(self.source_mode_label)

        browse_button = QPushButton("Open Source")
        browse_button.clicked.connect(self.browse_source)
        file_row.addWidget(browse_button)

        load_batch_button = QPushButton("Load Batch")
        load_batch_button.clicked.connect(self.load_batch_sources)
        file_row.addWidget(load_batch_button)

        save_json_button = QPushButton("Save Output")
        save_json_button.clicked.connect(self.export_json)
        file_row.addWidget(save_json_button)

        open_project_button = QPushButton("Open Project")
        open_project_button.clicked.connect(self.open_project)
        file_row.addWidget(open_project_button)

        save_project_button = QPushButton("Save Project")
        save_project_button.clicked.connect(self.save_project)
        file_row.addWidget(save_project_button)

        save_project_as_button = QPushButton("Save Project As")
        save_project_as_button.clicked.connect(self.save_project_as)
        file_row.addWidget(save_project_as_button)

        filter_box = self._make_box("Filters")
        filter_layout = filter_box.layout()
        filter_row = QHBoxLayout()
        filter_layout.addLayout(filter_row)
        self.show_all_checkbox = QCheckBox("Show identifiers and numbers")
        self.show_all_checkbox.toggled.connect(self.refresh_views)
        filter_row.addWidget(self.show_all_checkbox)
        search_label = QLabel("Search")
        search_label.setStyleSheet("font-weight: 700;")
        filter_row.addWidget(search_label)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Supports wildcard: * and ?")
        self.search_input.textChanged.connect(self.refresh_views)
        filter_row.addWidget(self.search_input, 1)
        group_filter_label = QLabel("Group filter")
        group_filter_label.setStyleSheet("font-weight: 700;")
        filter_row.addWidget(group_filter_label)
        self.group_filter_combo = QComboBox()
        self.group_filter_combo.currentIndexChanged.connect(self.refresh_views)
        filter_row.addWidget(self.group_filter_combo, 1)
        root.addWidget(filter_box)

        batch_box = self._make_box("Batch Selection Actions")
        batch_layout = batch_box.layout()
        batch_row = QHBoxLayout()
        batch_layout.addLayout(batch_row)
        to_skipped_button = QPushButton("Move Selected To Skipped")
        to_skipped_button.clicked.connect(lambda: self.set_skip_for_selected(True))
        batch_row.addWidget(to_skipped_button)
        to_worklist_button = QPushButton("Move Selected To Worklist")
        to_worklist_button.clicked.connect(lambda: self.set_skip_for_selected(False))
        batch_row.addWidget(to_worklist_button)
        copy_source_button = QPushButton("Copy Source")
        copy_source_button.clicked.connect(lambda: self.copy_selected_rows("source"))
        batch_row.addWidget(copy_source_button)
        copy_translation_button = QPushButton("Copy Translation")
        copy_translation_button.clicked.connect(lambda: self.copy_selected_rows("translation"))
        batch_row.addWidget(copy_translation_button)
        copy_both_button = QPushButton("Copy Both")
        copy_both_button.clicked.connect(lambda: self.copy_selected_rows("both"))
        batch_row.addWidget(copy_both_button)
        apply_group_label = QLabel("Apply group")
        apply_group_label.setStyleSheet("font-weight: 700;")
        batch_row.addWidget(apply_group_label)
        self.group_apply_combo = QComboBox()
        self.group_apply_combo.setEditable(True)
        self.group_apply_combo.setInsertPolicy(QComboBox.NoInsert)
        batch_row.addWidget(self.group_apply_combo, 1)
        apply_group_button = QPushButton("Apply")
        apply_group_button.clicked.connect(self.apply_group_to_selected)
        batch_row.addWidget(apply_group_button)
        clear_group_button = QPushButton("Clear")
        clear_group_button.clicked.connect(self.clear_group_for_selected)
        batch_row.addWidget(clear_group_button)
        root.addWidget(batch_box)
        self.stats_label = QLabel("Files: 0 | Text items: 0 | Worklist: 0 | Translated: 0 | Skipped: 0 | Encoding: utf-8")
        root.addWidget(self.stats_label)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(True)
        root.addWidget(self.splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        left_header = QHBoxLayout()
        left_layout.addLayout(left_header)
        self.view_hint_label = QLabel("Browse and select rows on the left. Double-click Translation to edit inline.")
        left_header.addWidget(self.view_hint_label, 1)
        left_refresh_button = QPushButton("Refresh View")
        left_refresh_button.clicked.connect(self.refresh_views)
        left_header.addWidget(left_refresh_button)

        self.view_tabs = QTabWidget()
        self.view_tabs.currentChanged.connect(self.on_view_tab_changed)
        left_layout.addWidget(self.view_tabs, 1)

        self.worklist_table = self._create_table()
        self.translated_table = self._create_table()
        self.skipped_table = self._create_table()
        for name, table in (("Worklist", self.worklist_table), ("Translated", self.translated_table), ("Skipped", self.skipped_table)):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.addWidget(table)
            self.view_tabs.addTab(page, name)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.editor_tabs = QTabWidget()
        right_layout.addWidget(self.editor_tabs, 1)

        self._build_manual_tab()
        self._build_batch_tab()
        self._build_auto_tab()

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([1, 1])
        self.splitter.splitterMoved.connect(self._resize_all_tables)
        QTimer.singleShot(0, self._resize_all_tables)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage("Load a source file or open a project.")

        QShortcut(QKeySequence.Copy, self.worklist_table, activated=lambda: self.copy_selected_rows("source"))
        QShortcut(QKeySequence.Copy, self.translated_table, activated=lambda: self.copy_selected_rows("source"))
        QShortcut(QKeySequence.Copy, self.skipped_table, activated=lambda: self.copy_selected_rows("source"))

    def _build_manual_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top_box = self._make_box("Manual")
        top_layout = top_box.layout()
        top_row = QHBoxLayout()
        top_layout.addLayout(top_row)
        top_row.addWidget(QLabel("Key"))
        self.key_value = QLineEdit()
        self.key_value.setReadOnly(True)
        top_row.addWidget(self.key_value, 1)
        copy_key_button = QPushButton("Copy Key")
        copy_key_button.clicked.connect(self.copy_current_key)
        top_row.addWidget(copy_key_button)
        self.skip_toggle = QCheckBox("Skipped")
        self.skip_toggle.toggled.connect(self.on_skip_toggle_changed)
        top_row.addWidget(self.skip_toggle)

        self.kind_label = QLabel("kind: -")
        top_layout.addWidget(self.kind_label)
        self.group_label = QLabel("group: -")
        top_layout.addWidget(self.group_label)
        self.pointer_label = QLabel("pointer: -")
        self.pointer_label.setWordWrap(True)
        top_layout.addWidget(self.pointer_label)
        layout.addWidget(top_box)

        layout.addWidget(QLabel("Source"))
        self.source_editor = QPlainTextEdit()
        self.source_editor.setReadOnly(True)
        self.source_editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.source_editor, 3)

        layout.addWidget(QLabel("Translation"))
        self.translation_editor = QPlainTextEdit()
        self.translation_editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.translation_editor.textChanged.connect(self.on_translation_editor_changed)
        layout.addWidget(self.translation_editor, 4)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        save_current_button = QPushButton("Save Current")
        save_current_button.clicked.connect(self.save_current_translation)
        actions.addWidget(save_current_button)
        reset_button = QPushButton("Reset To Source")
        reset_button.clicked.connect(self.reset_current_translation)
        actions.addWidget(reset_button)
        actions.addStretch(1)

        self.editor_tabs.addTab(page, "Manual")

    def _build_batch_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        paste_box = self._make_box("Batch Paste To Selected Rows")
        paste_layout = paste_box.layout()
        paste_layout.addWidget(QLabel("Paste one translation per line. They will be applied to the selected rows in visible order."))
        self.batch_paste_editor = QPlainTextEdit()
        self.batch_paste_editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        paste_layout.addWidget(self.batch_paste_editor)
        paste_actions = QHBoxLayout()
        paste_layout.addLayout(paste_actions)
        apply_paste_button = QPushButton("Apply Batch Paste")
        apply_paste_button.clicked.connect(self.apply_batch_paste)
        paste_actions.addWidget(apply_paste_button)
        clear_paste_button = QPushButton("Clear")
        clear_paste_button.clicked.connect(self.batch_paste_editor.clear)
        paste_actions.addWidget(clear_paste_button)
        paste_actions.addStretch(1)
        layout.addWidget(paste_box)

        replace_box = self._make_box("Find / Replace")
        replace_layout = replace_box.layout()
        row1 = QHBoxLayout()
        replace_layout.addLayout(row1)
        row1.addWidget(QLabel("Find"))
        self.find_input = QLineEdit()
        row1.addWidget(self.find_input, 1)
        row1.addWidget(QLabel("Replace"))
        self.replace_input = QLineEdit()
        row1.addWidget(self.replace_input, 1)
        row2 = QHBoxLayout()
        replace_layout.addLayout(row2)
        find_next_button = QPushButton("Find Next")
        find_next_button.clicked.connect(self.find_next)
        row2.addWidget(find_next_button)
        replace_next_button = QPushButton("Replace Next")
        replace_next_button.clicked.connect(self.replace_next)
        row2.addWidget(replace_next_button)
        replace_all_button = QPushButton("Replace All In View")
        replace_all_button.clicked.connect(self.replace_all)
        row2.addWidget(replace_all_button)
        row2.addStretch(1)
        layout.addWidget(replace_box)
        layout.addStretch(1)

        self.editor_tabs.addTab(page, "Batch")

    def _build_auto_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        summary_box = self._make_box("Auto")
        summary_layout = summary_box.layout()
        self.auto_summary_label = QLabel("Auto scope: no rows yet. Multi-select rows on the left, or use the current view.")
        self.auto_summary_label.setWordWrap(True)
        summary_layout.addWidget(self.auto_summary_label)
        refresh_auto_button = QPushButton("Refresh Auto Scope")
        refresh_auto_button.clicked.connect(self.preview_auto_scope)
        summary_layout.addWidget(refresh_auto_button)
        layout.addWidget(summary_box)

        web_box = self._make_box("Web Workflow")
        web_layout = web_box.layout()
        web_layout.addWidget(QLabel("Use this for Gemini / GPT web pages. The exported text is numbered, so pasted results can map back safely."))
        web_button_row = QHBoxLayout()
        web_layout.addLayout(web_button_row)
        copy_sources_button = QPushButton("Copy Numbered Source")
        copy_sources_button.clicked.connect(self.copy_auto_source_lines)
        web_button_row.addWidget(copy_sources_button)
        copy_prompt_button = QPushButton("Copy Web Prompt")
        copy_prompt_button.clicked.connect(self.copy_auto_web_prompt)
        web_button_row.addWidget(copy_prompt_button)
        web_button_row.addStretch(1)
        self.auto_web_prompt = QPlainTextEdit()
        self.auto_web_prompt.setReadOnly(True)
        self.auto_web_prompt.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.auto_web_prompt.setPlaceholderText("Prompt preview will appear here.")
        web_layout.addWidget(self.auto_web_prompt, 2)
        web_layout.addWidget(QLabel("Paste translated numbered blocks below. Format: [1] text / [2] text / ..."))
        self.auto_paste_editor = QPlainTextEdit()
        self.auto_paste_editor.setPlaceholderText("[1] translated text\n[2] translated text\n[3] translated text")
        web_layout.addWidget(self.auto_paste_editor, 2)
        web_apply_row = QHBoxLayout()
        web_layout.addLayout(web_apply_row)
        apply_web_results_button = QPushButton("Apply Pasted Results")
        apply_web_results_button.clicked.connect(self.apply_auto_pasted_results)
        web_apply_row.addWidget(apply_web_results_button)
        clear_web_results_button = QPushButton("Clear Pasted Results")
        clear_web_results_button.clicked.connect(self.auto_paste_editor.clear)
        web_apply_row.addWidget(clear_web_results_button)
        web_apply_row.addStretch(1)
        layout.addWidget(web_box, 3)

        api_box = self._make_box("API Workflow")
        api_layout = api_box.layout()
        api_layout.addWidget(QLabel("API mode translates the current auto scope directly through an OpenAI-compatible endpoint."))

        api_profile_row = QHBoxLayout()
        api_layout.addLayout(api_profile_row)
        api_profile_row.addWidget(QLabel("Profile"))
        self.api_profile_combo = QComboBox()
        self.api_profile_combo.setEditable(True)
        self.api_profile_combo.setInsertPolicy(QComboBox.NoInsert)
        self.api_profile_combo.currentIndexChanged.connect(self.load_selected_api_profile)
        api_profile_row.addWidget(self.api_profile_combo, 1)
        use_api_profile_button = QPushButton("Use Profile")
        use_api_profile_button.clicked.connect(self.load_selected_api_profile)
        api_profile_row.addWidget(use_api_profile_button)
        save_api_profile_button = QPushButton("Save Profile")
        save_api_profile_button.clicked.connect(self.save_current_api_profile)
        api_profile_row.addWidget(save_api_profile_button)

        api_provider_row = QHBoxLayout()
        api_layout.addLayout(api_provider_row)
        api_provider_row.addWidget(QLabel("Provider"))
        self.api_provider_combo = QComboBox()
        self.api_provider_combo.addItems([
            "OpenAI Compatible",
            "Gemini (API)",
            "Domestic OpenAI-Compatible",
            "Custom",
        ])
        api_provider_row.addWidget(self.api_provider_combo, 1)
        api_provider_row.addWidget(QLabel("Model"))
        self.api_model_input = QLineEdit()
        self.api_model_input.setPlaceholderText("deepseek-chat / DeepSeek-V3 / qwen-max / gemini-2.5-pro")
        api_provider_row.addWidget(self.api_model_input, 1)

        api_auth_row = QHBoxLayout()
        api_layout.addLayout(api_auth_row)
        api_auth_row.addWidget(QLabel("API Key"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("Paste your API key here")
        self.api_key_input.setEchoMode(QLineEdit.Password)
        api_auth_row.addWidget(self.api_key_input, 1)

        api_base_row = QHBoxLayout()
        api_layout.addLayout(api_base_row)
        api_base_row.addWidget(QLabel("Base URL"))
        self.api_base_url_input = QLineEdit()
        self.api_base_url_input.setPlaceholderText("https://api.siliconflow.cn/v1 / https://api.deepseek.com / custom compatible endpoint")
        api_base_row.addWidget(self.api_base_url_input, 1)

        api_param_row = QHBoxLayout()
        api_layout.addLayout(api_param_row)
        api_param_row.addWidget(QLabel("Temperature"))
        self.api_temperature_input = QLineEdit()
        self.api_temperature_input.setPlaceholderText("0.2")
        api_param_row.addWidget(self.api_temperature_input)
        api_param_row.addWidget(QLabel("Batch size"))
        self.api_batch_size_input = QLineEdit()
        self.api_batch_size_input.setPlaceholderText("20")
        api_param_row.addWidget(self.api_batch_size_input)
        api_param_row.addStretch(1)

        self.api_note_label = QLabel("Suggested models: chat/instruct types usually work better for translation.")
        self.api_note_label.setWordWrap(True)

        api_button_row = QHBoxLayout()
        api_layout.addLayout(api_button_row)
        api_button_row.addWidget(self.api_note_label, 1)
        self.translate_api_button = QPushButton("Translate via API")
        self.translate_api_button.clicked.connect(self.translate_via_api)
        api_button_row.addWidget(self.translate_api_button)
        layout.addWidget(api_box)
        layout.addStretch(1)

        self.editor_tabs.addTab(page, "Auto")
    def _create_table(self) -> CopyableTableWidget:
        table = CopyableTableWidget()
        table.itemSelectionChanged.connect(self.on_table_selection_changed)
        table.itemChanged.connect(self.on_table_item_changed)
        return table

    def _make_box(self, title: str) -> QFrame:
        box = QFrame()
        box.setObjectName("panelBox")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title_label = QLabel(title); title_label.setStyleSheet("font-weight: 700;")
        title_label.setObjectName("panelTitle")
        layout.addWidget(title_label)
        return box
    def _apply_theme(self) -> None:
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("#e8f2e6"))
        palette.setColor(QPalette.Base, QColor("#f8fbf6"))
        palette.setColor(QPalette.AlternateBase, QColor("#eef5ec"))
        palette.setColor(QPalette.Text, QColor("#1f2a1f"))
        palette.setColor(QPalette.WindowText, QColor("#1f2a1f"))
        palette.setColor(QPalette.Button, QColor("#d5e6d2"))
        palette.setColor(QPalette.ButtonText, QColor("#1f2a1f"))
        palette.setColor(QPalette.Highlight, QColor("#87b781"))
        palette.setColor(QPalette.HighlightedText, QColor("#0f200f"))
        self.setPalette(palette)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #e8f2e6; color: #1f2a1f; font-size: 13px; }
            QLineEdit, QPlainTextEdit, QTableWidget, QComboBox {
                background: #f8fbf6;
                border: 1px solid #aabca7;
                border-radius: 8px;
                selection-background-color: #93c38d;
            }
            QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] { background: #edf4ea; }
            QPushButton {
                background: #d4e4d1;
                border: 1px solid #9ab297;
                border-radius: 8px;
                padding: 7px 12px;
            }
            QPushButton:hover { background: #c9ddc6; }
            QHeaderView::section {
                background: #dbe8d8;
                border: none;
                border-bottom: 1px solid #b8c8b5;
                padding: 8px;
            }
            QTableWidget { gridline-color: #d2d7d2; }
            QTableWidget::item {
                color: #1f2a1f;
                border-right: 1px dashed #d2d7d2;
                border-bottom: 1px dashed #d2d7d2;
                padding: 6px;
            }
            QTableWidget::item:selected { background: #b9dcb5; color: #102010; }
            QCheckBox { spacing: 8px; }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #7e9878;
                border-radius: 4px;
                background: #f8fbf6;
            }
            QCheckBox::indicator:checked {
                background: #7fba78;
                border: 2px solid #5f905a;
            }
            QFrame#panelBox {
                background: #edf5ea;
                border: 1px solid #aabca7;
                border-radius: 10px;
            }
            QLabel#panelTitle { font-weight: 600; color: #3a5736; }
            QTabBar::tab {
                background: #d9e7d6;
                border: 1px solid #aabca7;
                border-bottom: none;
                padding: 8px 16px;
                margin-right: 4px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }
            QTabBar::tab:selected {
                background: #b8d4b3;
                color: #123012;
                font-weight: 600;
            }
            QStatusBar { background: #dce9d9; }
            """
        )

    def _load_startup_state(self) -> None:
        self._load_api_profiles()
        last_autosave_path: Path | None = None
        if self.settings_path.exists():
            try:
                ui_state = json.loads(self.settings_path.read_text(encoding="utf-8"))
                raw_last_autosave = str(ui_state.get("last_autosave_path", "")).strip()
                if raw_last_autosave:
                    last_autosave_path = Path(raw_last_autosave)
                raw_last_project_dir = str(ui_state.get("last_project_dir", "")).strip()
                if raw_last_project_dir:
                    self.last_project_dir = Path(raw_last_project_dir)
            except Exception:
                last_autosave_path = None
        if last_autosave_path is not None and last_autosave_path.exists():
            self.load_project_file(last_autosave_path)
            return
        if self.autosave_path.exists():
            self.load_project_file(self.autosave_path)
            return
        if self.default_source_path.exists():
            self.path_input.setText(str(self.default_source_path))
            self.load_from_current_path()

    def _default_api_profiles(self) -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": "Profile 1",
                    "provider": "OpenAI Compatible",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3",
                    "temperature": "0.2",
                    "batch_size": "20",
                },
                {
                    "name": "Profile 2",
                    "provider": "OpenAI Compatible",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "temperature": "0.2",
                    "batch_size": "20",
                },
            ]
        }

    def _normalize_api_profiles(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        used_names: set[str] = set()
        default_name_map = {
            "SiliconFlow Default": "Profile 1",
            "DeepSeek Official": "Profile 2",
            "OpenAI Compatible Template": "Profile 3",
        }
        for index, profile in enumerate(profiles, start=1):
            item = dict(profile)
            raw_name = str(item.get("name", "")).strip()
            name = default_name_map.get(raw_name, raw_name)
            if not name or name == "Custom":
                name = f"Profile {index}"
            candidate = name
            suffix = 2
            while candidate in used_names:
                candidate = f"{name} ({suffix})"
                suffix += 1
            item["name"] = candidate
            normalized.append(item)
            used_names.add(candidate)
        return normalized

    def _load_api_profiles(self) -> None:
        if not self.api_profiles_path.exists():
            self.api_profiles_path.write_text(json.dumps(self._default_api_profiles(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            payload = json.loads(self.api_profiles_path.read_text(encoding="utf-8"))
        except Exception:
            payload = self._default_api_profiles()
        profiles = self._normalize_api_profiles(payload.get("profiles", []))
        if profiles != payload.get("profiles", []):
            self._write_api_profiles(profiles)
        self.api_profile_combo.blockSignals(True)
        self.api_profile_combo.clear()
        self.api_profile_combo.addItem("Custom")
        for profile in profiles:
            self.api_profile_combo.addItem(str(profile.get("name", "Unnamed")))
        self.api_profile_combo.blockSignals(False)
        self.api_profile_combo.setEditText("Custom")
        if profiles:
            self.api_profile_combo.setCurrentIndex(1)
            self._apply_api_profile(profiles[0])

    def _read_api_profiles(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.api_profiles_path.read_text(encoding="utf-8"))
            return self._normalize_api_profiles(payload.get("profiles", []))
        except Exception:
            return []

    def _write_api_profiles(self, profiles: list[dict[str, Any]]) -> None:
        self.api_profiles_path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _current_api_config(self) -> dict[str, Any]:
        return {
            "provider": self.api_provider_combo.currentText(),
            "base_url": self.api_base_url_input.text().strip(),
            "model": self.api_model_input.text().strip(),
            "api_key": self.api_key_input.text().strip(),
            "temperature": self.api_temperature_input.text().strip() or "0.2",
            "batch_size": self.api_batch_size_input.text().strip() or "20",
        }

    def _apply_api_profile(self, profile: dict[str, Any]) -> None:
        provider = str(profile.get("provider", "OpenAI Compatible"))
        index = self.api_provider_combo.findText(provider)
        self.api_provider_combo.setCurrentIndex(max(index, 0))
        self.api_base_url_input.setText(str(profile.get("base_url", "")))
        self.api_model_input.setText(str(profile.get("model", "")))
        self.api_key_input.setText(str(profile.get("api_key", "")))
        self.api_temperature_input.setText(str(profile.get("temperature", "0.2")))
        self.api_batch_size_input.setText(str(profile.get("batch_size", "20")))

    def load_selected_api_profile(self) -> None:
        index = self.api_profile_combo.currentIndex()
        if index <= 0:
            return
        profiles = self._read_api_profiles()
        if 0 <= index - 1 < len(profiles):
            self._apply_api_profile(profiles[index - 1])

    def save_current_api_profile(self) -> None:
        name = self.api_profile_combo.currentText().strip()
        if not name or name == "Custom":
            existing_names = {str(profile.get("name", "")).strip() for profile in self._read_api_profiles()}
            index = 1
            while f"Profile {index}" in existing_names:
                index += 1
            name = f"Profile {index}"
        config = self._current_api_config()
        profiles = self._read_api_profiles()
        saved = False
        for profile in profiles:
            if str(profile.get("name")) == name:
                profile.update(config)
                profile["name"] = name
                saved = True
                break
        if not saved:
            config["name"] = name
            profiles.append(config)
        self._write_api_profiles(profiles)
        self._load_api_profiles()
        found = self.api_profile_combo.findText(name)
        if found >= 0:
            self.api_profile_combo.setCurrentIndex(found)
        else:
            self.api_profile_combo.setEditText(name)
        self.statusBar().showMessage(f"Saved API profile {name}.")
    def _restore_ui_state(self) -> None:
        if not self.settings_path.exists():
            self._refresh_group_controls()
            return
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            self._refresh_group_controls()
            return
        if isinstance(data.get("width"), int) and isinstance(data.get("height"), int):
            self.resize(data["width"], data["height"])
        splitter_sizes = data.get("splitter_sizes")
        if isinstance(splitter_sizes, list) and len(splitter_sizes) == 2 and all(isinstance(size, int) for size in splitter_sizes):
            self.splitter.setSizes(splitter_sizes)
        if isinstance(data.get("show_all"), bool):
            self.show_all_checkbox.setChecked(data["show_all"])
        self._refresh_group_controls(data.get("group_filter", "All groups"))

    def _save_ui_state(self) -> None:
        data = {
            "width": self.width(),
            "height": self.height(),
            "splitter_sizes": self.splitter.sizes(),
            "show_all": self.show_all_checkbox.isChecked(),
            "group_filter": self.group_filter_combo.currentText(),
            "last_autosave_path": str(self._autosave_target_path()) if self.source_data is not None else "",
            "last_project_dir": str(self.last_project_dir) if self.last_project_dir is not None else "",
        }
        self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    def closeEvent(self, event) -> None:
        self.is_closing = True
        self.api_poll_timer.stop()
        self._save_ui_state()
        self._autosave_project()
        super().closeEvent(event)

    def _make_document_id(self, path: Path, index: int) -> str:
        digest = hashlib.sha1(f"{path}:{index}".encode("utf-8", errors="replace")).hexdigest()[:10]
        stem = re.sub(r"[^0-9A-Za-z_-]+", "_", path.stem).strip("_") or "source"
        return f"{stem}-{digest}"

    def _rebuild_rows_from_documents(self) -> None:
        rows: list[dict[str, Any]] = []
        for document in self.source_documents:
            extracted = entries_to_dict_rows(extract_translation_entries(document["data"], mode=document["mode"]))
            for row in extracted:
                row.setdefault("group", "")
                row.setdefault("skip", False)
                row["local_pointer"] = str(row["pointer"])
                row["pointer"] = f"{document['id']}::{row['local_pointer']}"
                row["source_id"] = document["id"]
                row["source_path"] = document["path"]
                row["source_label"] = document["label"]
                rows.append(row)
        self.rows = rows
        self.rows_by_pointer = {str(row["pointer"]): row for row in self.rows}

    def _update_loaded_source_summary(self) -> None:
        if not self.source_documents:
            self.path_input.setText("")
            return
        if len(self.source_documents) == 1:
            self.path_input.setText(self.source_documents[0]["path"])
            return
        first_dir = Path(self.source_documents[0]["path"]).parent
        self.path_input.setText(f"{len(self.source_documents)} files loaded from {first_dir}")

    def _set_loaded_documents(self, documents: list[dict[str, Any]], *, project_path: Path | None = None) -> None:
        self.source_documents = documents
        if documents:
            self.source_mode = str(documents[0]["mode"])
            encodings = {str(document["encoding"]) for document in documents}
            self.source_encoding = next(iter(encodings)) if len(encodings) == 1 else "mixed"
            self.source_path = documents[0]["path"]
            self.source_data = documents[0]["data"] if len(documents) == 1 else {"_batch": True, "count": len(documents)}
        else:
            self.source_mode = MODE_AUTO
            self.source_encoding = "utf-8"
            self.source_path = ""
            self.source_data = None
        self.project_path = project_path
        self.last_project_dir = project_path.parent if project_path is not None else self.last_project_dir
        self.current_pointer = None
        self.last_auto_scope_pointers = []
        self._rebuild_rows_from_documents()
        self._update_loaded_source_summary()
        self._update_source_mode_label()
        self._refresh_group_controls(self.group_filter_combo.currentText() or "All groups")
        self.refresh_views(select_first=True)

    def _load_paths_as_documents(self, paths: list[Path]) -> list[dict[str, Any]]:
        if not paths:
            return []
        requested_mode = self._selected_source_mode()
        documents: list[dict[str, Any]] = []
        detected_modes: set[str] = set()
        for index, path in enumerate(paths, start=1):
            data, mode, encoding = load_source_file(path, requested_mode)
            detected_modes.add(mode)
            documents.append(
                {
                    "id": self._make_document_id(path, index),
                    "path": str(path),
                    "label": path.name,
                    "data": data,
                    "mode": mode,
                    "encoding": encoding,
                }
            )
        if len(detected_modes) > 1:
            labels = ", ".join(sorted(source_mode_label(mode) for mode in detected_modes))
            raise ValueError(f"Batch load requires the same detected mode. Found: {labels}")
        return documents
    def browse_source(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open source file",
            self.path_input.text().strip() or str(Path.home()),
            "Supported Files (*.json *.xml *.txt *.ini *.cfg *.lang *.properties);;All Files (*.*)",
        )
        if not file_path:
            return
        self.path_input.setText(file_path)
        self.load_from_current_path()
    def load_batch_sources(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open source files",
            self.path_input.text().strip() or str(Path.home()),
            "Supported Files (*.json *.xml *.txt *.ini *.cfg *.lang *.properties);;All Files (*.*)",
        )
        if not file_paths:
            return
        try:
            paths = [Path(raw_path) for raw_path in file_paths]
            documents = self._load_paths_as_documents(paths)
            self._set_loaded_documents(documents)
            self.statusBar().showMessage(f"Loaded batch with {len(documents)} files ({source_mode_label(self.source_mode)})")
            self._autosave_project()
        except Exception as exc:
            QMessageBox.critical(self, "Batch load failed", str(exc))
    def load_from_current_path(self) -> None:
        raw_path = self.path_input.text().strip()
        if not raw_path:
            QMessageBox.critical(self, "Load failed", "Choose or enter a source path first.")
            return
        path = Path(raw_path)
        try:
            documents = self._load_paths_as_documents([path])
            self._set_loaded_documents(documents)
            self.statusBar().showMessage(f"Loaded source {path} ({source_mode_label(self.source_mode)})")
            self._autosave_project()
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
    def _refresh_group_controls(self, preferred_filter: str = "All groups") -> None:
        groups = sorted({str(row.get("group", "")).strip() for row in self.rows if str(row.get("group", "")).strip()})
        self.group_filter_combo.blockSignals(True)
        self.group_filter_combo.clear()
        self.group_filter_combo.addItems(["All groups", "Ungrouped"] + groups)
        index = self.group_filter_combo.findText(preferred_filter)
        self.group_filter_combo.setCurrentIndex(max(index, 0))
        self.group_filter_combo.blockSignals(False)

        current_apply = self.group_apply_combo.currentText().strip()
        self.group_apply_combo.blockSignals(True)
        self.group_apply_combo.clear()
        self.group_apply_combo.addItems(groups)
        self.group_apply_combo.setEditText(current_apply)
        self.group_apply_combo.blockSignals(False)

    def _project_payload(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_data": self.source_data,
            "source_mode": self.source_mode,
            "source_encoding": self.source_encoding,
            "source_mode_selection": self._selected_source_mode(),
            "source_documents": self.source_documents,
            "rows": self.rows,
        }
    def _load_project_payload(self, payload: dict[str, Any], project_path: Path | None) -> None:
        raw_documents = payload.get("source_documents")
        if isinstance(raw_documents, list) and raw_documents:
            self.source_documents = raw_documents
            self.source_mode = str(payload.get("source_mode", self.source_documents[0].get("mode", MODE_JSON_VALUE)))
            self.source_encoding = str(payload.get("source_encoding", self.source_documents[0].get("encoding", "utf-8")))
            self.source_path = str(payload.get("source_path", self.source_documents[0].get("path", "")))
            self.source_data = payload.get("source_data")
        else:
            self.source_path = str(payload.get("source_path", ""))
            self.source_data = payload.get("source_data")
            self.source_mode = str(payload.get("source_mode", MODE_JSON_VALUE))
            self.source_encoding = str(payload.get("source_encoding", "utf-8"))
            self.source_documents = []
            if self.source_path and self.source_data is not None:
                self.source_documents = [
                    {
                        "id": self._make_document_id(Path(self.source_path), 1),
                        "path": self.source_path,
                        "label": Path(self.source_path).name,
                        "data": self.source_data,
                        "mode": self.source_mode,
                        "encoding": self.source_encoding,
                    }
                ]
        incoming_rows = payload.get("rows", [])
        self._set_loaded_documents(self.source_documents, project_path=project_path)
        incoming_by_pointer = {str(row.get("pointer", "")): row for row in incoming_rows}
        incoming_by_local: dict[tuple[str, str], dict[str, Any]] = {}
        for row in incoming_rows:
            source_id = str(row.get("source_id", ""))
            local_pointer = str(row.get("local_pointer", row.get("pointer", "")))
            if source_id and local_pointer:
                incoming_by_local[(source_id, local_pointer)] = row
        for row in self.rows:
            saved = incoming_by_pointer.get(str(row["pointer"]))
            if saved is None:
                saved = incoming_by_local.get((str(row.get("source_id", "")), str(row.get("local_pointer", ""))))
            if saved is None:
                continue
            row["translation"] = str(saved.get("translation", row["translation"]))
            row["group"] = str(saved.get("group", row.get("group", "")))
            row["skip"] = bool(saved.get("skip", row.get("skip", False)))
        self.rows_by_pointer = {str(row["pointer"]): row for row in self.rows}
        self._set_source_mode_combo(str(payload.get("source_mode_selection", self.source_mode)))
        self._update_source_mode_label()
        self._refresh_group_controls(self.group_filter_combo.currentText() or "All groups")
        self.refresh_views(select_first=True)
    def save_project(self) -> None:
        if self.source_data is None:
            QMessageBox.critical(self, "Save project failed", "Load a source or project first.")
            return
        if self.project_path is None:
            self.save_project_as()
            return
        self._write_project_file(self.project_path)
        self.statusBar().showMessage(f"Saved project {self.project_path}")

    def _project_dialog_dir(self) -> str:
        if self.project_path is not None:
            candidate = self.project_path.parent
        elif self.last_project_dir is not None:
            candidate = self.last_project_dir
        else:
            candidate = self.projects_root
        candidate.mkdir(parents=True, exist_ok=True)
        return str(candidate)
    def save_project_as(self) -> None:
        if self.source_data is None:
            QMessageBox.critical(self, "Save project failed", "Load a source or project first.")
            return
        initial_name = Path(self.source_path).stem + PROJECT_SUFFIX if self.source_path else "project" + PROJECT_SUFFIX
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project As",
            str(Path(self._project_dialog_dir()) / initial_name),
            "Project Files (*.tzproj.json);;JSON Files (*.json)",
        )
        if not output_path:
            return
        self.project_path = Path(output_path)
        self.last_project_dir = self.project_path.parent
        self._write_project_file(self.project_path)
        self.statusBar().showMessage(f"Saved project {self.project_path}")
    def open_project(self) -> None:
        project_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            self._project_dialog_dir(),
            "Project Files (*.tzproj.json);;JSON Files (*.json)",
        )
        if not project_path:
            return
        self.load_project_file(Path(project_path))
    def load_project_file(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.last_project_dir = path.parent
            self._load_project_payload(payload, path)
            self.statusBar().showMessage(f"Opened project {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Open project failed", str(exc))

    def _write_project_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._project_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _autosave_target_path(self) -> Path:
        if self.project_path is not None:
            return self.project_path.with_name(self.project_path.stem + ".autosave" + PROJECT_SUFFIX)
        if self.source_path:
            source_path = Path(self.source_path)
            digest = hashlib.sha1(str(source_path).encode("utf-8", errors="replace")).hexdigest()[:10]
            stem = re.sub(r"[^0-9A-Za-z_-]+", "_", source_path.stem).strip("_") or "source"
            return self.autosave_root / f"{stem}-{digest}{PROJECT_SUFFIX}"
        return self.autosave_path

    def _autosave_project(self) -> None:
        if self.source_data is None:
            return
        self._write_project_file(self._autosave_target_path())
    def _view_name(self) -> str:
        return ["worklist", "translated", "skipped"][self.view_tabs.currentIndex()]

    def _row_is_translated(self, row: dict[str, Any]) -> bool:
        return bool(row["translation"].strip()) and row["translation"] != row["source"]

    def _row_matches_filters(self, row: dict[str, Any], view_name: str) -> bool:
        skipped = bool(row.get("skip"))
        translated = self._row_is_translated(row)
        if view_name == "worklist" and (skipped or translated):
            return False
        if view_name == "translated" and (skipped or not translated):
            return False
        if view_name == "skipped" and not skipped:
            return False
        if not self.show_all_checkbox.isChecked() and row["kind"] != "text":
            return False

        group_filter = self.group_filter_combo.currentText()
        row_group = str(row.get("group", "")).strip()
        if group_filter == "Ungrouped" and row_group:
            return False
        if group_filter not in ("", "All groups", "Ungrouped") and row_group != group_filter:
            return False

        keyword = self.search_input.text().strip()
        if keyword:
            haystacks = [row["key"], row["source"], row["translation"], row_group]
            if "*" in keyword or "?" in keyword:
                if not any(fnmatch.fnmatchcase(text.casefold(), keyword.casefold()) for text in haystacks):
                    return False
            else:
                lowered = keyword.casefold()
                if not any(lowered in text.casefold() for text in haystacks):
                    return False
        return True
    def visible_rows(self, view_name: str) -> list[dict[str, Any]]:
        filtered = [row for row in self.rows if self._row_matches_filters(row, view_name)]
        if len(self.source_documents) <= 1:
            return filtered
        grouped: list[dict[str, Any]] = []
        by_source: dict[str, list[dict[str, Any]]] = {}
        for row in filtered:
            by_source.setdefault(str(row.get("source_id", "")), []).append(row)
        for document in self.source_documents:
            source_id = str(document["id"])
            source_rows = by_source.get(source_id, [])
            if not source_rows:
                continue
            grouped.append(
                {
                    "is_header": True,
                    "pointer": "",
                    "key": "",
                    "kind": "header",
                    "source": f"[{document['label']}]",
                    "translation": "",
                    "group": "",
                    "skip": False,
                }
            )
            grouped.extend(source_rows)
        return grouped
    def refresh_views(self, select_first: bool = False) -> None:
        worklist_rows = self.visible_rows("worklist")
        translated_rows = self.visible_rows("translated")
        skipped_rows = self.visible_rows("skipped")
        self._fill_table(self.worklist_table, worklist_rows)
        self._fill_table(self.translated_table, translated_rows)
        self._fill_table(self.skipped_table, skipped_rows)

        text_count = sum(1 for row in self.rows if classify_text(row["source"]) == "text")
        self.stats_label.setText(
            f"Files: {len(self.source_documents)} | Text items: {text_count} | Worklist: {len(worklist_rows)} | Translated: {len(translated_rows)} | Skipped: {len(skipped_rows)} | Encoding: {self.source_encoding}"
        )

        table = self.current_table()
        if self.current_pointer and self.pointer_visible_in_table(self.current_pointer, table):
            self.select_pointer(self.current_pointer, table)
        elif select_first and table.rowCount() > 0:
            for row_index in range(table.rowCount()):
                pointer = self.pointer_at_row(table, row_index)
                if pointer:
                    self.select_pointer(pointer, table)
                    break
            else:
                self.clear_editor()
        elif table.rowCount() == 0:
            self.clear_editor()
    def _resize_table_columns(self, table: CopyableTableWidget) -> None:
        viewport_width = max(table.viewport().width(), 520)
        source_width = max(180, viewport_width // 2)
        translation_width = max(180, viewport_width - source_width)
        table.setColumnWidth(0, source_width)
        table.setColumnWidth(1, translation_width)

    def _resize_all_tables(self, *_args: Any) -> None:
        for table in (self.worklist_table, self.translated_table, self.skipped_table):
            self._resize_table_columns(table)
            table.resizeRowsToContents()
    def _fill_table(self, table: CopyableTableWidget, rows: list[dict[str, Any]]) -> None:
        table.blockSignals(True)
        table.clearSpans()
        table.clearContents()
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            if row.get("is_header"):
                header_item = QTableWidgetItem(f"  {row['source']}")
                header_item.setFlags(Qt.ItemIsEnabled)
                header_font = header_item.font()
                header_font.setBold(True)
                header_font.setPointSize(max(header_font.pointSize(), 11))
                header_item.setFont(header_font)
                header_item.setForeground(QColor("#173417"))
                header_item.setBackground(QColor("#c9ddc6"))
                table.setItem(row_index, 0, header_item)
                spacer_item = QTableWidgetItem("")
                spacer_item.setFlags(Qt.ItemIsEnabled)
                spacer_item.setBackground(QColor("#c9ddc6"))
                table.setItem(row_index, 1, spacer_item)
                table.setSpan(row_index, 0, 1, 2)
                table.setRowHeight(row_index, 34)
                continue
            source_item = QTableWidgetItem(row["source"])
            source_item.setFlags(source_item.flags() & ~Qt.ItemIsEditable)
            source_item.setData(Qt.UserRole, row["pointer"])
            source_item.setToolTip(row["source"])
            source_item.setForeground(QColor("#1f2a1f"))
            translation_item = QTableWidgetItem(row["translation"])
            translation_item.setData(Qt.UserRole, row["pointer"])
            translation_item.setToolTip(row["translation"])
            translation_item.setForeground(QColor("#1f2a1f"))
            table.setItem(row_index, 0, source_item)
            table.setItem(row_index, 1, translation_item)
        table.blockSignals(False)
        self._resize_table_columns(table)
        table.resizeRowsToContents()
    def current_table(self) -> CopyableTableWidget:
        return [self.worklist_table, self.translated_table, self.skipped_table][self.view_tabs.currentIndex()]

    def on_view_tab_changed(self, index: int) -> None:
        messages = [
            "Worklist shows rows that are not translated and not skipped.",
            "Translated shows finished rows for review and adjustment.",
            "Skipped rows are excluded from JSON export until moved back.",
        ]
        self.view_hint_label.setText(messages[index])
        table = self.current_table()
        if table.rowCount() > 0:
            self.select_pointer(self.pointer_at_row(table, 0), table)
        else:
            self.clear_editor()

    def pointer_at_row(self, table: CopyableTableWidget, row_index: int) -> str:
        item = table.item(row_index, 0)
        return item.data(Qt.UserRole) if item else ""

    def pointer_visible_in_table(self, pointer: str, table: CopyableTableWidget) -> bool:
        for row_index in range(table.rowCount()):
            if self.pointer_at_row(table, row_index) == pointer:
                return True
        return False

    def select_pointer(self, pointer: str, table: CopyableTableWidget | None = None) -> None:
        table = table or self.current_table()
        for row_index in range(table.rowCount()):
            if self.pointer_at_row(table, row_index) == pointer:
                table.setCurrentCell(row_index, 0)
                self.current_pointer = pointer
                self.load_editor_from_pointer(pointer)
                break

    def get_row_by_pointer(self, pointer: str | None) -> dict[str, Any] | None:
        if not pointer:
            return None
        return self.rows_by_pointer.get(str(pointer))
    def on_table_selection_changed(self) -> None:
        table = self.sender()
        if table is self.worklist_table:
            self.view_tabs.setCurrentIndex(0)
        elif table is self.translated_table:
            self.view_tabs.setCurrentIndex(1)
        elif table is self.skipped_table:
            self.view_tabs.setCurrentIndex(2)
        current_row = table.currentRow()
        if current_row < 0:
            self.clear_editor()
            return
        pointer = self.pointer_at_row(table, current_row)
        if not pointer:
            self.clear_editor()
            return
        self.current_pointer = pointer
        self.load_editor_from_pointer(pointer)
    def load_editor_from_pointer(self, pointer: str) -> None:
        row = self.get_row_by_pointer(pointer)
        if row is None or row.get("is_header"):
            self.clear_editor()
            return
        self.is_syncing_editor = True
        self.key_value.setText(row["key"])
        self.kind_label.setText(f"kind: {row['kind']}")
        self.group_label.setText(f"group: {row.get('group', '') or '-'}")
        pointer_text = str(row.get("source_label", "")).strip()
        if pointer_text:
            pointer_text = f"{pointer_text} | {row['pointer']}"
        else:
            pointer_text = row["pointer"]
        self.pointer_label.setText(f"pointer: {pointer_text}")
        self.source_editor.setPlainText(row["source"])
        self.translation_editor.setPlainText(row["translation"])
        self.skip_toggle.setChecked(bool(row.get("skip", False)))
        self.is_syncing_editor = False
    def on_skip_toggle_changed(self, checked: bool) -> None:
        if self.is_syncing_editor:
            return
        row = self.get_row_by_pointer(self.current_pointer)
        if row is None:
            return
        row["skip"] = checked
        self._autosave_project()
        self.refresh_views(select_first=True)
        self.statusBar().showMessage("Updated skipped state.")

    def on_translation_editor_changed(self) -> None:
        if self.is_syncing_editor:
            return
        row = self.get_row_by_pointer(self.current_pointer)
        if row is None:
            return
        new_text = self.translation_editor.toPlainText()
        row["translation"] = new_text
        table = self.current_table()
        current_row = table.currentRow()
        if current_row >= 0:
            item = table.item(current_row, 1)
            if item is not None and item.text() != new_text:
                table.blockSignals(True)
                item.setText(new_text)
                table.blockSignals(False)
        self._autosave_project()
        self._update_stats_only()

    def on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        row = self.get_row_by_pointer(item.data(Qt.UserRole))
        if row is None:
            return
        row["translation"] = item.text()
        if row["pointer"] == self.current_pointer and self.translation_editor.toPlainText() != item.text():
            self.is_syncing_editor = True
            self.translation_editor.setPlainText(item.text())
            self.is_syncing_editor = False
        self._autosave_project()
        self._update_stats_only()
        self.statusBar().showMessage("Updated row.")

    def _selected_pointers(self, table: CopyableTableWidget | None = None) -> list[str]:
        table = table or self.current_table()
        pointers: list[str] = []
        for model_index in sorted(table.selectionModel().selectedRows(), key=lambda item: item.row()):
            pointer = self.pointer_at_row(table, model_index.row())
            if pointer:
                pointers.append(pointer)
        return pointers

    def set_skip_for_selected(self, skip: bool) -> None:
        pointers = self._selected_pointers()
        if not pointers:
            QMessageBox.information(self, "No selection", "Select one or more rows first.")
            return
        for pointer in pointers:
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                row["skip"] = skip
        self._autosave_project()
        self.refresh_views(select_first=True)
        self.statusBar().showMessage(f"Updated skipped state on {len(pointers)} rows.")

    def apply_group_to_selected(self) -> None:
        pointers = self._selected_pointers()
        if not pointers:
            QMessageBox.information(self, "No selection", "Select one or more rows first.")
            return
        group = self.group_apply_combo.currentText().strip()
        for pointer in pointers:
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                row["group"] = group
        self._refresh_group_controls(self.group_filter_combo.currentText() or "All groups")
        self._autosave_project()
        self.refresh_views()
        self.statusBar().showMessage(f"Applied group to {len(pointers)} rows.")

    def clear_group_for_selected(self) -> None:
        pointers = self._selected_pointers()
        if not pointers:
            QMessageBox.information(self, "No selection", "Select one or more rows first.")
            return
        for pointer in pointers:
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                row["group"] = ""
        self._refresh_group_controls(self.group_filter_combo.currentText() or "All groups")
        self._autosave_project()
        self.refresh_views()
        self.statusBar().showMessage(f"Cleared group on {len(pointers)} rows.")

    def copy_current_key(self) -> None:
        QApplication.clipboard().setText(self.key_value.text())
        self.statusBar().showMessage("Copied key.")

    def copy_selected_rows(self, mode: str) -> None:
        copied = self.current_table().copy_rows(mode)
        if copied:
            self.statusBar().showMessage(f"Copied selected {mode}.")

    def apply_batch_paste(self) -> None:
        pointers = self._selected_pointers()
        if not pointers:
            QMessageBox.information(self, "No selection", "Select rows on the left first.")
            return
        lines = self.batch_paste_editor.toPlainText().splitlines()
        if not lines:
            QMessageBox.information(self, "No pasted text", "Paste one translation per line first.")
            return
        if len(lines) != len(pointers):
            QMessageBox.critical(self, "Line count mismatch", f"Selected rows: {len(pointers)}\nPasted lines: {len(lines)}\nThey must match.")
            return
        for pointer, text in zip(pointers, lines):
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                row["translation"] = text
        self._autosave_project()
        self.refresh_views(select_first=True)
        self.statusBar().showMessage(f"Applied batch paste to {len(pointers)} rows.")

    def _find_scope_rows(self) -> list[dict[str, Any]]:
        pointers = self._selected_pointers()
        if len(pointers) > 1:
            scoped_rows: list[dict[str, Any]] = []
            for pointer in pointers:
                row = self.get_row_by_pointer(pointer)
                if row is not None:
                    scoped_rows.append(row)
            return scoped_rows

        table = self.current_table()
        visible: list[dict[str, Any]] = []
        for row_index in range(table.rowCount()):
            pointer = self.pointer_at_row(table, row_index)
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                visible.append(row)
        return visible

    def _find_next_match(self, needle: str) -> dict[str, Any] | None:
        rows = self._find_scope_rows()
        if not rows:
            return None

        start_index = 0
        current_row = self.get_row_by_pointer(self.current_pointer)
        if current_row is not None:
            for index, row in enumerate(rows):
                if row["pointer"] == current_row["pointer"]:
                    start_index = index + 1
                    break

        ordered = rows[start_index:] + rows[:start_index]
        for row in ordered:
            if needle in row["key"] or needle in row["source"] or needle in row["translation"]:
                return row
        return None

    def find_next(self) -> None:
        needle = self.find_input.text()
        if not needle:
            return

        match = self._find_next_match(needle)
        if match is None:
            QMessageBox.information(self, "Find", "No matches in the current scope.")
            return

        self.last_find_pointer = match["pointer"]
        self.select_pointer(match["pointer"], self.current_table())
        self.statusBar().showMessage("Found next match.")

    def replace_next(self) -> None:
        needle = self.find_input.text()
        replacement = self.replace_input.text()
        if not needle:
            return

        current_row = self.get_row_by_pointer(self.current_pointer)
        if current_row is not None and needle in current_row["translation"]:
            current_row["translation"] = current_row["translation"].replace(needle, replacement, 1)
            self.load_editor_from_pointer(current_row["pointer"])
            self._autosave_project()
            self._update_stats_only()
            self.statusBar().showMessage("Replaced in current row.")
            return

        match = self._find_next_match(needle)
        if match is None:
            QMessageBox.information(self, "Replace", "No matches in the current scope.")
            return

        self.last_find_pointer = match["pointer"]
        self.select_pointer(match["pointer"], self.current_table())
        self.statusBar().showMessage("Moved to next match. Click Replace Next again to replace it.")

    def replace_all(self) -> None:
        needle = self.find_input.text()
        replacement = self.replace_input.text()
        if not needle:
            return

        changed = 0
        for row in self._find_scope_rows():
            count_before = row["translation"].count(needle)
            if count_before:
                row["translation"] = row["translation"].replace(needle, replacement)
                changed += count_before

        self._autosave_project()
        self.refresh_views()
        self.statusBar().showMessage(f"Replaced {changed} occurrence(s) in current scope.")
    def _auto_scope_rows(self) -> list[dict[str, Any]]:
        pointers = self._selected_pointers()
        if pointers:
            rows: list[dict[str, Any]] = []
            for pointer in pointers:
                row = self.get_row_by_pointer(pointer)
                if row is not None:
                    rows.append(row)
            return rows
        return self._find_scope_rows()

    def _auto_block_prefix(self, row: dict[str, Any] | None = None) -> str:
        if row is not None:
            source_label = str(row.get("source_label", "")).strip()
            if source_label:
                base = Path(source_label).stem
            else:
                source_path = str(row.get("source_path", "")).strip()
                base = Path(source_path).stem if source_path else "rows"
        else:
            base = Path(self.source_path).stem if self.source_path else "rows"
        prefix = re.sub(r"[^0-9A-Za-z_-]+", "_", base).strip("_")
        return prefix or "rows"
    def _auto_block_id(self, row: dict[str, Any], index: int) -> str:
        return f"{self._auto_block_prefix(row)}-{index:04d}"
    def _auto_block_pairs(self, rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        counts: dict[str, int] = {}
        pairs: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            prefix = self._auto_block_prefix(row)
            counts[prefix] = counts.get(prefix, 0) + 1
            pairs.append((self._auto_block_id(row, counts[prefix]), row))
        return pairs
    def _format_numbered_blocks(self, rows: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for block_id, row in self._auto_block_pairs(rows):
            blocks.append(f"[{block_id}]\n{row['source']}")
        return "\n\n".join(blocks)

    def _build_auto_prompt_text(self, rows: list[dict[str, Any]]) -> str:
        numbered_blocks = self._format_numbered_blocks(rows)
        return (
            "Translate the following game text into Simplified Chinese.\n"
            "Rules:\n"
            "1. Keep the same anchor blocks.\n"
            "2. Return the result in the exact same block format: [file-0001], [file-0002] ...\n"
            "3. Do not add explanations, comments, or extra headings.\n"
            "4. Preserve placeholders, symbols, punctuation, and line breaks inside each block.\n"
            "5. If a line should stay unchanged, return it unchanged.\n"
            "6. Do not merge or reorder block ids.\n\n"
            f"Input blocks:\n{numbered_blocks}"
        )
    def _parse_numbered_blocks(self, text: str) -> dict[str, str]:
        lines = text.splitlines()
        results: dict[str, str] = {}
        current_id: str | None = None
        current_lines: list[str] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r")
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped:
                block_id = stripped.split("]", 1)[0][1:].strip()
                if block_id:
                    if current_id is not None:
                        results[current_id] = "\n".join(current_lines).strip()
                    current_id = block_id
                    current_lines = []
                    remainder = stripped.split("]", 1)[1].lstrip()
                    if remainder:
                        current_lines.append(remainder)
                    continue
            if current_id is not None:
                current_lines.append(line)
        if current_id is not None:
            results[current_id] = "\n".join(current_lines).strip()
        return results
    def _remember_auto_scope(self, rows: list[dict[str, Any]]) -> None:
        self.last_auto_scope_pointers = [str(row["pointer"]) for row in rows]

    def _saved_auto_scope_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pointer in self.last_auto_scope_pointers:
            row = self.get_row_by_pointer(pointer)
            if row is not None:
                rows.append(row)
        return rows

    def _normalize_anchor_id(self, block_id: str) -> str:
        return block_id.strip().lower()

    def _numeric_anchor_suffix(self, block_id: str) -> str | None:
        match = re.search(r"(\d+)$", block_id.strip())
        if match is None:
            return None
        return str(int(match.group(1)))

    def _apply_anchored_translations(self, rows: list[dict[str, Any]], translated_blocks: dict[str, str]) -> tuple[int, list[str]]:
        exact_map = {
            self._normalize_anchor_id(block_id): value
            for block_id, value in translated_blocks.items()
            if block_id.strip()
        }
        numeric_map: dict[str, str] = {}
        numeric_duplicates: set[str] = set()
        for block_id, value in translated_blocks.items():
            numeric_id = self._numeric_anchor_suffix(block_id)
            if numeric_id is None:
                continue
            if numeric_id in numeric_map:
                numeric_duplicates.add(numeric_id)
            else:
                numeric_map[numeric_id] = value
        for numeric_id in numeric_duplicates:
            numeric_map.pop(numeric_id, None)

        applied = 0
        missing: list[str] = []
        for block_id, row in self._auto_block_pairs(rows):
            exact_key = self._normalize_anchor_id(block_id)
            translated_text = exact_map.get(exact_key)
            if translated_text is None:
                numeric_id = self._numeric_anchor_suffix(block_id)
                if numeric_id is not None:
                    translated_text = numeric_map.get(numeric_id)
            if translated_text is None:
                missing.append(block_id)
                continue
            row["translation"] = translated_text
            applied += 1
        return applied, missing
    def preview_auto_scope(self) -> None:
        rows = self._auto_scope_rows()
        scope_name = "selected rows" if self._selected_pointers() else f"current {self._view_name()} view"
        self.auto_summary_label.setText(f"Auto scope: {len(rows)} rows from {scope_name}.")
        self.auto_web_prompt.setPlainText(self._build_auto_prompt_text(rows) if rows else "")

    def copy_auto_source_lines(self) -> None:
        rows = self._auto_scope_rows()
        if not rows:
            QMessageBox.information(self, "Auto", "No rows in the current auto scope.")
            return
        self._remember_auto_scope(rows)
        payload = self._format_numbered_blocks(rows)
        QApplication.clipboard().setText(payload)
        self.auto_web_prompt.setPlainText(self._build_auto_prompt_text(rows))
        self.auto_summary_label.setText(f"Copied numbered source blocks for {len(rows)} rows.")
        self.statusBar().showMessage("Copied numbered source blocks.")
    def copy_auto_web_prompt(self) -> None:
        rows = self._auto_scope_rows()
        if not rows:
            QMessageBox.information(self, "Auto", "No rows in the current auto scope.")
            return
        self._remember_auto_scope(rows)
        prompt = self._build_auto_prompt_text(rows)
        self.auto_web_prompt.setPlainText(prompt)
        QApplication.clipboard().setText(prompt)
        self.auto_summary_label.setText(f"Copied web prompt for {len(rows)} rows.")
        self.statusBar().showMessage("Copied web prompt.")
    def apply_auto_pasted_results(self) -> None:
        rows = self._saved_auto_scope_rows() or self._auto_scope_rows()
        if not rows:
            QMessageBox.information(self, "Auto", "No rows in the current auto scope.")
            return
        translated_blocks = self._parse_numbered_blocks(self.auto_paste_editor.toPlainText())
        if not translated_blocks:
            QMessageBox.critical(self, "Block parse failed", "No anchored blocks were found in the pasted text.")
            return
        applied, missing = self._apply_anchored_translations(rows, translated_blocks)
        if applied == 0:
            QMessageBox.critical(self, "Block match failed", "No matching anchor ids were found for the current scope.")
            return
        self._autosave_project()
        self.refresh_views()
        self.statusBar().showMessage(f"Applied auto results to {applied} rows.")
        self.auto_summary_label.setText(f"Applied pasted results to {applied} rows. Missing: {len(missing)}")
    def translate_via_api(self) -> None:
        if self.api_request_active:
            self.statusBar().showMessage("API translation is already running.")
            return

        rows = self._auto_scope_rows()
        scope_name = "selected rows" if self._selected_pointers() else f"current {self._view_name()} view"
        self.auto_summary_label.setText(f"API scope: {len(rows)} rows from {scope_name}.")
        if not rows:
            self.auto_web_prompt.setPlainText("No rows in the current auto scope. Select rows on the left, or switch to a view that has rows.")
            QMessageBox.information(self, "API Translate", "No rows in the current auto scope.")
            return

        config = self._current_api_config()
        if not config["base_url"] or not config["model"] or not config["api_key"]:
            self.auto_web_prompt.setPlainText("Base URL, Model, and API Key are required before API translation can start.")
            QMessageBox.critical(self, "API Translate", "Base URL, Model, and API Key are required.")
            return

        try:
            temperature = float(config["temperature"])
        except ValueError:
            self.auto_web_prompt.setPlainText(f"Invalid temperature: {config['temperature']}")
            QMessageBox.critical(self, "API Translate", "Temperature must be a number.")
            return

        self._remember_auto_scope(rows)
        prompt = self._build_auto_prompt_text(rows)
        payload = {
            "model": config["model"],
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": "You are a translation engine. Return only the numbered translated blocks."},
                {"role": "user", "content": prompt},
            ],
        }

        base_url = config["base_url"].rstrip("/")
        endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        pointers = [str(row["pointer"]) for row in rows]

        self.api_request_active = True
        self.translate_api_button.setEnabled(False)
        self.auto_web_prompt.setPlainText(
            f"Sending API request to {endpoint}\nRows: {len(rows)}\nModel: {config['model']}"
        )
        self.auto_summary_label.setText(f"Sending API request for {len(rows)} rows...")
        self.statusBar().showMessage(f"Translating {len(rows)} rows via API...")
        self.api_poll_timer.start()
        self.api_request_thread = threading.Thread(
            target=self._run_api_request,
            args=(endpoint, payload, config["api_key"], pointers),
            daemon=True,
        )
        self.api_request_thread.start()
    def _run_api_request(self, endpoint: str, payload: dict[str, Any], api_key: str, pointers: list[str]) -> None:
        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=180) as response:
                body = response.read().decode("utf-8")
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
            self.api_result_queue.put(("success", {"content": content, "pointers": pointers}))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.api_result_queue.put(("http_error", {"message": f"HTTP {exc.code}", "detail": detail}))
        except Exception as exc:
            self.api_result_queue.put(("error", {"message": str(exc)}))

    def _finish_api_request(self) -> None:
        self.api_request_active = False
        self.api_request_thread = None
        self.api_poll_timer.stop()
        if hasattr(self, "translate_api_button"):
            self.translate_api_button.setEnabled(True)

    def _poll_api_results(self) -> None:
        handled = False
        while not self.api_result_queue.empty():
            handled = True
            kind, payload = self.api_result_queue.get()
            if kind == "success":
                self._handle_api_success(payload)
            else:
                self._handle_api_error(payload)
        if handled and not self.api_request_active:
            self.api_poll_timer.stop()

    def _handle_api_success(self, payload: dict[str, Any]) -> None:
        self._finish_api_request()
        if self.is_closing:
            return
        pointers = [str(pointer) for pointer in payload.get("pointers", [])]
        rows = [row for pointer in pointers if (row := self.get_row_by_pointer(pointer)) is not None]
        content = str(payload.get("content", ""))
        translated_blocks = self._parse_numbered_blocks(content)
        if not translated_blocks:
            self.auto_web_prompt.setPlainText(content)
            self.auto_summary_label.setText("API returned no anchored blocks.")
            QMessageBox.critical(self, "API Translate", "No anchored blocks were found in the API response. The raw response was placed in the preview area.")
            return
        applied, missing = self._apply_anchored_translations(rows, translated_blocks)
        if applied == 0:
            self.auto_web_prompt.setPlainText(content)
            self.auto_summary_label.setText("API returned unmatched anchor ids.")
            QMessageBox.critical(self, "API Translate", "No matching anchor ids were found in the API response. The raw response was placed in the preview area.")
            return
        self._autosave_project()
        self.refresh_views()
        self.auto_web_prompt.setPlainText(content)
        self.auto_summary_label.setText(f"API translation applied to {applied} rows. Missing: {len(missing)}")
        self.statusBar().showMessage(f"API translation applied to {applied} rows.")
    def _handle_api_error(self, payload: dict[str, Any]) -> None:
        self._finish_api_request()
        if self.is_closing:
            return
        message = str(payload.get("message", "API request failed."))
        detail = str(payload.get("detail", ""))
        self.auto_web_prompt.setPlainText(detail or message)
        self.auto_summary_label.setText("API request failed.")
        QMessageBox.critical(self, "API Translate", f"{message}\n{detail}".strip())

    def save_current_translation(self) -> None:
        row = self.get_row_by_pointer(self.current_pointer)
        if row is None:
            return
        row["translation"] = self.translation_editor.toPlainText()
        self._autosave_project()
        self.statusBar().showMessage("Saved current item.")
        self.refresh_views()

    def reset_current_translation(self) -> None:
        row = self.get_row_by_pointer(self.current_pointer)
        if row is None:
            return
        row["translation"] = row["source"]
        self.is_syncing_editor = True
        self.translation_editor.setPlainText(row["translation"])
        self.is_syncing_editor = False
        self._autosave_project()
        self.refresh_views()
        self.statusBar().showMessage("Reset current item.")

    def _update_stats_only(self) -> None:
        text_count = sum(1 for row in self.rows if classify_text(row["source"]) == "text")
        self.stats_label.setText(
            f"Files: {len(self.source_documents)} | Text items: {text_count} | Worklist: {len(self.visible_rows('worklist'))} | Translated: {len(self.visible_rows('translated'))} | Skipped: {len(self.visible_rows('skipped'))} | Encoding: {self.source_encoding}"
        )
    def export_json(self) -> None:
        if self.source_data is None or not self.source_documents:
            QMessageBox.critical(self, "Save failed", "Load a source or project first.")
            return
        if len(self.source_documents) == 1:
            document = self.source_documents[0]
            default_name = Path(document["path"]).name
            initial_dir = str(Path(document["path"]).parent)
            if document["mode"] == MODE_EQUALS:
                file_filter = "Text Files (*.txt *.ini *.cfg *.lang *.properties);;All Files (*.*)"
            elif document["mode"] == MODE_XML:
                file_filter = "XML Files (*.xml);;All Files (*.*)"
            else:
                file_filter = "JSON Files (*.json);;All Files (*.*)"
            output_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save Output",
                str(Path(initial_dir) / default_name),
                file_filter,
            )
            if not output_path:
                return
            try:
                export_rows = [row for row in self.rows if not row.get("skip") and row.get("source_id") == document["id"]]
                entry_rows = []
                for row in export_rows:
                    row_copy = dict(row)
                    row_copy["pointer"] = row_copy.get("local_pointer", row_copy["pointer"])
                    entry_rows.append(row_copy)
                translated = apply_translations(document["data"], rows_to_entries(entry_rows), mode=document["mode"])
                save_source_file(output_path, translated, document["mode"], encoding=document["encoding"])
                self.statusBar().showMessage(f"Saved to {output_path}")
            except Exception as exc:
                QMessageBox.critical(self, "Save failed", str(exc))
            return

        initial_dir = str(Path(self.source_documents[0]["path"]).parent)
        output_dir = QFileDialog.getExistingDirectory(self, "Choose Output Folder", initial_dir)
        if not output_dir:
            return
        try:
            for document in self.source_documents:
                export_rows = [row for row in self.rows if not row.get("skip") and row.get("source_id") == document["id"]]
                entry_rows = []
                for row in export_rows:
                    row_copy = dict(row)
                    row_copy["pointer"] = row_copy.get("local_pointer", row_copy["pointer"])
                    entry_rows.append(row_copy)
                translated = apply_translations(document["data"], rows_to_entries(entry_rows), mode=document["mode"])
                output_path = Path(output_dir) / Path(document["path"]).name
                save_source_file(output_path, translated, document["mode"], encoding=document["encoding"])
            self.statusBar().showMessage(f"Saved {len(self.source_documents)} files to {output_dir}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_all_tables()
    def clear_editor(self) -> None:
        self.current_pointer = None
        if not hasattr(self, "key_value"):
            return
        self.is_syncing_editor = True
        self.key_value.clear()
        self.kind_label.setText("kind: -")
        self.group_label.setText("group: -")
        self.pointer_label.setText("pointer: -")
        self.source_editor.clear()
        self.translation_editor.clear()
        self.skip_toggle.blockSignals(True)
        self.skip_toggle.setChecked(False)
        self.skip_toggle.blockSignals(False)
        self.is_syncing_editor = False
def main() -> None:
    app = QApplication([])
    window = TranslatorWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()



























