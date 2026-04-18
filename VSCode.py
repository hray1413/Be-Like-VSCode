import sys
import re
import subprocess
import os
import json
from PyQt5.QtCore import (Qt, QDir, QSize, QFileInfo, QProcess,
                           QTimer, QSettings, QThread, pyqtSignal)
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
        self.setMinimumWidth(480)
        self._main_window = None  # 快取，避免重複查找

        layout = QVBoxLayout()

        # 輸入列
        input_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入關鍵字或正則表達式…")
        self.search_input.returnPressed.connect(self.find_text)
        input_row.addWidget(self.search_input)
        search_btn = QPushButton("搜尋")
        search_btn.clicked.connect(self.find_text)
        input_row.addWidget(search_btn)
        layout.addLayout(input_row)

        # 選項列
        option_row = QHBoxLayout()
        self.chk_regex = QCheckBox("正則表達式 (Regex)")
        self.chk_case  = QCheckBox("區分大小寫")
        self.chk_word  = QCheckBox("完整單字")
        option_row.addWidget(self.chk_regex)
        option_row.addWidget(self.chk_case)
        option_row.addWidget(self.chk_word)
        option_row.addStretch()
        layout.addLayout(option_row)

        # 錯誤提示（平常隱藏）
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f48771;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        # 結果計數
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #858585; font-size: 12px;")
        layout.addWidget(self.count_label)

        # 結果列表
        self.result_list = QListWidget()
        self.result_list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.result_list)

        self.setLayout(layout)

    def _get_main_window(self):
        if self._main_window:
            return self._main_window
        parent = self.parentWidget() or self.editor.parentWidget()
        if parent and hasattr(parent, 'parentWidget'):
            self._main_window = parent.parentWidget()
        return self._main_window

    def _build_pattern(self, keyword):
        """根據選項建立 re pattern，回傳 (pattern, error_msg)。"""
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE

        if self.chk_regex.isChecked():
            try:
                pattern = re.compile(keyword, flags)
            except re.error as e:
                return None, f"Regex 錯誤：{e}"
        else:
            escaped = re.escape(keyword)
            if self.chk_word.isChecked():
                escaped = rf"\b{escaped}\b"
            pattern = re.compile(escaped, flags)

        return pattern, None

    def find_text(self):
        self.result_list.clear()
        self.error_label.hide()
        self.count_label.setText("")

        keyword = self.search_input.text()
        if not keyword:
            return

        pattern, err = self._build_pattern(keyword)
        if err:
            self.error_label.setText(err)
            self.error_label.show()
            return

        main_window = self._get_main_window()
        if not main_window:
            return

        total = 0
        for tab_index in range(main_window.tabs.count()):
            tab = main_window.tabs.widget(tab_index)
            editor = main_window.editor_widgets.get(tab)
            if not editor:
                continue
            lines = editor.toPlainText().split('\n')
            for line_num, line in enumerate(lines, start=1):
                matches = list(pattern.finditer(line))
                if not matches:
                    continue
                total += len(matches)
                file_name = QFileInfo(getattr(tab, 'file_path', '未命名')).fileName()
                # 標示每個符合位置（col）
                cols = ", ".join(f"欄{m.start()+1}" for m in matches)
                item_text = f"{file_name}  第 {line_num} 行  [{cols}]：{line.strip()}"
                item = QListWidgetItem(item_text)
                item.setData(Qt.UserRole, (tab_index, line_num, matches[0].start()))
                self.result_list.addItem(item)

        self.count_label.setText(f"共找到 {total} 個符合，{self.result_list.count()} 行")

    def _on_item_clicked(self, item):
        main_window = self._get_main_window()
        if not main_window:
            return
        tab_index, line_num, col = item.data(Qt.UserRole)
        main_window.tabs.setCurrentIndex(tab_index)
        editor = main_window.editor_widgets[main_window.tabs.widget(tab_index)]

        # 移到對應行並高亮該行的符合文字
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for _ in range(line_num - 1):
            cursor.movePosition(QTextCursor.Down)
        cursor.movePosition(QTextCursor.StartOfLine)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, col)

        # 選取符合的文字長度
        keyword = self.search_input.text()
        pattern, _ = self._build_pattern(keyword)
        line_text = editor.document().findBlockByLineNumber(line_num - 1).text()
        m = pattern.search(line_text, col)
        if m:
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(m.group()))

        editor.setTextCursor(cursor)
        editor.setFocus()

# ───────────────────────────── 取代對話框 ─────────────────────────────
class ReplaceDialog(QDialog):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setWindowTitle("搜尋與取代")
        self.setMinimumWidth(480)

        layout = QVBoxLayout()

        # 搜尋輸入
        layout.addWidget(QLabel("搜尋："))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入關鍵字或正則表達式…")
        layout.addWidget(self.search_input)

        # 取代輸入
        layout.addWidget(QLabel("取代為："))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("取代內容（Regex 模式可用 \\1 回參）")
        layout.addWidget(self.replace_input)

        # 選項列
        opt_row = QHBoxLayout()
        self.chk_regex = QCheckBox("正則表達式")
        self.chk_case  = QCheckBox("區分大小寫")
        self.chk_word  = QCheckBox("完整單字")
        opt_row.addWidget(self.chk_regex)
        opt_row.addWidget(self.chk_case)
        opt_row.addWidget(self.chk_word)
        opt_row.addStretch()
        layout.addLayout(opt_row)

        # 錯誤 / 結果提示
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #858585; font-size: 12px;")
        layout.addWidget(self.info_label)

        # 按鈕列
        btn_row = QHBoxLayout()
        btn_find    = QPushButton("找下一個")
        btn_replace = QPushButton("取代")
        btn_all     = QPushButton("全部取代")
        btn_find.clicked.connect(self.find_next)
        btn_replace.clicked.connect(self.replace_one)
        btn_all.clicked.connect(self.replace_all)
        btn_row.addWidget(btn_find)
        btn_row.addWidget(btn_replace)
        btn_row.addWidget(btn_all)
        layout.addLayout(btn_row)

        self.setLayout(layout)

    def _build_pattern(self, keyword):
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        if self.chk_regex.isChecked():
            try:
                return re.compile(keyword, flags), None
            except re.error as e:
                return None, f"Regex 錯誤：{e}"
        escaped = re.escape(keyword)
        if self.chk_word.isChecked():
            escaped = rf"\b{escaped}\b"
        return re.compile(escaped, flags), None

    def find_next(self):
        keyword = self.search_input.text()
        if not keyword:
            return False
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return False

        full_text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        start = cursor.selectionEnd()
        m = pattern.search(full_text, start)
        if not m:
            m = pattern.search(full_text)  # 從頭繞回
        if m:
            c = self.editor.textCursor()
            c.setPosition(m.start())
            c.setPosition(m.end(), QTextCursor.KeepAnchor)
            self.editor.setTextCursor(c)
            self.info_label.setText("")
            return True
        self.info_label.setText("找不到符合內容")
        return False

    def replace_one(self):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self.find_next()
            return
        keyword = self.search_input.text()
        replace = self.replace_input.text()
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return
        selected = cursor.selectedText()
        new_text = pattern.sub(replace, selected, count=1)
        cursor.insertText(new_text)
        self.find_next()

    def replace_all(self):
        keyword = self.search_input.text()
        replace = self.replace_input.text()
        if not keyword:
            return
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return
        original = self.editor.toPlainText()
        new_text, count = pattern.subn(replace, original)
        if count:
            self.editor.setPlainText(new_text)
            self.info_label.setText(f"已取代 {count} 處")
        else:
            self.info_label.setText("找不到符合內容")

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

        # Ctrl+/ — 切換行註解
        if event.key() == Qt.Key_Slash and event.modifiers() == Qt.ControlModifier:
            self._toggle_comment()
            return

        # Ctrl+D — 選取下一個相同文字
        if event.key() == Qt.Key_D and event.modifiers() == Qt.ControlModifier:
            self._select_next_occurrence()
            return

        # Tab / Shift+Tab — 整段縮排 / 反縮排
        if event.key() == Qt.Key_Tab:
            cursor = self.textCursor()
            if cursor.hasSelection():
                self._indent_selection(dedent=False)
            else:
                self.insertPlainText("    ")
            return

        if event.key() == Qt.Key_Backtab:
            self._indent_selection(dedent=True)
            return

        # 自動配對括號
        pairs = {'(': ')', '[': ']', '{': '}', '"': '"', "'": "'"}
        if event.text() in pairs:
            super().keyPressEvent(event)
            self.insertPlainText(pairs[event.text()])
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.Left)
            self.setTextCursor(cursor)
            return

        super().keyPressEvent(event)

        # 自動完成觸發
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

    # ── 整段縮排 / 反縮排 ──
    def _indent_selection(self, dedent=False):
        cursor = self.textCursor()
        start = cursor.selectionStart()
        end   = cursor.selectionEnd()

        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.StartOfLine)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)

        lines = cursor.selectedText().split('\u2029')  # Qt 段落分隔符
        new_lines = []
        for line in lines:
            if dedent:
                if line.startswith('    '):
                    new_lines.append(line[4:])
                elif line.startswith('\t'):
                    new_lines.append(line[1:])
                else:
                    new_lines.append(line)
            else:
                new_lines.append('    ' + line)
        cursor.insertText('\u2029'.join(new_lines))

    # ── 切換行註解 Ctrl+/ ──
    def _toggle_comment(self):
        cursor = self.textCursor()
        start = cursor.selectionStart()
        end   = cursor.selectionEnd()

        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.StartOfLine)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)

        lines = cursor.selectedText().split('\u2029')
        # 如果全部都有 # 就取消，否則加上
        all_commented = all(l.lstrip().startswith('#') for l in lines if l.strip())
        new_lines = []
        for line in lines:
            if all_commented:
                # 移除第一個 # （保留縮排）
                stripped = line.lstrip()
                indent = line[:len(line) - len(stripped)]
                new_lines.append(indent + stripped[1:].lstrip() if stripped.startswith('#') else line)
            else:
                new_lines.append('# ' + line)
        cursor.insertText('\u2029'.join(new_lines))

    # ── Ctrl+D 選取下一個相同文字 ──
    def _select_next_occurrence(self):
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.WordUnderCursor)
            self.setTextCursor(cursor)
            return

        word = cursor.selectedText()
        if not word:
            return

        doc = self.document()
        # 從目前選取結尾往後找
        search_cursor = doc.find(word, cursor.selectionEnd())
        if search_cursor.isNull():
            # 繞回從頭找
            search_cursor = doc.find(word, 0)
        if not search_cursor.isNull():
            self.setTextCursor(search_cursor)

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

# ══════════════════════════════════════════════════════════════════════
#  非同步語法高亮架構
#
#  小檔（< SYNC_THRESHOLD 行）→ 直接在主執行緒 highlightBlock() 同步高亮
#  大檔                       → HighlightWorker（QThread）在背景跑 re，
#                               算完後 signal 通知 BaseHighlighter 套用結果
# ══════════════════════════════════════════════════════════════════════
SYNC_THRESHOLD = 500   # 行數門檻，低於此值同步高亮

# ── 格式描述（可跨 thread 傳遞的純資料）─────────────────────────────
# 每條 rule 用 Python re.compile 物件 + 顏色字串儲存
class _Rule:
    __slots__ = ('pattern', 'color', 'bold', 'italic')
    def __init__(self, pattern, color, bold=False, italic=False):
        self.pattern = pattern
        self.color   = color
        self.bold    = bold
        self.italic  = italic

# ── 背景 Worker ───────────────────────────────────────────────────────
class HighlightWorker(QThread):
    """
    在子執行緒中對整份文字跑 re，產出
      results: list[ list[ (start, length, color, bold, italic) ] ]
    每個外層元素對應一行。
    """
    results_ready = pyqtSignal(list)   # 算完後發給 highlighter

    def __init__(self, text, rules, parent=None):
        super().__init__(parent)
        self._text  = text
        self._rules = rules        # list[_Rule]

    def run(self):
        lines = self._text.split('\n')
        output = []
        for line in lines:
            spans = []
            for rule in self._rules:
                for m in rule.pattern.finditer(line):
                    spans.append((m.start(), m.end() - m.start(),
                                  rule.color, rule.bold, rule.italic))
            output.append(spans)
        self.results_ready.emit(output)

# ── 基礎高亮器 ────────────────────────────────────────────────────────
class BaseHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._rules: list[_Rule] = []
        self._async_data: list[list] = []   # Worker 回傳結果快取
        self._worker: HighlightWorker | None = None
        self._setup_rules()
        # 監聽文字變動，大檔時啟動背景 Worker
        document.contentsChanged.connect(self._on_contents_changed)

    def _setup_rules(self):
        pass  # 子類覆寫，呼叫 _kw() / _re()

    # ── 快速格式建立 ──
    @staticmethod
    def _make_fmt(color, bold=False, italic=False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:   fmt.setFontWeight(QFont.Bold)
        if italic: fmt.setFontItalic(True)
        return fmt

    def _kw(self, words, color="#569cd6", bold=True):
        for w in words:
            pat = re.compile(rf"\b{re.escape(w)}\b")
            self._rules.append(_Rule(pat, color, bold=bold))

    def _re(self, pattern, color, bold=False, italic=False):
        pat = re.compile(pattern)
        self._rules.append(_Rule(pat, color, bold=bold, italic=italic))

    # ── 文字變動時決定同步 or 非同步 ──
    def _on_contents_changed(self):
        doc = self.document()
        if doc is None:
            return
        line_count = doc.blockCount()
        if line_count >= SYNC_THRESHOLD:
            self._start_worker(doc.toPlainText())

    def _start_worker(self, text):
        # 如果前一個 Worker 還在跑，先停掉
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(200)
        self._worker = HighlightWorker(text, self._rules, self)
        self._worker.results_ready.connect(self._on_results_ready)
        self._worker.start()

    def _on_results_ready(self, data):
        self._async_data = data
        # 通知 Qt 重新高亮所有區塊
        self.rehighlight()

    # ── Qt 每次渲染一行時呼叫 ──
    def highlightBlock(self, text):
        block_num = self.currentBlock().blockNumber()
        line_count = self.document().blockCount()

        if line_count >= SYNC_THRESHOLD and self._async_data:
            # 大檔：從快取取預算好的結果直接套用，速度極快
            if block_num < len(self._async_data):
                for start, length, color, bold, italic in self._async_data[block_num]:
                    self.setFormat(start, length, self._make_fmt(color, bold, italic))
        else:
            # 小檔：同步直接跑 re（低延遲，不需要 thread overhead）
            for rule in self._rules:
                for m in rule.pattern.finditer(text):
                    self.setFormat(m.start(), m.end() - m.start(),
                                   self._make_fmt(rule.color, rule.bold, rule.italic))

# ───────────────────────────── Python ────────────────────────────────
class PythonHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['def','class','if','elif','else','try','except','finally',
                  'while','for','in','import','from','as','return','with','pass',
                  'break','continue','and','or','not','is','lambda',
                  'True','False','None','yield','raise','del','global','nonlocal',
                  'assert','async','await'])
        self._kw(['print','len','range','enumerate','open','type','isinstance',
                  'list','dict','set','tuple','int','str','float','bool',
                  'super','self','zip','map','filter','sorted','reversed',
                  'min','max','sum','abs','round','repr','id','hash',
                  'getattr','setattr','hasattr','callable','staticmethod',
                  'classmethod','property'], color="#dcdcaa", bold=False)
        self._re("@\\w+", "#c586c0")                             # 裝飾器
        self._re("\\b[0-9]+\\.?[0-9]*([eE][+-]?[0-9]+)?\\b", "#b5cea8")  # 數字
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")      # 雙引號字串
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")      # 單引號字串
        self._re("#.*", "#6a9955", italic=True)                   # 註解
        self._re("\\bdef\\s+\\w+", "#dcdcaa")                    # 函式名
        self._re("\\bclass\\s+\\w+", "#4ec9b0")                  # 類別名

# ───────────────────────────── JavaScript / TypeScript ───────────────
class JSHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['var','let','const','function','return','if','else','for',
                  'while','do','switch','case','break','continue','class',
                  'import','export','from','new','delete','typeof','instanceof',
                  'this','super','extends','implements','interface','enum',
                  'try','catch','finally','throw','in','of','void',
                  'true','false','null','undefined','NaN','Infinity',
                  'async','await','yield','static','get','set',
                  # TypeScript 額外
                  'type','namespace','declare','abstract','readonly',
                  'public','private','protected','as','keyof','infer'])
        self._kw(['console','Math','Object','Array','String','Number','Boolean',
                  'Promise','fetch','setTimeout','setInterval','Map','Set',
                  'JSON','Date','Error','RegExp'], color="#dcdcaa", bold=False)
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re("`[^`]*`", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*.*\\*/", "#6a9955", italic=True)
        self._re("\\b\\w+(?=\\s*\\()", "#dcdcaa")   # 函式呼叫
        self._re(":[\\s]*[A-Z]\\w*", "#4ec9b0")     # 型別標注（TS）

# ───────────────────────────── HTML / XML ────────────────────────────
class HTMLHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re("<!--[^>]*-->", "#6a9955", italic=True)   # 註解（先處理）
        self._re("</?[\\w:-]+", "#4ec9b0")                  # 標籤名
        self._re(">", "#4ec9b0")
        self._re("\\b[\\w:-]+(?=\\s*=)", "#9cdcfe")         # 屬性名
        self._re('"[^"]*"', "#ce9178")                      # 屬性值
        self._re("'[^']*'", "#ce9178")
        self._re("&[a-zA-Z0-9#]+;", "#f48771")              # HTML 實體
        self._re("<!DOCTYPE[^>]*>", "#808080")               # DOCTYPE

# ───────────────────────────── CSS / SCSS / Less ──────────────────────
class CSSHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)  # 區塊註解
        self._re("//.*", "#6a9955", italic=True)             # 行內（SCSS/Less）
        self._re("[.#][\\w-]+", "#d7ba7d")                   # class / id 選擇器
        self._re("@[\\w-]+", "#c586c0")                      # at-rules / 變數
        self._re("\\$[\\w-]+", "#9cdcfe")                    # SCSS 變數
        self._re("--[\\w-]+", "#9cdcfe")                     # CSS 自訂變數
        self._re("\\b[a-z-]+(?=\\s*:)", "#9cdcfe")           # 屬性名
        self._re("#[0-9a-fA-F]{3,8}\\b", "#ce9178")          # 顏色值
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*(px|em|rem|vh|vw|%|pt|s|ms)?\\b", "#b5cea8")
        self._kw(['important', 'inherit', 'initial', 'unset', 'none',
                  'auto', 'normal', 'bold', 'italic', 'flex', 'grid',
                  'block', 'inline', 'absolute', 'relative', 'fixed',
                  'sticky', 'center', 'left', 'right', 'top', 'bottom'],
                 color="#569cd6", bold=False)

# ───────────────────────────── C / C++ ───────────────────────────────
class CHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['auto','break','case','char','const','continue','default',
                  'do','double','else','enum','extern','float','for','goto',
                  'if','inline','int','long','register','restrict','return',
                  'short','signed','sizeof','static','struct','switch',
                  'typedef','union','unsigned','void','volatile','while',
                  # C++ 額外
                  'bool','class','delete','explicit','export','false',
                  'friend','mutable','namespace','new','nullptr','operator',
                  'private','protected','public','template','this','throw',
                  'true','try','catch','typeid','typename','using','virtual',
                  'override','final','constexpr','decltype','noexcept',
                  'static_assert','thread_local','alignas','alignof'])
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*([uUlLfF]*)\\b", "#b5cea8")
        self._re("0x[0-9a-fA-F]+", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("#\\s*(include|define|ifdef|ifndef|endif|pragma|undef|if|elif|else|error)", "#c586c0")
        self._re("<[\\w./]+>", "#ce9178")                    # #include <...>
        self._re("\\b[A-Z][A-Z0-9_]+\\b", "#b5cea8")        # 全大寫常數

# ───────────────────────────── Java ──────────────────────────────────
class JavaHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['abstract','assert','boolean','break','byte','case','catch',
                  'char','class','const','continue','default','do','double',
                  'else','enum','extends','final','finally','float','for',
                  'goto','if','implements','import','instanceof','int',
                  'interface','long','native','new','null','package','private',
                  'protected','public','return','short','static','strictfp',
                  'super','switch','synchronized','this','throw','throws',
                  'transient','try','var','void','volatile','while',
                  'true','false','record','sealed','permits','yield'])
        self._kw(['System','String','Integer','Double','Boolean','List','Map',
                  'Set','ArrayList','HashMap','Optional','Stream','Object',
                  'Math','Arrays','Collections'], color="#4ec9b0", bold=False)
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*[lLfFdD]?\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("@\\w+", "#c586c0")                         # 注解 annotation

# ───────────────────────────── C# ────────────────────────────────────
class CSharpHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['abstract','as','base','bool','break','byte','case','catch',
                  'char','checked','class','const','continue','decimal','default',
                  'delegate','do','double','else','enum','event','explicit',
                  'extern','false','finally','fixed','float','for','foreach',
                  'goto','if','implicit','in','int','interface','internal',
                  'is','lock','long','namespace','new','null','object','operator',
                  'out','override','params','private','protected','public',
                  'readonly','ref','return','sbyte','sealed','short','sizeof',
                  'stackalloc','static','string','struct','switch','this','throw',
                  'true','try','typeof','uint','ulong','unchecked','unsafe',
                  'ushort','using','virtual','void','volatile','while',
                  'async','await','var','dynamic','record','init','with',
                  'global','file','required','scoped'])
        self._re('@?"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*[uUlLfFdDmM]?\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("///.*", "#6a9955", italic=True)            # XML doc comment
        self._re("\\[\\w+[^\\]]*\\]", "#c586c0")             # Attribute

# ───────────────────────────── Go ────────────────────────────────────
class GoHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['break','case','chan','const','continue','default','defer',
                  'else','fallthrough','for','func','go','goto','if','import',
                  'interface','map','package','range','return','select','struct',
                  'switch','type','var',
                  'true','false','nil','iota'])
        self._kw(['append','cap','close','complex','copy','delete','imag',
                  'len','make','new','panic','print','println','real',
                  'recover','error','string','int','int8','int16','int32',
                  'int64','uint','uint8','uint16','uint32','uint64','uintptr',
                  'float32','float64','complex64','complex128','byte','rune',
                  'bool'], color="#4ec9b0", bold=False)
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("`[^`]*`", "#ce9178")                       # raw string
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)

# ───────────────────────────── Rust ──────────────────────────────────
class RustHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['as','async','await','break','const','continue','crate',
                  'dyn','else','enum','extern','false','fn','for','if',
                  'impl','in','let','loop','match','mod','move','mut',
                  'pub','ref','return','self','Self','static','struct',
                  'super','trait','true','type','union','unsafe','use',
                  'where','while','abstract','become','box','do','final',
                  'macro','override','priv','try','typeof','unsized','virtual','yield'])
        self._kw(['i8','i16','i32','i64','i128','isize',
                  'u8','u16','u32','u64','u128','usize',
                  'f32','f64','bool','char','str','String','Vec',
                  'Option','Result','Box','Rc','Arc','HashMap','HashSet',
                  'println','print','eprintln','panic','assert','assert_eq',
                  'todo','unimplemented','unreachable'],
                 color="#4ec9b0", bold=False)
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("b\"[^\"]*\"", "#ce9178")                   # byte string
        self._re("\\b[0-9]+\\.?[0-9]*(_[a-z0-9]+)?\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("#!?\\[[^\\]]*\\]", "#c586c0")              # Attribute
        self._re("'[a-zA-Z_]\\w*", "#569cd6")               # lifetime

# ───────────────────────────── Ruby ──────────────────────────────────
class RubyHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['BEGIN','END','alias','and','begin','break','case','class',
                  'def','defined?','do','else','elsif','end','ensure',
                  'false','for','if','in','module','next','nil','not',
                  'or','redo','rescue','retry','return','self','super',
                  'then','true','undef','unless','until','when','while','yield',
                  '__FILE__','__LINE__','__method__','__dir__'])
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("%[qQwWiI]?[{(\\[|!][^})|!]*[})|!]", "#ce9178")  # %q{} 字串
        self._re(":[a-zA-Z_]\\w*", "#569cd6")               # symbol
        self._re("@{1,2}[a-zA-Z_]\\w*", "#9cdcfe")          # @var / @@var
        self._re("\\$[a-zA-Z_]\\w*", "#c586c0")              # $global
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("#.*", "#6a9955", italic=True)
        self._re("=begin.*=end", "#6a9955", italic=True)

# ───────────────────────────── PHP ───────────────────────────────────
class PHPHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['abstract','and','array','as','break','callable','case',
                  'catch','class','clone','const','continue','declare',
                  'default','die','do','echo','else','elseif','empty',
                  'enddeclare','endfor','endforeach','endif','endswitch',
                  'endwhile','eval','exit','extends','final','finally',
                  'fn','for','foreach','function','global','goto','if',
                  'implements','include','include_once','instanceof',
                  'insteadof','interface','isset','list','match','namespace',
                  'new','null','or','print','private','protected','public',
                  'readonly','require','require_once','return','static',
                  'switch','throw','trait','try','true','false','unset',
                  'use','var','while','xor','yield'])
        self._re("\\$[a-zA-Z_]\\w*", "#9cdcfe")             # 變數
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("<<<['\"]?\\w+['\"]?", "#ce9178")           # heredoc
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("#.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("<\\?php|\\?>", "#c586c0")                  # PHP 標籤

# ───────────────────────────── Shell / Bash ───────────────────────────
class ShellHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['if','then','else','elif','fi','for','while','do','done',
                  'case','esac','function','return','in','until',
                  'break','continue','exit','local','export','source',
                  'true','false','null'])
        self._kw(['echo','printf','read','cd','ls','pwd','mkdir','rm','cp',
                  'mv','cat','grep','sed','awk','find','sort','uniq','wc',
                  'chmod','chown','sudo','apt','yum','pip','python3',
                  'git','curl','wget','tar','zip','unzip'],
                 color="#dcdcaa", bold=False)
        self._re("\\$[{(]?[a-zA-Z_][\\w]*[})]?", "#9cdcfe") # 變數
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re("`[^`]*`", "#c586c0")                      # 命令替換
        self._re("#.*", "#6a9955", italic=True)
        self._re("\\b[0-9]+\\b", "#b5cea8")
        self._re("&&|\\|\\||>>?|<<", "#c586c0")             # 運算子

# ───────────────────────────── SQL ───────────────────────────────────
class SQLHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['SELECT','FROM','WHERE','INSERT','INTO','VALUES','UPDATE',
                  'SET','DELETE','CREATE','TABLE','DROP','ALTER','ADD',
                  'COLUMN','INDEX','VIEW','DATABASE','SCHEMA','GRANT',
                  'REVOKE','COMMIT','ROLLBACK','BEGIN','TRANSACTION',
                  'JOIN','LEFT','RIGHT','INNER','OUTER','FULL','CROSS',
                  'ON','AS','AND','OR','NOT','IN','IS','NULL','LIKE',
                  'BETWEEN','EXISTS','UNION','ALL','DISTINCT','GROUP',
                  'BY','ORDER','HAVING','LIMIT','OFFSET','ASC','DESC',
                  'PRIMARY','KEY','FOREIGN','REFERENCES','UNIQUE',
                  'DEFAULT','CHECK','CONSTRAINT','RETURNING','WITH',
                  # 小寫也支援
                  'select','from','where','insert','into','values','update',
                  'set','delete','create','table','drop','alter','join',
                  'left','right','inner','outer','on','as','and','or','not',
                  'in','is','null','like','between','exists','union',
                  'group','by','order','having','limit','offset'])
        self._kw(['COUNT','SUM','AVG','MIN','MAX','COALESCE','NULLIF',
                  'CAST','CONVERT','CONCAT','LENGTH','SUBSTR','UPPER',
                  'LOWER','TRIM','NOW','DATE','YEAR','MONTH','DAY',
                  'count','sum','avg','min','max','coalesce'],
                 color="#dcdcaa", bold=False)
        self._re("'[^']*'", "#ce9178")
        self._re('"[^"]*"', "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("--.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)

# ───────────────────────────── JSON ──────────────────────────────────
class JSONHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"\\s*:', "#9cdcfe")  # key
        self._re(':\\s*"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")  # string value
        self._re("\\b(true|false|null)\\b", "#569cd6")
        self._re(":\\s*-?[0-9]+\\.?[0-9]*([eE][+-]?[0-9]+)?", "#b5cea8")

# ───────────────────────────── YAML ──────────────────────────────────
class YAMLHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re("^---", "#c586c0")                          # document start
        self._re("^\\s*[\\w-]+\\s*:", "#9cdcfe")            # key
        self._re(":\\s*.+", "#ce9178")                       # value
        self._re("^\\s*- ", "#569cd6")                       # list item
        self._re("&\\w+|\\*\\w+", "#4ec9b0")                # anchor / alias
        self._re("!\\w+", "#c586c0")                         # tag
        self._re("#.*", "#6a9955", italic=True)
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re("\\b(true|false|null|yes|no|on|off)\\b", "#569cd6")
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")

# ───────────────────────────── Markdown ──────────────────────────────
class MarkdownHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re("^#{1,6}\\s.*", "#569cd6", bold=True)       # 標題
        self._re("\\*\\*[^*]+\\*\\*", "#dcdcaa", bold=True)  # **粗體**
        self._re("\\*[^*]+\\*", "#ce9178", italic=True)       # *斜體*
        self._re("__[^_]+__", "#dcdcaa", bold=True)
        self._re("_[^_]+_", "#ce9178", italic=True)
        self._re("`[^`]+`", "#4ec9b0")                        # `行內程式碼`
        self._re("^```.*", "#c586c0")                          # 程式碼區塊標記
        self._re("^>.*", "#6a9955", italic=True)              # 引用
        self._re("^\\s*[-*+]\\s", "#569cd6")                  # 列表
        self._re("^\\s*[0-9]+\\.\\s", "#569cd6")             # 有序列表
        self._re("\\[([^\\]]+)\\]\\([^)]+\\)", "#4ec9b0")    # 連結
        self._re("!\\[([^\\]]+)\\]\\([^)]+\\)", "#c586c0")   # 圖片
        self._re("^---+$", "#808080")                          # 分隔線

# ───────────────────────────── Kotlin ────────────────────────────────
class KotlinHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['abstract','actual','annotation','as','break','by','catch',
                  'class','companion','const','constructor','continue',
                  'crossinline','data','delegate','do','dynamic','else',
                  'enum','expect','external','false','field','file','final',
                  'finally','for','fun','get','if','import','in','infix',
                  'init','inline','inner','interface','internal','is',
                  'it','lateinit','noinline','null','object','open',
                  'operator','out','override','package','param','private',
                  'property','protected','public','receiver','reified',
                  'return','sealed','set','setparam','super','suspend',
                  'tailrec','this','throw','true','try','typealias',
                  'typeof','val','value','var','vararg','when','where','while'])
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re('"""[^"]*"""', "#ce9178")                   # triple-quoted
        self._re("'[^'\\\\]*(\\\\.[^'\\\\]*)*'", "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*[LFf]?\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("@\\w+", "#c586c0")

# ───────────────────────────── Swift ─────────────────────────────────
class SwiftHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['associatedtype','class','deinit','enum','extension',
                  'fileprivate','func','import','init','inout','internal',
                  'let','open','operator','precedencegroup','private',
                  'protocol','public','rethrows','static','struct',
                  'subscript','typealias','var','break','case','catch',
                  'continue','default','defer','do','else','fallthrough',
                  'for','guard','if','in','repeat','return','throw',
                  'switch','where','while','Any','as','false','is',
                  'nil','rethrows','self','Self','super','throw','throws',
                  'true','try','async','await','actor','nonisolated',
                  'some','any','consuming','borrowing'])
        self._re('"[^"\\\\]*(\\\\.[^"\\\\]*)*"', "#ce9178")
        self._re('"""[^"]*"""', "#ce9178")
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("//.*", "#6a9955", italic=True)
        self._re("/\\*[^*]*\\*+(?:[^/*][^*]*\\*+)*/", "#6a9955", italic=True)
        self._re("@\\w+", "#c586c0")                         # property wrapper

# ───────────────────────────── Dockerfile ────────────────────────────
class DockerHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._kw(['FROM','RUN','CMD','LABEL','EXPOSE','ENV','ADD','COPY',
                  'ENTRYPOINT','VOLUME','USER','WORKDIR','ARG','ONBUILD',
                  'STOPSIGNAL','HEALTHCHECK','SHELL',
                  'from','run','cmd','label','expose','env','add','copy',
                  'entrypoint','volume','user','workdir','arg'])
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re("#.*", "#6a9955", italic=True)
        self._re("\\$[{]?[a-zA-Z_]\\w*[}]?", "#9cdcfe")

# ───────────────────────────── TOML ──────────────────────────────────
class TOMLHighlighter(BaseHighlighter):
    def _setup_rules(self):
        self._re("^\\[+[^\\]]+\\]+", "#569cd6", bold=True)  # [section]
        self._re("^\\s*[\\w.-]+\\s*=", "#9cdcfe")           # key
        self._re('"[^"]*"', "#ce9178")
        self._re("'[^']*'", "#ce9178")
        self._re('"""[^"]*"""', "#ce9178")
        self._re("\\b(true|false)\\b", "#569cd6")
        self._re("\\b[0-9]{4}-[0-9]{2}-[0-9]{2}", "#4ec9b0") # date
        self._re("\\b[0-9]+\\.?[0-9]*\\b", "#b5cea8")
        self._re("#.*", "#6a9955", italic=True)

# ══════════════════════════════════════════════════════════════════════
#  根據副檔名選擇高亮器
# ══════════════════════════════════════════════════════════════════════
def get_highlighter_for_file(path, document):
    ext = QFileInfo(path).suffix().lower()
    name = QFileInfo(path).fileName().lower()

    MAP = {
        # Python
        ('py', 'pyw', 'pyi'): PythonHighlighter,
        # JavaScript / TypeScript
        ('js', 'jsx', 'mjs', 'cjs', 'ts', 'tsx'): JSHighlighter,
        # HTML / XML
        ('html', 'htm', 'xml', 'xhtml', 'svg'): HTMLHighlighter,
        # CSS / SCSS / Less
        ('css', 'scss', 'sass', 'less'): CSSHighlighter,
        # C / C++
        ('c', 'h', 'cpp', 'cxx', 'cc', 'hpp', 'hxx'): CHighlighter,
        # Java
        ('java',): JavaHighlighter,
        # C#
        ('cs',): CSharpHighlighter,
        # Go
        ('go',): GoHighlighter,
        # Rust
        ('rs',): RustHighlighter,
        # Ruby
        ('rb', 'rake', 'gemspec'): RubyHighlighter,
        # PHP
        ('php', 'php3', 'php4', 'php5', 'phtml'): PHPHighlighter,
        # Shell
        ('sh', 'bash', 'zsh', 'fish', 'ksh'): ShellHighlighter,
        # SQL
        ('sql',): SQLHighlighter,
        # JSON
        ('json', 'jsonc'): JSONHighlighter,
        # YAML
        ('yaml', 'yml'): YAMLHighlighter,
        # Markdown
        ('md', 'markdown', 'mdx'): MarkdownHighlighter,
        # Kotlin
        ('kt', 'kts'): KotlinHighlighter,
        # Swift
        ('swift',): SwiftHighlighter,
        # Dockerfile
        ('dockerfile',): DockerHighlighter,
        # TOML
        ('toml',): TOMLHighlighter,
    }

    # 無副檔名特殊檔名（如 Dockerfile、Makefile）
    NAMEMAP = {
        'dockerfile': DockerHighlighter,
        'makefile': ShellHighlighter,
        '.bashrc': ShellHighlighter,
        '.zshrc': ShellHighlighter,
        '.gitignore': ShellHighlighter,
    }
    if name in NAMEMAP:
        return NAMEMAP[name](document)

    for exts, cls in MAP.items():
        if ext in exts:
            return cls(document)

    return PythonHighlighter(document)  # 預設

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
        self._add_action(edit_menu, "復原", self.undo, "Ctrl+Z")
        self._add_action(edit_menu, "取消復原", self.redo, "Ctrl+Y")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "搜尋", self.open_search, "Ctrl+F")
        self._add_action(edit_menu, "取代", self.open_replace, "Ctrl+H")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "跳到指定行", self.goto_line, "Ctrl+L")
        self._add_action(edit_menu, "全選", lambda: self._current_editor() and self._current_editor().selectAll(), "Ctrl+A")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "縮排選取", lambda: self._current_editor() and self._current_editor()._indent_selection(False), "Tab")
        self._add_action(edit_menu, "反縮排選取", lambda: self._current_editor() and self._current_editor()._indent_selection(True), "Shift+Tab")
        self._add_action(edit_menu, "切換行註解", lambda: self._current_editor() and self._current_editor()._toggle_comment(), "Ctrl+/")
        self._add_action(edit_menu, "選取下一個相同文字", lambda: self._current_editor() and self._current_editor()._select_next_occurrence(), "Ctrl+D")

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
            ("↩ 復原", self.undo), ("↪ 取消復原", self.redo),
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

    def undo(self):
        editor = self._current_editor()
        if editor:
            editor.undo()

    def redo(self):
        editor = self._current_editor()
        if editor:
            editor.redo()

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
