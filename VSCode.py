import sys
import re
import subprocess
import os
import json
from PyQt5.QtCore import (Qt, QDir, QRegExp, QSize, QFileInfo, QProcess,
                           QTimer, QSettings)
from PyQt5.QtGui import (QColor, QSyntaxHighlighter, QTextCharFormat, QPainter,
                         QTextFormat, QPalette, QTextCursor, QFont, QKeySequence,
                         QFontMetrics)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QTreeView, QPlainTextEdit,
    QTabWidget, QFileSystemModel, QVBoxLayout, QWidget, QAction,
    QFileDialog, QMessageBox, QInputDialog, QLineEdit, QDialog, QLabel, QPushButton,
    QHBoxLayout, QTextEdit, QCompleter, QListView, QMenu, QTextBrowser, QListWidget,
    QListWidgetItem, QStatusBar, QToolBar, QShortcut, QFontDialog, QColorDialog,
    QActionGroup, QCheckBox
)

# ───────────────────────────── 搜尋對話框 ─────────────────────────────
class SearchDialog(QDialog):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setWindowTitle("搜尋文字")
        layout = QVBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入要搜尋的文字…")
        self.result_list = QListWidget()
        self.search_button = QPushButton("搜尋")
        self.search_button.clicked.connect(self.find_text)
        layout.addWidget(QLabel("關鍵字："))
        layout.addWidget(self.search_input)
        layout.addWidget(self.search_button)
        layout.addWidget(self.result_list)
        self.setLayout(layout)

    def find_text(self):
        self.result_list.clear()
        keyword = self.search_input.text()
        if not keyword:
            return
        parent = self.parentWidget()
        if not parent:
            parent = self.editor.parentWidget()
        if not hasattr(parent, 'parentWidget'):
            return
        main_window = parent.parentWidget()
        if not main_window:
            return
        for tab_index in range(main_window.tabs.count()):
            tab = main_window.tabs.widget(tab_index)
            editor = main_window.editor_widgets.get(tab)
            if editor:
                lines = editor.toPlainText().split('\n')
                for line_num, line in enumerate(lines, start=1):
                    if keyword in line:
                        file_name = getattr(tab, 'file_path', '未命名')
                        file_name = QFileInfo(file_name).fileName()
                        item_text = f"{file_name}: 第 {line_num} 行: {line.strip()}"
                        item = QListWidgetItem(item_text)
                        item.setData(Qt.UserRole, (tab_index, line_num))
                        self.result_list.addItem(item)
        self.result_list.itemClicked.connect(lambda item: self.go_to_result(item, main_window))

    def go_to_result(self, item, main_window):
        tab_index, line_num = item.data(Qt.UserRole)
        main_window.tabs.setCurrentIndex(tab_index)
        editor = main_window.editor_widgets[main_window.tabs.widget(tab_index)]
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for _ in range(line_num - 1):
            cursor.movePosition(QTextCursor.Down)
        editor.setTextCursor(cursor)
        editor.setFocus()

# ───────────────────────────── 取代對話框 ─────────────────────────────
class ReplaceDialog(QDialog):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setWindowTitle("取代文字")
        layout = QVBoxLayout()
        self.search_input = QLineEdit()
        self.replace_input = QLineEdit()
        self.replace_button = QPushButton("全部取代")
        self.replace_button.clicked.connect(self.replace_text)
        layout.addWidget(QLabel("搜尋："))
        layout.addWidget(self.search_input)
        layout.addWidget(QLabel("取代為："))
        layout.addWidget(self.replace_input)
        layout.addWidget(self.replace_button)
        self.setLayout(layout)

    def replace_text(self):
        search = self.search_input.text()
        replace = self.replace_input.text()
        if search:
            text = self.editor.toPlainText().replace(search, replace)
            self.editor.setPlainText(text)

# ───────────────────────────── 終端機 ─────────────────────────────────
class TerminalWidget(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 13px;")
        self.process = QProcess()
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.start("bash")
        self.append("$ ")

    def read_output(self):
        data = self.process.readAllStandardOutput().data().decode(errors='replace')
        self.insertPlainText(data)
        self.append("$ ")

    def read_error(self):
        data = self.process.readAllStandardError().data().decode(errors='replace')
        self.setTextColor(QColor("#f48771"))
        self.insertPlainText(data)
        self.setTextColor(QColor("#d4d4d4"))
        self.append("$ ")

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.movePosition(QTextCursor.StartOfLine, QTextCursor.KeepAnchor)
            line = cursor.selectedText().lstrip("$ ").strip()
            super().keyPressEvent(event)
            if line:
                self.process.write((line + "\n").encode())
        else:
            super().keyPressEvent(event)

# ───────────────────────────── 行號區域 ───────────────────────────────
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.lineNumberAreaWidth(), 0)

    def paintEvent(self, event):
        self.editor.lineNumberAreaPaintEvent(event)

# ───────────────────────────── 程式碼編輯器 ───────────────────────────
class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.highlighter = PythonHighlighter(self.document())
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.blockCountChanged.connect(self.updateLineNumberAreaWidth)
        self.updateRequest.connect(self.updateLineNumberArea)
        self.lineNumberArea = LineNumberArea(self)
        self.updateLineNumberAreaWidth(0)
        self.highlight_current_line()

        keywords = [
            'def', 'class', 'import', 'from', 'return', 'if', 'else', 'elif',
            'while', 'for', 'in', 'try', 'except', 'finally', 'with', 'as',
            'pass', 'break', 'continue', 'lambda', 'True', 'False', 'None',
            'and', 'or', 'not', 'is', 'print', 'len', 'range', 'enumerate',
            'open', 'self', 'super', '__init__'
        ]
        self.completer = QCompleter(keywords)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.activated.connect(self.insert_completion)
        self.cursorPositionChanged.connect(self.highlight_brackets)

    def insert_completion(self, text):
        tc = self.textCursor()
        extra = len(text) - len(self.completer.completionPrefix())
        tc.movePosition(QTextCursor.Left)
        tc.movePosition(QTextCursor.EndOfWord)
        tc.insertText(text[-extra:])
        self.setTextCursor(tc)

    def keyPressEvent(self, event):
        if self.completer.popup().isVisible():
            if event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab,
                               Qt.Key_Escape, Qt.Key_Backtab):
                event.ignore()
                return

        if event.key() == Qt.Key_Tab:
            self.insertPlainText("    ")
            return

        pairs = {'(': ')', '[': ']', '{': '}', '"': '"', "'": "'"}
        if event.text() in pairs:
            super().keyPressEvent(event)
            self.insertPlainText(pairs[event.text()])
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.Left)
            self.setTextCursor(cursor)
            return

        super().keyPressEvent(event)

        if event.text().isalpha() or event.text() == '_':
            cursor = self.textCursor()
            cursor.select(QTextCursor.WordUnderCursor)
            prefix = cursor.selectedText()
            if prefix != self.completer.completionPrefix():
                self.completer.setCompletionPrefix(prefix)
                self.completer.popup().setCurrentIndex(
                    self.completer.completionModel().index(0, 0))
            if len(prefix) >= 2:
                cr = self.cursorRect()
                cr.setWidth(self.completer.popup().sizeHintForColumn(0)
                            + self.completer.popup().verticalScrollBar().sizeHint().width())
                self.completer.complete(cr)
            else:
                self.completer.popup().hide()
        else:
            self.completer.popup().hide()

    def highlight_brackets(self):
        selections = []
        if not self.isReadOnly():
            sel = QPlainTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(Qt.yellow).lighter(160))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)

        cursor = self.textCursor()
        doc = self.document()
        pos = cursor.position()
        open_br = "([{"
        close_br = ")]}"
        pairs_map = {'(': ')', '[': ']', '{': '}', ')': '(', ']': '[', '}': '{'}
        ch = doc.characterAt(pos)
        if ch not in open_br + close_br:
            ch = doc.characterAt(pos - 1)
            pos -= 1
        if ch in open_br + close_br:
            match_pos = self._find_matching_bracket(doc, pos, ch, pairs_map)
            if match_pos >= 0:
                fmt = QTextCharFormat()
                fmt.setBackground(QColor("#3a3a5c"))
                fmt.setForeground(QColor("#ffcc00"))
                for p in (pos, match_pos):
                    sel = QPlainTextEdit.ExtraSelection()
                    sel.format = fmt
                    c = self.textCursor()
                    c.setPosition(p)
                    c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
                    sel.cursor = c
                    selections.append(sel)
        self.setExtraSelections(selections)

    def _find_matching_bracket(self, doc, pos, ch, pairs_map):
        target = pairs_map.get(ch, '')
        direction = 1 if ch in "([{" else -1
        depth = 0
        i = pos
        length = doc.characterCount()
        while 0 <= i < length:
            c = doc.characterAt(i)
            if c == ch:
                depth += 1
            elif c == target:
                depth -= 1
                if depth == 0:
                    return i
            i += direction
        return -1

    def highlight_current_line(self):
        self.highlight_brackets()

    def lineNumberAreaWidth(self):
        digits = len(str(max(1, self.blockCount())))
        return 6 + self.fontMetrics().horizontalAdvance('9') * digits

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.lineNumberArea.setGeometry(cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height())

    def updateLineNumberAreaWidth(self, _):
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)

    def updateLineNumberArea(self, rect, dy):
        if dy:
            self.lineNumberArea.scroll(0, dy)
        else:
            self.lineNumberArea.update(0, rect.y(), self.lineNumberArea.width(), rect.height())

    def lineNumberAreaPaintEvent(self, event):
        painter = QPainter(self.lineNumberArea)
        painter.fillRect(event.rect(), QColor("#2d2d2d"))
        block = self.firstVisibleBlock()
        blockNumber = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        current_line = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(blockNumber + 1)
                painter.setPen(QColor("#ffffff") if blockNumber == current_line else QColor("#858585"))
                painter.drawText(0, top, self.lineNumberArea.width() - 4,
                                 self.fontMetrics().height(), Qt.AlignRight, number)
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            blockNumber += 1

# ───────────────────────────── Python 語法高亮 ────────────────────────
class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.highlightingRules = []

        kwFmt = QTextCharFormat()
        kwFmt.setForeground(QColor("#569cd6"))
        kwFmt.setFontWeight(QFont.Bold)
        for word in ['def', 'class', 'if', 'elif', 'else', 'try', 'except', 'finally',
                     'while', 'for', 'in', 'import', 'from', 'as', 'return', 'with', 'pass',
                     'break', 'continue', 'and', 'or', 'not', 'is', 'lambda', 'True', 'False', 'None']:
            self.highlightingRules.append((QRegExp(f"\\b{word}\\b"), kwFmt))

        builtinFmt = QTextCharFormat()
        builtinFmt.setForeground(QColor("#dcdcaa"))
        for word in ['print', 'len', 'range', 'enumerate', 'open', 'type',
                     'isinstance', 'list', 'dict', 'set', 'tuple', 'int', 'str',
                     'float', 'bool', 'super', 'self']:
            self.highlightingRules.append((QRegExp(f"\\b{word}\\b"), builtinFmt))

        decorFmt = QTextCharFormat()
        decorFmt.setForeground(QColor("#c586c0"))
        self.highlightingRules.append((QRegExp("@\\w+"), decorFmt))

        numFmt = QTextCharFormat()
        numFmt.setForeground(QColor("#b5cea8"))
        self.highlightingRules.append((QRegExp("\\b[0-9]+\\.?[0-9]*\\b"), numFmt))

        strFmt = QTextCharFormat()
        strFmt.setForeground(QColor("#ce9178"))
        self.highlightingRules.append((QRegExp('"[^"\\\\]*(\\\\.[^"\\\\]*)*"'), strFmt))
        self.highlightingRules.append((QRegExp("'[^'\\\\]*(\\\\.[^'\\\\]*)*'"), strFmt))

        commentFmt = QTextCharFormat()
        commentFmt.setForeground(QColor("#6a9955"))
        commentFmt.setFontItalic(True)
        self.highlightingRules.append((QRegExp("#.*"), commentFmt))

        funcFmt = QTextCharFormat()
        funcFmt.setForeground(QColor("#dcdcaa"))
        self.highlightingRules.append((QRegExp("\\bdef\\s+(\\w+)"), funcFmt))

        classFmt = QTextCharFormat()
        classFmt.setForeground(QColor("#4ec9b0"))
        self.highlightingRules.append((QRegExp("\\bclass\\s+(\\w+)"), classFmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.highlightingRules:
            index = pattern.indexIn(text)
            while index >= 0:
                length = pattern.matchedLength()
                self.setFormat(index, length, fmt)
                index = pattern.indexIn(text, index + length)

# ───────────────────────────── JavaScript 語法高亮 ────────────────────
class JSHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.highlightingRules = []
        kwFmt = QTextCharFormat()
        kwFmt.setForeground(QColor("#569cd6"))
        kwFmt.setFontWeight(QFont.Bold)
        for word in ['var', 'let', 'const', 'function', 'return', 'if', 'else',
                     'for', 'while', 'class', 'import', 'export', 'from', 'new',
                     'this', 'true', 'false', 'null', 'undefined', 'typeof',
                     'async', 'await', 'try', 'catch', 'finally']:
            self.highlightingRules.append((QRegExp(f"\\b{word}\\b"), kwFmt))
        strFmt = QTextCharFormat()
        strFmt.setForeground(QColor("#ce9178"))
        for pattern in ['"[^"]*"', "'[^']*'", '`[^`]*`']:
            self.highlightingRules.append((QRegExp(pattern), strFmt))
        numFmt = QTextCharFormat()
        numFmt.setForeground(QColor("#b5cea8"))
        self.highlightingRules.append((QRegExp("\\b[0-9]+\\.?[0-9]*\\b"), numFmt))
        commentFmt = QTextCharFormat()
        commentFmt.setForeground(QColor("#6a9955"))
        commentFmt.setFontItalic(True)
        self.highlightingRules.append((QRegExp("//.*"), commentFmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.highlightingRules:
            idx = pattern.indexIn(text)
            while idx >= 0:
                self.setFormat(idx, pattern.matchedLength(), fmt)
                idx = pattern.indexIn(text, idx + pattern.matchedLength())

# ───────────────────────────── HTML 語法高亮 ──────────────────────────
class HTMLHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.highlightingRules = []
        tagFmt = QTextCharFormat()
        tagFmt.setForeground(QColor("#4ec9b0"))
        self.highlightingRules.append((QRegExp("<[^>]+>"), tagFmt))
        attrFmt = QTextCharFormat()
        attrFmt.setForeground(QColor("#9cdcfe"))
        self.highlightingRules.append((QRegExp("\\b\\w+(?==)"), attrFmt))
        valFmt = QTextCharFormat()
        valFmt.setForeground(QColor("#ce9178"))
        self.highlightingRules.append((QRegExp('"[^"]*"'), valFmt))
        commentFmt = QTextCharFormat()
        commentFmt.setForeground(QColor("#6a9955"))
        self.highlightingRules.append((QRegExp("<!--.*-->"), commentFmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.highlightingRules:
            idx = pattern.indexIn(text)
            while idx >= 0:
                self.setFormat(idx, pattern.matchedLength(), fmt)
                idx = pattern.indexIn(text, idx + pattern.matchedLength())

def get_highlighter_for_file(path, document):
    ext = QFileInfo(path).suffix().lower()
    if ext in ('js', 'ts', 'jsx', 'tsx'):
        return JSHighlighter(document)
    elif ext in ('html', 'htm', 'xml'):
        return HTMLHighlighter(document)
    else:
        return PythonHighlighter(document)

# ───────────────────────────── Git 對話框 ─────────────────────────────
class GitDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Git 操作")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setStyleSheet("background:#1e1e1e; color:#d4d4d4; font-family:monospace;")

        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("輸入 Git 指令（如 status, commit -m 'msg', log --oneline）")
        self.command_input.returnPressed.connect(self.run_command)

        btn_layout = QHBoxLayout()
        for label, cmd in [("Status", "status"), ("Log", "log --oneline -10"),
                            ("Diff", "diff"), ("Pull", "pull"), ("Push", "push")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, c=cmd: self._quick_run(c))
            btn_layout.addWidget(btn)

        run_button = QPushButton("執行")
        run_button.clicked.connect(self.run_command)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("快速操作："))
        layout.addLayout(btn_layout)
        layout.addWidget(QLabel("自訂指令："))
        input_row = QHBoxLayout()
        input_row.addWidget(self.command_input)
        input_row.addWidget(run_button)
        layout.addLayout(input_row)
        layout.addWidget(QLabel("輸出："))
        layout.addWidget(self.output)
        self.setLayout(layout)

    def _quick_run(self, cmd):
        self.command_input.setText(cmd)
        self.run_command()

    def run_command(self):
        command = self.command_input.text().strip()
        if not command:
            return
        full_cmd = ["git"] + command.split()
        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True)
            self.output.append(f'<span style="color:#569cd6">$ git {command}</span>')
            if result.stdout:
                self.output.append(result.stdout)
            if result.stderr:
                self.output.append(f'<span style="color:#f48771">{result.stderr}</span>')
        except FileNotFoundError:
            self.output.append('<span style="color:#f48771">找不到 git，請確認已安裝 Git。</span>')

# ───────────────────────────── 主視窗 ─────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyEditor")
        self.setGeometry(100, 100, 1280, 800)
        self.editor_widgets = {}
        self.recent_files = []
        self.current_font_size = 13
        self.is_dark_theme = True
        self.settings = QSettings("PyEditor", "PyEditor")
        self._load_settings()
        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._setup_autosave()
        self._apply_theme()

    def _build_ui(self):
        self.splitter = QSplitter(Qt.Horizontal)
        self.file_model = QFileSystemModel()
        self.file_model.setRootPath(QDir.homePath())
        self.tree = QTreeView()
        self.tree.setModel(self.file_model)
        self.tree.setRootIndex(self.file_model.index(QDir.homePath()))
        self.tree.setColumnWidth(0, 200)
        self.tree.doubleClicked.connect(self.open_from_tree)
        self.tree.setMinimumWidth(200)
        self.tree.setMaximumWidth(300)

        center_splitter = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._update_status)
        self.terminal = TerminalWidget()
        self.terminal.setMaximumHeight(200)

        center_splitter.addWidget(self.tabs)
        center_splitter.addWidget(self.terminal)
        center_splitter.setSizes([600, 150])

        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(center_splitter)
        self.splitter.setSizes([220, 1060])
        self.setCentralWidget(self.splitter)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("檔案")
        self._add_action(file_menu, "新增檔案", self.new_file, "Ctrl+N")
        self._add_action(file_menu, "開啟檔案", self.open_file, "Ctrl+O")
        self.recent_menu = file_menu.addMenu("最近開啟")
        self._refresh_recent_menu()
        self._add_action(file_menu, "儲存", self.save_file, "Ctrl+S")
        self._add_action(file_menu, "另存新檔", self.save_file_as, "Ctrl+Shift+S")
        file_menu.addSeparator()
        self._add_action(file_menu, "離開", self.close, "Ctrl+Q")

        edit_menu = menubar.addMenu("編輯")
        self._add_action(edit_menu, "搜尋", self.open_search, "Ctrl+F")
        self._add_action(edit_menu, "取代", self.open_replace, "Ctrl+H")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "跳到指定行", self.goto_line, "Ctrl+L")
        self._add_action(edit_menu, "全選", lambda: self._current_editor() and self._current_editor().selectAll(), "Ctrl+A")

        view_menu = menubar.addMenu("檢視")
        self._add_action(view_menu, "放大字型", self.zoom_in, "Ctrl++")
        self._add_action(view_menu, "縮小字型", self.zoom_out, "Ctrl+-")
        self._add_action(view_menu, "重置字型大小", self.zoom_reset, "Ctrl+0")
        self._add_action(view_menu, "選擇字型", self.choose_font)
        view_menu.addSeparator()
        self._add_action(view_menu, "切換亮/暗主題", self.toggle_theme, "Ctrl+T")
        view_menu.addSeparator()
        self._add_action(view_menu, "切換側邊欄", self.toggle_sidebar, "Ctrl+B")
        self._add_action(view_menu, "切換終端機", self.toggle_terminal, "Ctrl+`")

        run_menu = menubar.addMenu("執行")
        self._add_action(run_menu, "執行 Python 檔案", self.run_python, "F5")

        git_menu = menubar.addMenu("Git")
        self._add_action(git_menu, "Git 操作面板", self.open_git_dialog, "Ctrl+Shift+G")

    def _build_toolbar(self):
        toolbar = QToolBar("主工具列")
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        for label, slot in [
            ("新增", self.new_file), ("開啟", self.open_file), ("儲存", self.save_file),
            ("|", None),
            ("搜尋", self.open_search), ("取代", self.open_replace),
            ("|", None),
            ("執行 ▶", self.run_python), ("Git", self.open_git_dialog),
            ("|", None),
            ("A+", self.zoom_in), ("A-", self.zoom_out), ("主題", self.toggle_theme),
        ]:
            if label == "|":
                toolbar.addSeparator()
            else:
                act = QAction(label, self)
                if slot:
                    act.triggered.connect(slot)
                toolbar.addAction(act)

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("行 1, 欄 1")
        self.file_label = QLabel("未命名")
        self.encoding_label = QLabel("UTF-8")
        self.status_bar.addWidget(self.file_label)
        self.status_bar.addPermanentWidget(self.encoding_label)
        self.status_bar.addPermanentWidget(self.status_label)

    def _setup_autosave(self):
        self.autosave_timer = QTimer()
        self.autosave_timer.setInterval(30000)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start()

    def _add_action(self, menu, name, slot, shortcut=None):
        action = QAction(name, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _current_tab(self):
        return self.tabs.currentWidget()

    def _current_editor(self):
        tab = self._current_tab()
        return self.editor_widgets.get(tab)

    def _new_editor(self):
        editor = CodeEditor()
        font = QFont("Consolas", self.current_font_size)
        editor.setFont(font)
        editor.setStyleSheet("background:#1e1e1e; color:#d4d4d4;")
        editor.cursorPositionChanged.connect(self._update_status)
        editor.document().modificationChanged.connect(self._on_modification_changed)
        return editor

    def _update_status(self):
        editor = self._current_editor()
        if editor:
            cursor = editor.textCursor()
            line = cursor.blockNumber() + 1
            col = cursor.columnNumber() + 1
            self.status_label.setText(f"行 {line}, 欄 {col}")
            tab = self._current_tab()
            path = getattr(tab, 'file_path', '未命名')
            self.file_label.setText(QFileInfo(path).fileName() if path != '未命名' else '未命名')

    def _on_modification_changed(self, modified):
        tab = self._current_tab()
        if tab:
            idx = self.tabs.indexOf(tab)
            title = self.tabs.tabText(idx).rstrip(" ●")
            self.tabs.setTabText(idx, title + (" ●" if modified else ""))

    def new_file(self):
        tab = QWidget()
        tab.file_path = '未命名'
        editor = self._new_editor()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        self.editor_widgets[tab] = editor
        self.tabs.addTab(tab, "未命名")
        self.tabs.setCurrentWidget(tab)

    def open_file(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "開啟檔案", "",
                "所有檔案 (*);;Python (*.py);;JavaScript (*.js);;HTML (*.html *.htm)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"無法開啟檔案：{e}")
            return
        tab = QWidget()
        tab.file_path = path
        editor = self._new_editor()
        editor.highlighter = get_highlighter_for_file(path, editor.document())
        editor.setPlainText(content)
        editor.document().setModified(False)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        self.editor_widgets[tab] = editor
        name = QFileInfo(path).fileName()
        self.tabs.addTab(tab, name)
        self.tabs.setCurrentWidget(tab)
        self._add_recent(path)

    def open_from_tree(self, index):
        path = self.file_model.filePath(index)
        if os.path.isfile(path):
            self.open_file(path)

    def save_file(self):
        tab = self._current_tab()
        if not tab:
            return
        path = getattr(tab, 'file_path', '未命名')
        if path == '未命名':
            self.save_file_as()
            return
        self._write_file(tab, path)

    def save_file_as(self):
        tab = self._current_tab()
        if not tab:
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存新檔", "",
            "所有檔案 (*);;Python (*.py);;JavaScript (*.js);;HTML (*.html)")
        if path:
            tab.file_path = path
            self._write_file(tab, path)
            self.tabs.setTabText(self.tabs.currentIndex(), QFileInfo(path).fileName())
            self._add_recent(path)

    def _write_file(self, tab, path):
        editor = self.editor_widgets.get(tab)
        if not editor:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(editor.toPlainText())
            editor.document().setModified(False)
            self.status_bar.showMessage(f"已儲存：{path}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"儲存失敗：{e}")

    def _autosave(self):
        for tab, editor in self.editor_widgets.items():
            path = getattr(tab, 'file_path', '未命名')
            if path != '未命名' and editor.document().isModified():
                self._write_file(tab, path)
                self.status_bar.showMessage("自動儲存完成", 2000)

    def close_tab(self, index):
        tab = self.tabs.widget(index)
        editor = self.editor_widgets.get(tab)
        if editor and editor.document().isModified():
            reply = QMessageBox.question(self, "確認", "檔案已修改，確定要關閉嗎？",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                return
        if tab in self.editor_widgets:
            del self.editor_widgets[tab]
        self.tabs.removeTab(index)

    def _add_recent(self, path):
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:10]
        self._refresh_recent_menu()
        self._save_settings()

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        if not self.recent_files:
            self.recent_menu.addAction("（無紀錄）").setEnabled(False)
        for path in self.recent_files:
            action = QAction(QFileInfo(path).fileName(), self)
            action.setToolTip(path)
            action.triggered.connect(lambda _, p=path: self.open_file(p))
            self.recent_menu.addAction(action)

    def open_search(self):
        editor = self._current_editor()
        if editor:
            dlg = SearchDialog(editor)
            dlg.setParent(self, Qt.Dialog)
            dlg.show()

    def open_replace(self):
        editor = self._current_editor()
        if editor:
            dlg = ReplaceDialog(editor)
            dlg.exec_()

    def goto_line(self):
        editor = self._current_editor()
        if not editor:
            return
        line, ok = QInputDialog.getInt(self, "跳到指定行", "行號：", 1, 1, editor.blockCount())
        if ok:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(line - 1):
                cursor.movePosition(QTextCursor.Down)
            editor.setTextCursor(cursor)
            editor.setFocus()

    def zoom_in(self):
        self.current_font_size = min(self.current_font_size + 1, 40)
        self._apply_font_size()

    def zoom_out(self):
        self.current_font_size = max(self.current_font_size - 1, 6)
        self._apply_font_size()

    def zoom_reset(self):
        self.current_font_size = 13
        self._apply_font_size()

    def _apply_font_size(self):
        for editor in self.editor_widgets.values():
            font = editor.font()
            font.setPointSize(self.current_font_size)
            editor.setFont(font)
        self._save_settings()

    def choose_font(self):
        editor = self._current_editor()
        if not editor:
            return
        font, ok = QFontDialog.getFont(editor.font(), self)
        if ok:
            self.current_font_size = font.pointSize()
            for e in self.editor_widgets.values():
                e.setFont(font)

    def toggle_theme(self):
        self.is_dark_theme = not self.is_dark_theme
        self._apply_theme()
        self._save_settings()

    def _apply_theme(self):
        if self.is_dark_theme:
            self.setStyleSheet("""
                QMainWindow, QWidget { background: #1e1e1e; color: #d4d4d4; }
                QMenuBar { background: #252526; color: #cccccc; }
                QMenuBar::item:selected { background: #094771; }
                QMenu { background: #252526; color: #cccccc; border: 1px solid #454545; }
                QMenu::item:selected { background: #094771; }
                QToolBar { background: #333333; border: none; }
                QTabWidget::pane { border: 1px solid #454545; }
                QTabBar::tab { background: #2d2d2d; color: #cccccc; padding: 5px 12px; border: 1px solid #454545; }
                QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; border-bottom: 2px solid #007acc; }
                QTreeView { background: #252526; color: #cccccc; border: none; }
                QStatusBar { background: #007acc; color: white; }
                QLineEdit { background: #3c3c3c; color: #d4d4d4; border: 1px solid #555; padding: 2px; }
                QPushButton { background: #0e639c; color: white; border: none; padding: 4px 12px; }
                QPushButton:hover { background: #1177bb; }
            """)
            editor_style = "background:#1e1e1e; color:#d4d4d4;"
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget { background: #ffffff; color: #000000; }
                QMenuBar { background: #f3f3f3; color: #333333; }
                QMenuBar::item:selected { background: #c5d8f0; }
                QMenu { background: #f3f3f3; color: #333333; border: 1px solid #cccccc; }
                QMenu::item:selected { background: #c5d8f0; }
                QToolBar { background: #dddddd; border: none; }
                QTabBar::tab { background: #ececec; color: #333333; padding: 5px 12px; }
                QTabBar::tab:selected { background: #ffffff; border-bottom: 2px solid #007acc; }
                QTreeView { background: #f3f3f3; color: #333333; border: none; }
                QStatusBar { background: #007acc; color: white; }
                QLineEdit { background: #ffffff; color: #000000; border: 1px solid #aaa; padding: 2px; }
                QPushButton { background: #0e639c; color: white; border: none; padding: 4px 12px; }
                QPushButton:hover { background: #1177bb; }
            """)
            editor_style = "background:#ffffff; color:#000000;"
        for editor in self.editor_widgets.values():
            editor.setStyleSheet(editor_style)

    def toggle_sidebar(self):
        self.tree.setVisible(not self.tree.isVisible())

    def toggle_terminal(self):
        self.terminal.setVisible(not self.terminal.isVisible())

    def run_python(self):
        tab = self._current_tab()
        if not tab:
            return
        path = getattr(tab, 'file_path', '未命名')
        if path == '未命名':
            QMessageBox.warning(self, "提示", "請先儲存檔案再執行。")
            return
        self.save_file()
        self.terminal.setVisible(True)
        cmd = f"python3 \"{path}\"\n"
        self.terminal.process.write(cmd.encode())

    def open_git_dialog(self):
        dlg = GitDialog(self)
        dlg.exec_()

    def _save_settings(self):
        self.settings.setValue("recent_files", self.recent_files)
        self.settings.setValue("font_size", self.current_font_size)
        self.settings.setValue("dark_theme", self.is_dark_theme)

    def _load_settings(self):
        self.recent_files = self.settings.value("recent_files", []) or []
        self.current_font_size = int(self.settings.value("font_size", 13))
        self.is_dark_theme = self.settings.value("dark_theme", True)
        if isinstance(self.is_dark_theme, str):
            self.is_dark_theme = self.is_dark_theme.lower() != 'false'

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

# ───────────────────────────── 入口 ───────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
