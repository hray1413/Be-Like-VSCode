from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

from PyQt5.QtCore import (QDir, QFileInfo, QProcess, QSettings, QSize,
                           QThread, QTimer, Qt, pyqtSignal, QUrl)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QPainter,
                         QSyntaxHighlighter, QTextCharFormat, QTextCursor,
                         QTextFormat)
from PyQt5.QtWidgets import (QAction, QApplication, QCheckBox, QCompleter,
                              QDialog, QFileDialog, QFileSystemModel,
                              QFontDialog, QHBoxLayout, QInputDialog, QLabel,
                              QLineEdit, QListWidget, QListWidgetItem,
                              QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                              QPushButton, QSplitter, QStatusBar, QTabWidget,
                              QTextBrowser, QTextEdit, QToolBar, QTreeView,
                              QVBoxLayout, QWidget, QSlider, QStyle)
try:
    from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
    from PyQt5.QtMultimediaWidgets import QVideoWidget
    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False

# ══════════════════════════════════════════════════════════════════════
#  型態別名
# ══════════════════════════════════════════════════════════════════════
Span  = tuple[int, int, str, bool, bool]   # (start, length, color, bold, italic)
Spans = list[Span]

# ══════════════════════════════════════════════════════════════════════
#  搜尋對話框
# ══════════════════════════════════════════════════════════════════════

class SearchWorker(QThread):
    """非同步搜尋 Worker，來自 Scarch.py 架構，整合 abort 機制。"""
    result_found = pyqtSignal(int, int, int, str, str)  # tab_idx, line, col, text, file_name
    search_done  = pyqtSignal(int)                       # total 結果數

    def __init__(self, editors: dict, pattern: re.Pattern,
                 tab_names: dict[int, str]) -> None:
        super().__init__()
        self._editors   = editors    # {tab_widget: CodeEditor}
        self._pattern   = pattern
        self._tab_names = tab_names  # {tab_index: file_name}
        self._abort     = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        total = 0
        for tab_index, (tab, editor) in enumerate(self._editors.items()):
            block    = editor.document().firstBlock()
            line_num = 0
            while block.isValid():
                if self._abort:
                    return                          # 不 emit search_done，直接結束
                text = block.text()
                for m in self._pattern.finditer(text):
                    total += 1
                    self.result_found.emit(
                        tab_index, line_num + 1, m.start(),
                        text.strip(), self._tab_names.get(tab_index, '未命名'))
                block    = block.next()
                line_num += 1
        self.search_done.emit(total)


class SearchDialog(QDialog):
    def __init__(self, editor: CodeEditor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor  = editor
        self._worker: Optional[SearchWorker] = None
        self._main_window: Optional[MainWindow] = None
        self.setWindowTitle("搜尋")
        self.setMinimumWidth(520)

        layout = QVBoxLayout()

        # 輸入列
        input_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入關鍵字或正則表達式…")
        self.search_input.returnPressed.connect(self.find_text)
        input_row.addWidget(self.search_input)
        self._btn_search = QPushButton("搜尋")
        self._btn_search.clicked.connect(self.find_text)
        input_row.addWidget(self._btn_search)
        self._btn_stop = QPushButton("停止")
        self._btn_stop.clicked.connect(self._stop_search)
        self._btn_stop.setEnabled(False)
        input_row.addWidget(self._btn_stop)
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

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f48771;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #858585; font-size: 12px;")
        layout.addWidget(self.count_label)

        self.result_list = QListWidget()
        self.result_list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.result_list)
        self.setLayout(layout)

    def _get_main_window(self) -> Optional[MainWindow]:
        if self._main_window:
            return self._main_window
        parent = self.parentWidget() or self.editor.parentWidget()
        if parent and hasattr(parent, 'parentWidget'):
            self._main_window = parent.parentWidget()
        return self._main_window

    def _build_pattern(self, keyword: str) -> tuple[Optional[re.Pattern], Optional[str]]:
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        if self.chk_regex.isChecked():
            try:
                return re.compile(keyword, flags), None
            except re.error as e:
                return None, f"Regex 錯誤：{e}"
        escaped = re.escape(keyword)
        if self.chk_word.isChecked():
            escaped = rf"\b{escaped}\b"    # 注意：單個 \b，Scarch.py 原版是 \\b（bug）
        return re.compile(escaped, flags), None

    def find_text(self) -> None:
        # 先 abort 舊的 worker
        self._stop_search()
        self.result_list.clear()
        self.error_label.hide()
        self.count_label.setText("搜尋中…")

        keyword = self.search_input.text()
        if not keyword:
            self.count_label.setText("")
            return

        pattern, err = self._build_pattern(keyword)
        if err:
            self.error_label.setText(err)
            self.error_label.show()
            self.count_label.setText("")
            return

        main_window = self._get_main_window()
        if not main_window:
            return

        # 建立 tab_index → file_name 的對照表
        tab_names: dict[int, str] = {}
        editors_ordered: dict = {}
        for i in range(main_window.tabs.count()):
            tab = main_window.tabs.widget(i)
            editor = main_window.editor_widgets.get(tab)
            if editor:
                tab_names[i] = QFileInfo(getattr(tab, 'file_path', '未命名')).fileName()
                editors_ordered[tab] = editor

        self._worker = SearchWorker(editors_ordered, pattern, tab_names)
        self._worker.result_found.connect(self._on_result_found)
        self._worker.search_done.connect(self._on_search_done)
        self._worker.finished.connect(self._on_worker_finished)
        self._btn_search.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._worker.start()

    def _stop_search(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.abort()
            self._worker.finished.connect(self._worker.deleteLater)
            self._worker = None
        self._btn_search.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _on_result_found(self, tab_index: int, line_num: int, col: int,
                         text: str, file_name: str) -> None:
        cols = f"欄{col+1}"
        item = QListWidgetItem(f"{file_name}  第 {line_num} 行  [{cols}]：{text}")
        item.setData(Qt.UserRole, (tab_index, line_num, col))
        self.result_list.addItem(item)
        # 即時更新計數，讓使用者邊搜尋邊看到結果
        self.count_label.setText(f"找到 {self.result_list.count()} 行（搜尋中…）")

    def _on_search_done(self, total: int) -> None:
        self.count_label.setText(f"共找到 {total} 個符合，{self.result_list.count()} 行")

    def _on_worker_finished(self) -> None:
        self._btn_search.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        main_window = self._get_main_window()
        if not main_window:
            return
        tab_index, line_num, col = item.data(Qt.UserRole)
        main_window.tabs.setCurrentIndex(tab_index)
        editor = main_window.editor_widgets[main_window.tabs.widget(tab_index)]

        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for _ in range(line_num - 1):
            cursor.movePosition(QTextCursor.Down)
        cursor.movePosition(QTextCursor.StartOfLine)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, col)

        keyword    = self.search_input.text()
        pattern, _ = self._build_pattern(keyword)
        line_text  = editor.document().findBlockByLineNumber(line_num - 1).text()
        m = pattern.search(line_text, col)
        if m:
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(m.group()))

        editor.setTextCursor(cursor)
        editor.setFocus()

    def closeEvent(self, event) -> None:
        self._stop_search()
        super().closeEvent(event)

# ══════════════════════════════════════════════════════════════════════
#  取代對話框
# ══════════════════════════════════════════════════════════════════════
class ReplaceDialog(QDialog):
    def __init__(self, editor: CodeEditor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.setWindowTitle("搜尋與取代")
        self.setMinimumWidth(480)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("搜尋："))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入關鍵字或正則表達式…")
        layout.addWidget(self.search_input)

        layout.addWidget(QLabel("取代為："))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("取代內容（Regex 模式可用 \\1 回參）")
        layout.addWidget(self.replace_input)

        opt_row = QHBoxLayout()
        self.chk_regex = QCheckBox("正則表達式")
        self.chk_case  = QCheckBox("區分大小寫")
        self.chk_word  = QCheckBox("完整單字")
        opt_row.addWidget(self.chk_regex)
        opt_row.addWidget(self.chk_case)
        opt_row.addWidget(self.chk_word)
        opt_row.addStretch()
        layout.addLayout(opt_row)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #858585; font-size: 12px;")
        layout.addWidget(self.info_label)

        btn_row = QHBoxLayout()
        for label, slot in [("找下一個", self.find_next),
                             ("取代",     self.replace_one),
                             ("全部取代", self.replace_all)]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)
        self.setLayout(layout)

    def _build_pattern(self, keyword: str) -> tuple[Optional[re.Pattern], Optional[str]]:
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

    def find_next(self) -> bool:
        keyword = self.search_input.text()
        if not keyword:
            return False
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return False

        full_text = self.editor.toPlainText()
        start     = self.editor.textCursor().selectionEnd()
        m = pattern.search(full_text, start) or pattern.search(full_text)
        if m:
            c = self.editor.textCursor()
            c.setPosition(m.start())
            c.setPosition(m.end(), QTextCursor.KeepAnchor)
            self.editor.setTextCursor(c)
            self.info_label.setText("")
            return True
        self.info_label.setText("找不到符合內容")
        return False

    def replace_one(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self.find_next()
            return
        keyword = self.search_input.text()
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return
        cursor.insertText(pattern.sub(self.replace_input.text(), cursor.selectedText(), count=1))
        self.find_next()

    def replace_all(self) -> None:
        keyword = self.search_input.text()
        if not keyword:
            return
        pattern, err = self._build_pattern(keyword)
        if err:
            self.info_label.setText(err)
            return
        new_text, count = pattern.subn(self.replace_input.text(), self.editor.toPlainText())
        if count:
            self.editor.setPlainText(new_text)
            self.info_label.setText(f"已取代 {count} 處")
        else:
            self.info_label.setText("找不到符合內容")

# ══════════════════════════════════════════════════════════════════════
#  終端機
# ══════════════════════════════════════════════════════════════════════
class TerminalWidget(QTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(
            "background-color: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 13px;")

        self.process = QProcess()
        # 讓 stdout/stderr 合流，避免兩條 pipe 交錯
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.errorOccurred.connect(self._on_process_error)
        # 進程啟動完成後才顯示提示字元，避免 write failed
        self.process.started.connect(self._on_started)

        self._shell, self._shell_args = self._detect_shell()
        self.process.start(self._shell, self._shell_args)

    @staticmethod
    def _detect_shell() -> tuple[str, list[str]]:
        if sys.platform == "win32":
            # Git Bash 優先（體驗更好）
            for path in [
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
            ]:
                if os.path.exists(path):
                    return path, []
            # 沒有 Git Bash 就用 cmd.exe（不加 /K，用 pipe 模式）
            return "cmd.exe", []
        else:
            return os.environ.get("SHELL", "bash"), []

    def _on_started(self) -> None:
        """Shell 真正啟動後才顯示提示字元。"""
        self.append(f"[終端機：{self._shell}]\n$ ")

    def _on_process_error(self, error) -> None:
        msgs = {
            QProcess.FailedToStart: "找不到 shell 程式，請確認已安裝 Git Bash 或 cmd 可用。",
            QProcess.Crashed:       "Shell 程式意外結束。",
            QProcess.Timedout:      "Shell 啟動逾時。",
            QProcess.WriteError:    "無法寫入 shell（管道錯誤）。",
            QProcess.ReadError:     "無法讀取 shell 輸出。",
        }
        msg = msgs.get(error, f"未知錯誤（代碼 {error}）")
        self.setTextColor(QColor("#f48771"))
        self.append(f"[錯誤] {msg}")
        self.setTextColor(QColor("#d4d4d4"))

    def read_output(self) -> None:
        data = self.process.readAllStandardOutput().data().decode(errors='replace')
        self.insertPlainText(data)

    def shutdown(self) -> None:
        """
        由 MainWindow.closeEvent 呼叫，正確終止 shell：
        - 先送 exit 指令讓 shell 自己退出（優雅）
        - 等最多 2 秒
        - 還活著就強制 kill
        """
        proc = self.process
        if proc.state() == QProcess.NotRunning:
            return
        try:
            # 送 exit 讓 shell 自己結束
            if sys.platform == "win32":
                proc.write(b"exit\r\n")
            else:
                proc.write(b"exit\n")
            proc.closeWriteChannel()          # 關 stdin，通知 shell 沒有更多輸入
            if not proc.waitForFinished(2000):
                proc.terminate()
                if not proc.waitForFinished(1000):
                    proc.kill()
                    proc.waitForFinished(500)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def keyPressEvent(self, event) -> None:
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

# ══════════════════════════════════════════════════════════════════════
#  行號區域
# ══════════════════════════════════════════════════════════════════════
class LineNumberArea(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.lineNumberAreaWidth(), 0)

    def paintEvent(self, event) -> None:
        self.editor.lineNumberAreaPaintEvent(event)

# ══════════════════════════════════════════════════════════════════════
#  程式碼編輯器
# ══════════════════════════════════════════════════════════════════════
class CodeEditor(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.highlighter: BaseHighlighter = PythonHighlighter(self.document())
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
            'open', 'self', 'super', '__init__',
        ]
        self.completer = QCompleter(keywords)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.activated.connect(self.insert_completion)
        self.cursorPositionChanged.connect(self.highlight_brackets)
        self.verticalScrollBar().valueChanged.connect(self._notify_viewport)
        # LSP state
        self._diagnostics: list[dict]           = []
        self._lsp_manager: "Optional[LspManager]" = None
        self._file_path: str                    = ""
        self._doc_version: int                  = 0

        # Debounce didChange: 500ms after typing stops
        self._lsp_debounce = QTimer()
        self._lsp_debounce.setSingleShot(True)
        self._lsp_debounce.setInterval(500)
        self._lsp_debounce.timeout.connect(self._send_did_change)
        self.document().contentsChanged.connect(self._lsp_debounce.start)

        # Hover debounce: 800ms hover
        self._hover_timer = QTimer()
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(800)
        self._hover_timer.timeout.connect(self._send_hover)
        self._hover_pos: Optional[tuple[int, int]] = None
        self.setMouseTracking(True)

    def attach_lsp(self, manager: "LspManager", path: str) -> None:
        self._lsp_manager = manager
        self._file_path   = path
        manager.open_document(path, self.toPlainText())

    def detach_lsp(self) -> None:
        if self._lsp_manager and self._file_path:
            self._lsp_manager.close_document(self._file_path)
        self._lsp_manager = None
        self._file_path   = ""

    def set_diagnostics(self, diags: list) -> None:
        self._diagnostics = diags
        self._render_diagnostics()

    def _render_diagnostics(self) -> None:
        doc = self.document()
        extra = []
        for d in self._diagnostics:
            line     = d["line"]
            char     = d["char"]
            end_line = d.get("end_line", line)
            end_char = d.get("end_char", char + 1)
            sev      = d.get("severity", "error")
            color    = LSP_SEVERITY_COLOR.get(sev, "#f48771")

            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.SpellCheckUnderline)
            fmt.setUnderlineColor(QColor(color))

            block_s = doc.findBlockByNumber(line)
            block_e = doc.findBlockByNumber(end_line)
            if not block_s.isValid():
                continue
            sel = QPlainTextEdit.ExtraSelection()
            sel.format = fmt
            c = QTextCursor(block_s)
            c.movePosition(QTextCursor.StartOfBlock)
            c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, char)
            end_pos = (block_e.position() + end_char
                       if block_e.isValid() else c.position() + 1)
            c.setPosition(end_pos, QTextCursor.KeepAnchor)
            sel.cursor = c
            extra.append(sel)
        # merge with existing selections (bracket highlight, current line)
        current = [s for s in self.extraSelections()
                   if s.format.underlineStyle() != QTextCharFormat.SpellCheckUnderline]
        self.setExtraSelections(current + extra)

    def _send_did_change(self) -> None:
        if self._lsp_manager and self._file_path:
            self._lsp_manager.change_document(self._file_path, self.toPlainText())

    def _send_hover(self) -> None:
        if not self._lsp_manager or not self._file_path or not self._hover_pos:
            return
        line, char = self._hover_pos
        self._lsp_manager.request_hover(self._file_path, line, char)

    def apply_lsp_completion(self, items: list) -> None:
        if not items:
            return
        from PyQt5.QtCore import QStringListModel
        labels = [i["label"] for i in items]
        self.completer.setModel(QStringListModel(labels))
        cursor = self.textCursor()
        cursor.select(QTextCursor.WordUnderCursor)
        prefix = cursor.selectedText()
        self.completer.setCompletionPrefix(prefix)
        cr = self.cursorRect()
        cr.setWidth(self.completer.popup().sizeHintForColumn(0)
                    + self.completer.popup().verticalScrollBar().sizeHint().width())
        self.completer.complete(cr)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._lsp_manager and self._file_path:
            cursor = self.cursorForPosition(event.pos())
            self._hover_pos = (cursor.blockNumber(), cursor.columnNumber())
            self._hover_timer.start()

    def mousePressEvent(self, event) -> None:
        # Ctrl+Click => go to definition
        if (event.button() == Qt.LeftButton
                and event.modifiers() == Qt.ControlModifier
                and self._lsp_manager and self._file_path):
            cursor = self.cursorForPosition(event.pos())
            self._lsp_manager.request_definition(
                self._file_path,
                cursor.blockNumber(),
                cursor.columnNumber())
            return
        super().mousePressEvent(event)

    def _notify_viewport(self, *_) -> None:
        """計算目前可見行號範圍並通知 highlighter。"""
        first = self.firstVisibleBlock()
        if not first.isValid():
            return
        first_line = first.blockNumber()
        block      = first
        last_line  = first_line
        vp_bottom  = self.viewport().rect().bottom()
        while block.isValid():
            if self.blockBoundingGeometry(block).translated(self.contentOffset()).top() > vp_bottom:
                break
            last_line = block.blockNumber()
            block = block.next()
        self.highlighter.set_viewport(first_line, last_line)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        if dy != 0:
            self._notify_viewport()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.lineNumberArea.setGeometry(
            cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height())
        self._notify_viewport()

    def insert_completion(self, text: str) -> None:
        tc = self.textCursor()
        extra = len(text) - len(self.completer.completionPrefix())
        tc.movePosition(QTextCursor.Left)
        tc.movePosition(QTextCursor.EndOfWord)
        tc.insertText(text[-extra:])
        self.setTextCursor(tc)

    def keyPressEvent(self, event) -> None:
        if self.completer.popup().isVisible():
            if event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab,
                               Qt.Key_Escape, Qt.Key_Backtab):
                event.ignore()
                return

        if event.key() == Qt.Key_Slash and event.modifiers() == Qt.ControlModifier:
            self._toggle_comment()
            return

        if event.key() == Qt.Key_D and event.modifiers() == Qt.ControlModifier:
            self._select_next_occurrence()
            return

        if event.key() == Qt.Key_Tab:
            if self.textCursor().hasSelection():
                self._indent_selection(dedent=False)
            else:
                self.insertPlainText("    ")
            return

        if event.key() == Qt.Key_Backtab:
            self._indent_selection(dedent=True)
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

    def _indent_selection(self, dedent: bool = False) -> None:
        cursor = self.textCursor()
        cursor.setPosition(cursor.selectionStart())
        cursor.movePosition(QTextCursor.StartOfLine)
        cursor.setPosition(cursor.anchor() if not cursor.hasSelection()
                           else self.textCursor().selectionEnd(), QTextCursor.KeepAnchor)
        cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)

        lines     = cursor.selectedText().split('\u2029')
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

    def _toggle_comment(self) -> None:
        cursor = self.textCursor()
        cursor.setPosition(cursor.selectionStart())
        cursor.movePosition(QTextCursor.StartOfLine)
        end = self.textCursor().selectionEnd()
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)

        lines        = cursor.selectedText().split('\u2029')
        all_commented = all(l.lstrip().startswith('#') for l in lines if l.strip())
        new_lines = []
        for line in lines:
            if all_commented:
                stripped = line.lstrip()
                indent   = line[:len(line) - len(stripped)]
                new_lines.append(indent + stripped[1:].lstrip() if stripped.startswith('#') else line)
            else:
                new_lines.append('# ' + line)
        cursor.insertText('\u2029'.join(new_lines))

    def _select_next_occurrence(self) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.WordUnderCursor)
            self.setTextCursor(cursor)
            return
        word = cursor.selectedText()
        if not word:
            return
        found = self.document().find(word, cursor.selectionEnd())
        if found.isNull():
            found = self.document().find(word, 0)
        if not found.isNull():
            self.setTextCursor(found)

    def highlight_brackets(self) -> None:
        selections = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(Qt.yellow).lighter(160))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)

        cursor    = self.textCursor()
        doc       = self.document()
        pos       = cursor.position()
        open_br   = "([{"
        close_br  = ")]}"
        pairs_map = {'(': ')', '[': ']', '{': '}', ')': '(', ']': '[', '}': '{'}
        ch = doc.characterAt(pos)
        if ch not in open_br + close_br:
            ch  = doc.characterAt(pos - 1)
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

    def _find_matching_bracket(self, doc, pos: int, ch: str,
                               pairs_map: dict[str, str]) -> int:
        target    = pairs_map.get(ch, '')
        direction = 1 if ch in "([{" else -1
        depth     = 0
        i         = pos
        length    = doc.characterCount()
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

    def highlight_current_line(self) -> None:
        self.highlight_brackets()

    def lineNumberAreaWidth(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 6 + self.fontMetrics().horizontalAdvance('9') * digits

    def updateLineNumberAreaWidth(self, _) -> None:
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)

    def updateLineNumberArea(self, rect, dy: int) -> None:
        if dy:
            self.lineNumberArea.scroll(0, dy)
        else:
            self.lineNumberArea.update(0, rect.y(), self.lineNumberArea.width(), rect.height())

    def lineNumberAreaPaintEvent(self, event) -> None:
        painter      = QPainter(self.lineNumberArea)
        painter.fillRect(event.rect(), QColor("#2d2d2d"))
        block        = self.firstVisibleBlock()
        blockNumber  = block.blockNumber()
        top          = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom       = top + int(self.blockBoundingRect(block).height())
        current_line = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#ffffff") if blockNumber == current_line else QColor("#858585"))
                painter.drawText(0, top, self.lineNumberArea.width() - 4,
                                 self.fontMetrics().height(), Qt.AlignRight, str(blockNumber + 1))
            block       = block.next()
            top         = bottom
            bottom      = top + int(self.blockBoundingRect(block).height())
            blockNumber += 1

# ══════════════════════════════════════════════════════════════════════
#  增量 Viewport 高亮架構
#
#  小檔（< SYNC_THRESHOLD 行）→ highlightBlock() 直接同步
#  大檔                       → 只算 viewport ± VIEWPORT_PADDING 行
#                               結果存進 dict 快取，已算過的行不重算
#                               Worker 只把「viewport 範圍」的結果回傳，
#                               不把幾十萬行的 spans 全塞進記憶體
# ══════════════════════════════════════════════════════════════════════
SYNC_THRESHOLD   = 500    # 低於此行數直接同步高亮
VIEWPORT_PADDING = 300    # viewport 上下各多算幾行當緩衝
CACHE_MAX_LINES  = 5_000  # 快取最多保留幾行，超出時淘汰 viewport 外的舊資料

class _Rule:
    """單條高亮規則（純資料，可跨 thread 傳遞）。"""
    __slots__ = ('pattern', 'color', 'bold', 'italic')

    def __init__(self, pattern: re.Pattern, color: str,
                 bold: bool = False, italic: bool = False) -> None:
        self.pattern = pattern
        self.color   = color
        self.bold    = bold
        self.italic  = italic

class HighlightWorker(QThread):
    """
    子執行緒：只對 [line_offset, line_offset+len(lines)) 這個視窗跑 re。

    回傳的 patch dict 只包含這個視窗的結果，不是整份文件，
    避免 10 MB 大檔把幾十萬行 spans 全部塞進記憶體。

    每行開始前檢查 _abort flag，abort() 後立刻 return 不 emit。
    """
    results_ready = pyqtSignal(dict, int, int)   # (patch, range_start, range_end)

    def __init__(self, lines: list[str], line_offset: int,
                 rules: list[_Rule], parent=None) -> None:
        super().__init__(parent)
        self._lines       = lines
        self._line_offset = line_offset
        self._rules       = rules
        self._abort       = False

    def abort(self) -> None:
        """主執行緒呼叫：設旗標，讓 run() 在下一行跳出。"""
        self._abort = True

    def run(self) -> None:
        patch: dict[int, Spans] = {}
        for i, line in enumerate(self._lines):
            if self._abort:
                return                      # 不 emit，結果丟棄
            spans: Spans = []
            for rule in self._rules:
                for m in rule.pattern.finditer(line):
                    spans.append((m.start(), m.end() - m.start(),
                                  rule.color, rule.bold, rule.italic))
            patch[self._line_offset + i] = spans
        self.results_ready.emit(patch, self._line_offset,
                                self._line_offset + len(self._lines) - 1)

class BaseHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[_Rule]        = []
        self._cache: dict[int, Spans]   = {}   # 行號 → spans，只存 viewport 附近
        self._vp_start: int             = 0
        self._vp_end:   int             = 0
        self._worker: Optional[HighlightWorker] = None

        self._debounce = QTimer()
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._launch_worker)

        self._dirty: set[int] = set()

        self._setup_rules()
        document.contentsChanged.connect(self._on_contents_changed)

    def _setup_rules(self) -> None:
        pass   # 子類覆寫

    @staticmethod
    def _make_fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:   fmt.setFontWeight(QFont.Bold)
        if italic: fmt.setFontItalic(True)
        return fmt

    def _kw(self, words: list[str], color: str = "#569cd6", bold: bool = True) -> None:
        for w in words:
            self._rules.append(_Rule(re.compile(rf"\b{re.escape(w)}\b"), color, bold=bold))

    def _re(self, pattern: str, color: str,
            bold: bool = False, italic: bool = False) -> None:
        self._rules.append(_Rule(re.compile(pattern), color, bold=bold, italic=italic))

    def set_viewport(self, first_line: int, last_line: int) -> None:
        """由 CodeEditor 在 scroll / resize 時呼叫，更新可見範圍。"""
        new_start = max(0, first_line - VIEWPORT_PADDING)
        new_end   = last_line + VIEWPORT_PADDING
        if new_start == self._vp_start and new_end == self._vp_end:
            return
        self._vp_start = new_start
        self._vp_end   = new_end
        self._debounce.start()

    def _on_contents_changed(self) -> None:
        doc = self.document()
        if doc is None or doc.blockCount() < SYNC_THRESHOLD:
            return
        try:
            widget = QApplication.focusWidget()
            if widget and hasattr(widget, 'textCursor'):
                cur_line = widget.textCursor().blockNumber()
                for ln in range(max(0, cur_line - 5), cur_line + 6):
                    self._dirty.add(ln)
                    self._cache.pop(ln, None)
        except Exception:
            pass
        self._debounce.start()

    def _evict_cache(self) -> None:
        """
        把快取限制在 CACHE_MAX_LINES 以內。
        淘汰策略：優先丟掉距離當前 viewport 最遠的行。
        """
        if len(self._cache) <= CACHE_MAX_LINES:
            return
        vp_mid    = (self._vp_start + self._vp_end) / 2
        # 按距離 viewport 中心由遠到近排序，丟掉最遠的
        sorted_keys = sorted(self._cache.keys(), key=lambda ln: -abs(ln - vp_mid))
        evict_count = len(self._cache) - CACHE_MAX_LINES
        for key in sorted_keys[:evict_count]:
            del self._cache[key]

    def _launch_worker(self) -> None:
        doc = self.document()
        if doc is None or doc.blockCount() < SYNC_THRESHOLD:
            return

        # Abort 舊 Worker
        if self._worker and self._worker.isRunning():
            self._worker.abort()
            self._worker.finished.connect(self._worker.deleteLater)
            self._worker = None

        all_lines  = doc.toPlainText().split('\n')
        total      = len(all_lines)
        vp_end     = min(self._vp_end, total - 1)

        # 只算 viewport 範圍內還沒快取的行
        to_compute = [ln for ln in range(self._vp_start, vp_end + 1)
                      if ln not in self._cache]
        self._dirty.clear()

        if not to_compute:
            return

        first       = to_compute[0]
        last        = to_compute[-1]
        # ★ 只切出需要的那段 lines，不傳整份文件給 Worker
        lines_slice = all_lines[first: last + 1]

        worker = HighlightWorker(lines_slice, first, self._rules, self)
        worker.results_ready.connect(self._on_results_ready)
        self._worker = worker
        worker.start()

    def _on_results_ready(self, patch: dict[int, Spans],
                          range_start: int, range_end: int) -> None:
        # ★ patch 只含 viewport 範圍的行，不是全份文件
        self._cache.update(patch)
        self._evict_cache()         # 超出上限就淘汰舊行

        doc = self.document()
        if doc is None:
            return
        block = doc.findBlockByNumber(range_start)
        while block.isValid() and block.blockNumber() <= range_end:
            self.rehighlightBlock(block)
            block = block.next()

    def highlightBlock(self, text: str) -> None:
        block_num  = self.currentBlock().blockNumber()
        line_count = self.document().blockCount()

        if line_count >= SYNC_THRESHOLD:
            spans = self._cache.get(block_num)
            if spans is not None:
                for start, length, color, bold, italic in spans:
                    self.setFormat(start, length, self._make_fmt(color, bold, italic))
            # 快取未命中：留白，等 Worker 補
        else:
            for rule in self._rules:
                for m in rule.pattern.finditer(text):
                    self.setFormat(m.start(), m.end() - m.start(),
                                   self._make_fmt(rule.color, rule.bold, rule.italic))

# ══════════════════════════════════════════════════════════════════════
#  各語言高亮器（只需覆寫 _setup_rules，其餘繼承 BaseHighlighter）
# ══════════════════════════════════════════════════════════════════════
class PythonHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['def','class','if','elif','else','try','except','finally',
                  'while','for','in','import','from','as','return','with','pass',
                  'break','continue','and','or','not','is','lambda',
                  'True','False','None','yield','raise','del','global','nonlocal',
                  'assert','async','await'])
        self._kw(['print','len','range','enumerate','open','type','isinstance',
                  'list','dict','set','tuple','int','str','float','bool',
                  'super','self','zip','map','filter','sorted','reversed',
                  'min','max','sum','abs','round','repr','id','hash',
                  'getattr','setattr','hasattr','callable',
                  'staticmethod','classmethod','property'], color="#dcdcaa", bold=False)
        self._re(r"@\w+",                            "#c586c0")
        self._re(r"\b[0-9]+\.?[0-9]*([eE][+-]?[0-9]+)?\b", "#b5cea8")
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',          "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",          "#ce9178")
        self._re(r"#.*",                              "#6a9955", italic=True)
        self._re(r"\bdef\s+\w+",                     "#dcdcaa")
        self._re(r"\bclass\s+\w+",                   "#4ec9b0")

class JSHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['var','let','const','function','return','if','else','for',
                  'while','do','switch','case','break','continue','class',
                  'import','export','from','new','delete','typeof','instanceof',
                  'this','super','extends','implements','interface','enum',
                  'try','catch','finally','throw','in','of','void',
                  'true','false','null','undefined','NaN','Infinity',
                  'async','await','yield','static','get','set',
                  'type','namespace','declare','abstract','readonly',
                  'public','private','protected','as','keyof','infer'])
        self._kw(['console','Math','Object','Array','String','Number','Boolean',
                  'Promise','fetch','setTimeout','setInterval','Map','Set',
                  'JSON','Date','Error','RegExp'], color="#dcdcaa", bold=False)
        self._re(r'"[^"]*"',                    "#ce9178")
        self._re(r"'[^']*'",                    "#ce9178")
        self._re(r"`[^`]*`",                    "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*\b",       "#b5cea8")
        self._re(r"//.*",                       "#6a9955", italic=True)
        self._re(r"/\*.*?\*/",                  "#6a9955", italic=True)
        self._re(r"\b\w+(?=\s*\()",            "#dcdcaa")
        self._re(r":[\s]*[A-Z]\w*",            "#4ec9b0")

class HTMLHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r"<!--[^>]*-->",              "#6a9955", italic=True)
        self._re(r"</?[\w:-]+",               "#4ec9b0")
        self._re(r">",                          "#4ec9b0")
        self._re(r"\b[\w:-]+(?=\s*=)",        "#9cdcfe")
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r"&[a-zA-Z0-9#]+;",          "#f48771")
        self._re(r"<!DOCTYPE[^>]*>",           "#808080")

class CSSHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"[.#][\w-]+",               "#d7ba7d")
        self._re(r"@[\w-]+",                   "#c586c0")
        self._re(r"\$[\w-]+",                  "#9cdcfe")
        self._re(r"--[\w-]+",                  "#9cdcfe")
        self._re(r"\b[a-z-]+(?=\s*:)",        "#9cdcfe")
        self._re(r"#[0-9a-fA-F]{3,8}\b",      "#ce9178")
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*(px|em|rem|vh|vw|%|pt|s|ms)?\b", "#b5cea8")

class CHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['auto','break','case','char','const','continue','default',
                  'do','double','else','enum','extern','float','for','goto',
                  'if','inline','int','long','register','restrict','return',
                  'short','signed','sizeof','static','struct','switch',
                  'typedef','union','unsigned','void','volatile','while',
                  'bool','class','delete','explicit','false','friend','mutable',
                  'namespace','new','nullptr','operator','private','protected',
                  'public','template','this','throw','true','try','catch',
                  'typeid','typename','using','virtual','override','final',
                  'constexpr','decltype','noexcept','static_assert'])
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*[uUlLfF]*\b", "#b5cea8")
        self._re(r"0x[0-9a-fA-F]+",           "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"#\s*(include|define|ifdef|ifndef|endif|pragma|undef|if|elif|else|error)", "#c586c0")
        self._re(r"<[\w./]+>",                 "#ce9178")
        self._re(r"\b[A-Z][A-Z0-9_]+\b",      "#b5cea8")

class JavaHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
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
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*[lLfFdD]?\b", "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"@\w+",                      "#c586c0")

class CSharpHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
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
        self._re(r'@?"[^"\\]*(\\.[^"\\]*)*"', "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*[uUlLfFdDmM]?\b", "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"///.*",                     "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"\[\w+[^\]]*\]",            "#c586c0")

class GoHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['break','case','chan','const','continue','default','defer',
                  'else','fallthrough','for','func','go','goto','if','import',
                  'interface','map','package','range','return','select','struct',
                  'switch','type','var','true','false','nil','iota'])
        self._kw(['append','cap','close','complex','copy','delete','imag',
                  'len','make','new','panic','print','println','real','recover',
                  'error','string','int','int8','int16','int32','int64',
                  'uint','uint8','uint16','uint32','uint64','uintptr',
                  'float32','float64','complex64','complex128','byte','rune','bool'],
                 color="#4ec9b0", bold=False)
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"`[^`]*`",                   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)

class RustHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['as','async','await','break','const','continue','crate',
                  'dyn','else','enum','extern','false','fn','for','if',
                  'impl','in','let','loop','match','mod','move','mut',
                  'pub','ref','return','self','Self','static','struct',
                  'super','trait','true','type','union','unsafe','use',
                  'where','while'])
        self._kw(['i8','i16','i32','i64','i128','isize',
                  'u8','u16','u32','u64','u128','usize',
                  'f32','f64','bool','char','str','String','Vec',
                  'Option','Result','Box','Rc','Arc','HashMap','HashSet',
                  'println','print','eprintln','panic','assert','assert_eq',
                  'todo','unimplemented','unreachable'], color="#4ec9b0", bold=False)
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"b\"[^\"]*\"",               "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*(_[a-z0-9]+)?\b", "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"#!?\[[^\]]*\]",            "#c586c0")
        self._re(r"'[a-zA-Z_]\w*",            "#569cd6")

class RubyHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['BEGIN','END','alias','and','begin','break','case','class',
                  'def','defined?','do','else','elsif','end','ensure',
                  'false','for','if','in','module','next','nil','not',
                  'or','redo','rescue','retry','return','self','super',
                  'then','true','undef','unless','until','when','while','yield'])
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r":[a-zA-Z_]\w*",            "#569cd6")
        self._re(r"@{1,2}[a-zA-Z_]\w*",      "#9cdcfe")
        self._re(r"\$[a-zA-Z_]\w*",           "#c586c0")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"#.*",                       "#6a9955", italic=True)

class PHPHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
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
        self._re(r"\$[a-zA-Z_]\w*",           "#9cdcfe")
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"#.*",                       "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"<\?php|\?>",               "#c586c0")

class ShellHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['if','then','else','elif','fi','for','while','do','done',
                  'case','esac','function','return','in','until','break',
                  'continue','exit','local','export','source','true','false'])
        self._kw(['echo','printf','read','cd','ls','pwd','mkdir','rm','cp',
                  'mv','cat','grep','sed','awk','find','sort','uniq','wc',
                  'chmod','chown','sudo','apt','yum','pip','python',
                  'git','curl','wget','tar','zip','unzip'], color="#dcdcaa", bold=False)
        self._re(r"\$[{(]?[a-zA-Z_][\w]*[})]?", "#9cdcfe")
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r"`[^`]*`",                   "#c586c0")
        self._re(r"#.*",                       "#6a9955", italic=True)
        self._re(r"\b[0-9]+\b",               "#b5cea8")
        self._re(r"&&|\|\||>>?|<<",           "#c586c0")

class SQLHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        kws = ['SELECT','FROM','WHERE','INSERT','INTO','VALUES','UPDATE',
               'SET','DELETE','CREATE','TABLE','DROP','ALTER','ADD',
               'COLUMN','INDEX','VIEW','DATABASE','SCHEMA','GRANT',
               'REVOKE','COMMIT','ROLLBACK','BEGIN','TRANSACTION',
               'JOIN','LEFT','RIGHT','INNER','OUTER','FULL','CROSS',
               'ON','AS','AND','OR','NOT','IN','IS','NULL','LIKE',
               'BETWEEN','EXISTS','UNION','ALL','DISTINCT','GROUP',
               'BY','ORDER','HAVING','LIMIT','OFFSET','ASC','DESC',
               'PRIMARY','KEY','FOREIGN','REFERENCES','UNIQUE',
               'DEFAULT','CHECK','CONSTRAINT','RETURNING','WITH']
        self._kw(kws + [k.lower() for k in kws])
        self._kw(['COUNT','SUM','AVG','MIN','MAX','COALESCE','NULLIF',
                  'CAST','CONVERT','CONCAT','LENGTH','SUBSTR','UPPER',
                  'LOWER','TRIM','NOW','DATE','YEAR','MONTH','DAY',
                  'count','sum','avg','min','max','coalesce'],
                 color="#dcdcaa", bold=False)
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"--.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)

class JSONHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"\s*:', "#9cdcfe")
        self._re(r':\s*"[^"\\]*(\\.[^"\\]*)*"', "#ce9178")
        self._re(r"\b(true|false|null)\b",       "#569cd6")
        self._re(r":\s*-?[0-9]+\.?[0-9]*([eE][+-]?[0-9]+)?", "#b5cea8")

class YAMLHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r"^---",                      "#c586c0")
        self._re(r"^\s*[\w-]+\s*:",            "#9cdcfe")
        self._re(r":\s*.+",                    "#ce9178")
        self._re(r"^\s*- ",                    "#569cd6")
        self._re(r"&\w+|\*\w+",               "#4ec9b0")
        self._re(r"!\w+",                      "#c586c0")
        self._re(r"#.*",                       "#6a9955", italic=True)
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r"\b(true|false|null|yes|no|on|off)\b", "#569cd6")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")

class MarkdownHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r"^#{1,6}\s.*",              "#569cd6", bold=True)
        self._re(r"\*\*[^*]+\*\*",            "#dcdcaa", bold=True)
        self._re(r"\*[^*]+\*",                "#ce9178", italic=True)
        self._re(r"__[^_]+__",                "#dcdcaa", bold=True)
        self._re(r"_[^_]+_",                  "#ce9178", italic=True)
        self._re(r"`[^`]+`",                   "#4ec9b0")
        self._re(r"^```.*",                    "#c586c0")
        self._re(r"^>.*",                      "#6a9955", italic=True)
        self._re(r"^\s*[-*+]\s",              "#569cd6")
        self._re(r"^\s*[0-9]+\.\s",           "#569cd6")
        self._re(r"\[([^\]]+)\]\([^)]+\)",    "#4ec9b0")
        self._re(r"!\[([^\]]+)\]\([^)]+\)",   "#c586c0")
        self._re(r"^---+$",                    "#808080")

class KotlinHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
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
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r'"""[^"]*"""',               "#ce9178")
        self._re(r"'[^'\\]*(\\.[^'\\]*)*'",   "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*[LFf]?\b", "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"@\w+",                      "#c586c0")

class SwiftHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._kw(['associatedtype','class','deinit','enum','extension',
                  'fileprivate','func','import','init','inout','internal',
                  'let','open','operator','precedencegroup','private',
                  'protocol','public','rethrows','static','struct',
                  'subscript','typealias','var','break','case','catch',
                  'continue','default','defer','do','else','fallthrough',
                  'for','guard','if','in','repeat','return','throw',
                  'switch','where','while','Any','as','false','is',
                  'nil','self','Self','super','throw','throws','true','try',
                  'async','await','actor','nonisolated','some','any'])
        self._re(r'"[^"\\]*(\\.[^"\\]*)*"',   "#ce9178")
        self._re(r'"""[^"]*"""',               "#ce9178")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"//.*",                      "#6a9955", italic=True)
        self._re(r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/", "#6a9955", italic=True)
        self._re(r"@\w+",                      "#c586c0")

class DockerHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        kws = ['FROM','RUN','CMD','LABEL','EXPOSE','ENV','ADD','COPY',
               'ENTRYPOINT','VOLUME','USER','WORKDIR','ARG','ONBUILD',
               'STOPSIGNAL','HEALTHCHECK','SHELL']
        self._kw(kws + [k.lower() for k in kws])
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r"#.*",                       "#6a9955", italic=True)
        self._re(r"\$[{]?[a-zA-Z_]\w*[}]?",   "#9cdcfe")

class TOMLHighlighter(BaseHighlighter):
    def _setup_rules(self) -> None:
        self._re(r"^\[+[^\]]+\]+",            "#569cd6", bold=True)
        self._re(r"^\s*[\w.-]+\s*=",          "#9cdcfe")
        self._re(r'"[^"]*"',                   "#ce9178")
        self._re(r"'[^']*'",                   "#ce9178")
        self._re(r'"""[^"]*"""',               "#ce9178")
        self._re(r"\b(true|false)\b",          "#569cd6")
        self._re(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}", "#4ec9b0")
        self._re(r"\b[0-9]+\.?[0-9]*\b",      "#b5cea8")
        self._re(r"#.*",                       "#6a9955", italic=True)

# ── 副檔名 → 高亮器 對照表 ────────────────────────────────────────────
def get_highlighter_for_file(path: str, document) -> BaseHighlighter:
    ext  = QFileInfo(path).suffix().lower()
    name = QFileInfo(path).fileName().lower()

    NAME_MAP: dict[str, type[BaseHighlighter]] = {
        'dockerfile': DockerHighlighter,
        'makefile':   ShellHighlighter,
        '.bashrc':    ShellHighlighter,
        '.zshrc':     ShellHighlighter,
        '.gitignore': ShellHighlighter,
    }
    if name in NAME_MAP:
        return NAME_MAP[name](document)

    EXT_MAP: dict[frozenset[str], type[BaseHighlighter]] = {
        frozenset(['py','pyw','pyi']):                    PythonHighlighter,
        frozenset(['js','jsx','mjs','cjs','ts','tsx']):   JSHighlighter,
        frozenset(['html','htm','xml','xhtml','svg']):    HTMLHighlighter,
        frozenset(['css','scss','sass','less']):          CSSHighlighter,
        frozenset(['c','h','cpp','cxx','cc','hpp','hxx']): CHighlighter,
        frozenset(['java']):                              JavaHighlighter,
        frozenset(['cs']):                                CSharpHighlighter,
        frozenset(['go']):                                GoHighlighter,
        frozenset(['rs']):                                RustHighlighter,
        frozenset(['rb','rake','gemspec']):               RubyHighlighter,
        frozenset(['php','php3','php4','php5','phtml']): PHPHighlighter,
        frozenset(['sh','bash','zsh','fish','ksh']):      ShellHighlighter,
        frozenset(['sql']):                               SQLHighlighter,
        frozenset(['json','jsonc']):                      JSONHighlighter,
        frozenset(['yaml','yml']):                        YAMLHighlighter,
        frozenset(['md','markdown','mdx']):               MarkdownHighlighter,
        frozenset(['kt','kts']):                          KotlinHighlighter,
        frozenset(['swift']):                             SwiftHighlighter,
        frozenset(['dockerfile']):                        DockerHighlighter,
        frozenset(['toml']):                              TOMLHighlighter,
    }
    for exts, cls in EXT_MAP.items():
        if ext in exts:
            return cls(document)
    return PythonHighlighter(document)




# ══════════════════════════════════════════════════════════════════════
#  插件系統
#
#  架構：
#    PluginSandbox        — 限制 __builtins__，白名單 import
#    PluginAPI            — 插件可呼叫的所有方法
#    PluginManager        — 掃描 plugins/、載入、管理生命週期
#    PluginStore          — 從 GitHub Releases 抓 index.json / 下載
#    PluginManagerDialog  — UI：已安裝 / 商店 / 啟用停用
#
#  插件格式（單一 .py 檔）：
#    PLUGIN_META = {
#        "name": "我的插件",
#        "version": "1.0.0",
#        "description": "做一些很酷的事",
#        "author": "你的名字",
#    }
#    def register(api):
#        api.add_menu_item("工具/做某事", my_func)
#        api.add_shortcut("Ctrl+Shift+X", my_func)
#        api.on_save(lambda path, text: ...)
#        api.on_open(lambda path, text: ...)
#
#  固定來源（GitHub Releases）：
#    PLUGIN_REGISTRY_URL 指向你自己的 repo releases 的 index.json
# ══════════════════════════════════════════════════════════════════════
import importlib.util
import hashlib
import urllib.request
import urllib.error
import zipfile
import shutil
import textwrap

# 固定插件來源：改成你自己的 GitHub repo
PLUGIN_REGISTRY_URL = (
    "https://raw.githubusercontent.com/"
    "yourname/pyeditor-plugins/main/index.json"
)
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

# 允許插件使用的標準函式庫模組白名單
ALLOWED_MODULES: set[str] = {
    "re", "os.path", "json", "math", "datetime", "collections",
    "itertools", "functools", "string", "textwrap", "unicodedata",
    "pathlib", "typing", "dataclasses", "enum", "copy", "time",
}

# ── 沙盒 __builtins__ ──────────────────────────────────────────────────
_SAFE_BUILTINS = {
    k: v for k, v in __builtins__.items()
    if k in {
        "print", "len", "range", "enumerate", "zip", "map", "filter",
        "sorted", "reversed", "list", "dict", "set", "tuple", "str",
        "int", "float", "bool", "bytes", "type", "isinstance", "issubclass",
        "hasattr", "getattr", "setattr", "callable", "repr", "abs",
        "min", "max", "sum", "round", "any", "all", "chr", "ord",
        "hex", "oct", "bin", "hash", "id", "iter", "next", "vars",
        "True", "False", "None", "NotImplemented", "Ellipsis",
        "Exception", "ValueError", "TypeError", "KeyError",
        "IndexError", "AttributeError", "StopIteration",
    }
} if isinstance(__builtins__, dict) else {
    k: getattr(__builtins__, k)
    for k in dir(__builtins__)
    if k in {
        "print", "len", "range", "enumerate", "zip", "map", "filter",
        "sorted", "reversed", "list", "dict", "set", "tuple", "str",
        "int", "float", "bool", "bytes", "type", "isinstance", "issubclass",
        "hasattr", "getattr", "setattr", "callable", "repr", "abs",
        "min", "max", "sum", "round", "any", "all", "chr", "ord",
        "hex", "oct", "bin", "hash", "id", "iter", "next", "vars",
        "True", "False", "None", "NotImplemented", "Ellipsis",
        "Exception", "ValueError", "TypeError", "KeyError",
        "IndexError", "AttributeError", "StopIteration",
    }
}


class PluginSandbox:
    """
    在受限的執行環境中載入插件。
    - __builtins__ 限制在白名單
    - __import__ 只允許 ALLOWED_MODULES 中的模組
    - 不允許 open()、exec()、eval()、__import__ 任意模組
    """

    @staticmethod
    def _make_restricted_import(allowed: set[str]):
        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            base = name.split(".")[0]
            if name not in allowed and base not in allowed:
                raise ImportError(
                    f"插件不允許 import '{name}'。"
                    f"允許的模組：{sorted(allowed)}"
                )
            return __import__(name, globals, locals, fromlist, level)
        return _import

    @classmethod
    def execute(cls, source: str, filename: str, api: "PluginAPI") -> dict:
        """
        在沙盒中執行插件 source，回傳插件的 globals dict。
        插件應在 globals 中定義 PLUGIN_META 和 register(api)。
        """
        safe_globals: dict = {
            "__builtins__": {
                **_SAFE_BUILTINS,
                "__import__": cls._make_restricted_import(ALLOWED_MODULES),
            },
            "__name__":   filename,
            "__file__":   filename,
        }
        try:
            code = compile(source, filename, "exec")
            exec(code, safe_globals)
        except ImportError as e:
            raise
        except Exception as e:
            raise RuntimeError(f"插件載入錯誤：{e}") from e

        # 呼叫 register(api)
        register_fn = safe_globals.get("register")
        if callable(register_fn):
            try:
                register_fn(api)
            except Exception as e:
                raise RuntimeError(f"插件 register() 錯誤：{e}") from e

        return safe_globals


# ── 插件 API ──────────────────────────────────────────────────────────
class PluginAPI:
    """
    插件能呼叫的所有方法。
    主視窗的內部邏輯不暴露給插件，插件只能透過這個介面操作。
    """

    def __init__(self, main_window: "MainWindow") -> None:
        self._mw             = main_window
        self._menu_actions:  list[QAction]   = []
        self._shortcuts:     list[QShortcut] = []
        self._on_save_cbs:   list            = []
        self._on_open_cbs:   list            = []

    # ── 讀寫編輯器內容 ────────────────────────────────────────────────
    def get_text(self) -> str:
        editor = self._mw._current_editor()
        return editor.toPlainText() if editor else ""

    def set_text(self, text: str) -> None:
        editor = self._mw._current_editor()
        if editor:
            cursor = editor.textCursor()
            cursor.select(QTextCursor.Document)
            cursor.insertText(text)

    def get_selection(self) -> str:
        editor = self._mw._current_editor()
        return editor.textCursor().selectedText() if editor else ""

    def set_selection(self, text: str) -> None:
        editor = self._mw._current_editor()
        if editor:
            editor.textCursor().insertText(text)

    def get_cursor_position(self) -> tuple[int, int]:
        editor = self._mw._current_editor()
        if not editor:
            return (0, 0)
        c = editor.textCursor()
        return (c.blockNumber(), c.columnNumber())

    def get_file_path(self) -> str:
        tab = self._mw._current_tab()
        return getattr(tab, "file_path", "") if tab else ""

    # ── UI 擴充 ───────────────────────────────────────────────────────
    def add_menu_item(self, path: str, callback, shortcut: str = "") -> None:
        """
        在選單中加入項目。
        path 格式：「工具/格式化代碼」→ 在「工具」選單下加「格式化代碼」。
        若最上層選單不存在則自動建立。
        """
        parts    = path.split("/", 1)
        top_name = parts[0]
        item_name = parts[1] if len(parts) > 1 else parts[0]

        menubar = self._mw.menuBar()
        # 找或建立頂層選單
        target_menu = None
        for action in menubar.actions():
            if action.text() == top_name:
                target_menu = action.menu()
                break
        if target_menu is None:
            target_menu = menubar.addMenu(top_name)

        action = QAction(item_name, self._mw)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(lambda: self._safe_call(callback))
        target_menu.addAction(action)
        self._menu_actions.append(action)

    def add_shortcut(self, key: str, callback) -> None:
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        sc = QShortcut(QKeySequence(key), self._mw)
        sc.activated.connect(lambda: self._safe_call(callback))
        self._shortcuts.append(sc)

    def show_message(self, title: str, text: str) -> None:
        QMessageBox.information(self._mw, title, text)

    def show_status(self, text: str, timeout: int = 3000) -> None:
        self._mw.status_bar.showMessage(text, timeout)

    def ask_input(self, title: str, prompt: str, default: str = "") -> Optional[str]:
        text, ok = QInputDialog.getText(self._mw, title, prompt, text=default)
        return text if ok else None

    # ── 事件 hooks ────────────────────────────────────────────────────
    def on_save(self, callback) -> None:
        """callback(path: str, text: str) -> None"""
        self._on_save_cbs.append(callback)

    def on_open(self, callback) -> None:
        """callback(path: str, text: str) -> None"""
        self._on_open_cbs.append(callback)

    # ── 內部：觸發 hooks（由 PluginManager 呼叫）─────────────────────
    def _fire_save(self, path: str, text: str) -> None:
        for cb in self._on_save_cbs:
            self._safe_call(cb, path, text)

    def _fire_open(self, path: str, text: str) -> None:
        for cb in self._on_open_cbs:
            self._safe_call(cb, path, text)

    def _safe_call(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            QMessageBox.warning(
                self._mw, "插件錯誤",
                f"插件執行時發生錯誤：\n{e}")

    def _unregister(self) -> None:
        """移除這個插件加入的所有 UI 元素。"""
        menubar = self._mw.menuBar()
        for action in self._menu_actions:
            for bar_action in menubar.actions():
                m = bar_action.menu()
                if m and action in m.actions():
                    m.removeAction(action)
        self._menu_actions.clear()
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts.clear()
        self._on_save_cbs.clear()
        self._on_open_cbs.clear()


# ── 單個插件的狀態 ─────────────────────────────────────────────────────
class PluginRecord:
    __slots__ = ("path", "meta", "api", "enabled", "source_hash")

    def __init__(self, path: str, meta: dict, api: PluginAPI,
                 source_hash: str) -> None:
        self.path        = path
        self.meta        = meta          # PLUGIN_META dict
        self.api         = api
        self.enabled     = True
        self.source_hash = source_hash   # sha256，用於偵測插件更新


# ── 插件管理器 ─────────────────────────────────────────────────────────
class PluginManager:
    """
    掃描 plugins/ 目錄，在沙盒中載入每個 .py 插件。
    提供 fire_save / fire_open 讓 MainWindow 觸發事件。
    """

    def __init__(self, main_window: "MainWindow") -> None:
        self._mw      = main_window
        self._plugins: dict[str, PluginRecord] = {}   # path → record
        self._enabled_setting_key = "plugin_enabled"
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        self._load_all()

    def _load_all(self) -> None:
        if not os.path.isdir(PLUGINS_DIR):
            return
        for fname in sorted(os.listdir(PLUGINS_DIR)):
            if fname.endswith(".py"):
                fpath = os.path.join(PLUGINS_DIR, fname)
                self._load_one(fpath, show_error=False)

    def _load_one(self, path: str, show_error: bool = True) -> Optional[PluginRecord]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception as e:
            if show_error:
                QMessageBox.warning(self._mw, "插件載入失敗", f"{path}\n{e}")
            return None

        src_hash = hashlib.sha256(source.encode()).hexdigest()

        # 若已載入且 hash 沒變，不重新載入
        existing = self._plugins.get(path)
        if existing and existing.source_hash == src_hash:
            return existing

        api = PluginAPI(self._mw)
        try:
            glb = PluginSandbox.execute(source, path, api)
        except Exception as e:
            if show_error:
                QMessageBox.warning(self._mw, "插件錯誤", f"{os.path.basename(path)}\n{e}")
            return None

        meta = glb.get("PLUGIN_META", {"name": os.path.basename(path)})
        rec  = PluginRecord(path, meta, api, src_hash)

        # 讀取上次的啟用狀態
        settings = QSettings("PyEditor", "PyEditor")
        key = f"{self._enabled_setting_key}/{os.path.basename(path)}"
        rec.enabled = settings.value(key, True, type=bool)
        if not rec.enabled:
            api._unregister()

        self._plugins[path] = rec
        return rec

    def reload(self, path: str) -> None:
        if path in self._plugins:
            self._plugins[path].api._unregister()
            del self._plugins[path]
        self._load_one(path)

    def uninstall(self, path: str) -> None:
        if path in self._plugins:
            self._plugins[path].api._unregister()
            del self._plugins[path]
        try:
            os.remove(path)
        except Exception:
            pass

    def set_enabled(self, path: str, enabled: bool) -> None:
        rec = self._plugins.get(path)
        if not rec:
            return
        rec.enabled = enabled
        settings = QSettings("PyEditor", "PyEditor")
        settings.setValue(
            f"{self._enabled_setting_key}/{os.path.basename(path)}", enabled)
        if not enabled:
            rec.api._unregister()
        else:
            # 重新載入以重新 register
            self.reload(path)

    def all_plugins(self) -> list[PluginRecord]:
        return list(self._plugins.values())

    def fire_save(self, path: str, text: str) -> None:
        for rec in self._plugins.values():
            if rec.enabled:
                rec.api._fire_save(path, text)

    def fire_open(self, path: str, text: str) -> None:
        for rec in self._plugins.values():
            if rec.enabled:
                rec.api._fire_open(path, text)

    def shutdown(self) -> None:
        for rec in self._plugins.values():
            rec.api._unregister()
        self._plugins.clear()


# ── 插件商店（GitHub Releases 下載）────────────────────────────────────
class PluginStoreWorker(QThread):
    """在背景抓取 index.json 和下載插件，不卡 UI。"""
    index_ready    = pyqtSignal(list)    # list of {name, description, version, url, sha256}
    download_done  = pyqtSignal(str, str)  # plugin_name, local_path
    error_occurred = pyqtSignal(str)

    def __init__(self, mode: str, url: str = "", dest: str = "") -> None:
        super().__init__()
        self._mode = mode   # "fetch_index" | "download"
        self._url  = url
        self._dest = dest

    def run(self) -> None:
        try:
            if self._mode == "fetch_index":
                self._fetch_index()
            elif self._mode == "download":
                self._download()
        except Exception as e:
            self.error_occurred.emit(str(e))

    def _fetch_index(self) -> None:
        try:
            with urllib.request.urlopen(PLUGIN_REGISTRY_URL, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise RuntimeError(f"無法連線到插件倉庫：{e}")
        plugins = data.get("plugins", [])
        self.index_ready.emit(plugins)

    def _download(self) -> None:
        try:
            with urllib.request.urlopen(self._url, timeout=30) as resp:
                content = resp.read()
        except urllib.error.URLError as e:
            raise RuntimeError(f"下載失敗：{e}")

        os.makedirs(PLUGINS_DIR, exist_ok=True)
        fname = self._dest or os.path.basename(self._url.split("?")[0])
        if not fname.endswith(".py"):
            fname += ".py"
        fpath = os.path.join(PLUGINS_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(content)

        plugin_name = os.path.splitext(fname)[0]
        self.download_done.emit(plugin_name, fpath)


# ── 插件管理 UI ────────────────────────────────────────────────────────
class PluginManagerDialog(QDialog):
    """
    分兩個 Tab：
      已安裝 — 列出本機插件，可啟用/停用/移除/重新載入
      商店   — 從 GitHub Releases 瀏覽並安裝插件
    """

    def __init__(self, plugin_manager: PluginManager,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pm     = plugin_manager
        self._worker: Optional[PluginStoreWorker] = None

        self.setWindowTitle("插件管理器")
        self.setMinimumSize(680, 480)
        self.resize(760, 520)

        from PyQt5.QtWidgets import QTabWidget as _Tabs
        tabs = _Tabs()
        tabs.addTab(self._build_installed_tab(), "已安裝")
        tabs.addTab(self._build_store_tab(),     "插件商店")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

        info = QLabel(
            f"插件目錄：{PLUGINS_DIR}  |  "
            f"固定來源：{PLUGIN_REGISTRY_URL}"
        )
        info.setStyleSheet("color:#858585; font-size:11px;")
        info.setWordWrap(True)
        layout.addWidget(info)

    # ── 已安裝 Tab ────────────────────────────────────────────────────
    def _build_installed_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        self._installed_list = QListWidget()
        self._installed_list.currentRowChanged.connect(self._on_installed_select)
        v.addWidget(self._installed_list)

        btn_row = QHBoxLayout()
        self._btn_toggle  = QPushButton("停用")
        self._btn_reload  = QPushButton("重新載入")
        self._btn_remove  = QPushButton("移除")
        self._btn_open_dir = QPushButton("開啟插件目錄")
        for btn in (self._btn_toggle, self._btn_reload,
                    self._btn_remove, self._btn_open_dir):
            btn_row.addWidget(btn)

        self._btn_toggle.clicked.connect(self._toggle_plugin)
        self._btn_reload.clicked.connect(self._reload_plugin)
        self._btn_remove.clicked.connect(self._remove_plugin)
        self._btn_open_dir.clicked.connect(
            lambda: self._open_path(PLUGINS_DIR))
        v.addLayout(btn_row)

        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("color:#858585; font-size:12px;")
        v.addWidget(self._detail_label)

        self._refresh_installed()
        return w

    def _refresh_installed(self) -> None:
        self._installed_list.clear()
        for rec in self._pm.all_plugins():
            name    = rec.meta.get("name", os.path.basename(rec.path))
            version = rec.meta.get("version", "?")
            status  = "✔" if rec.enabled else "✖"
            item    = QListWidgetItem(f"{status}  {name}  v{version}")
            item.setData(Qt.UserRole, rec.path)
            if not rec.enabled:
                item.setForeground(QColor("#858585"))
            self._installed_list.addItem(item)

    def _current_rec(self) -> Optional[PluginRecord]:
        item = self._installed_list.currentItem()
        if not item:
            return None
        path = item.data(Qt.UserRole)
        return next((r for r in self._pm.all_plugins() if r.path == path), None)

    def _on_installed_select(self) -> None:
        rec = self._current_rec()
        if not rec:
            self._detail_label.setText("")
            return
        meta = rec.meta
        self._detail_label.setText(
            f"{meta.get('description', '無說明')}  "
            f"— {meta.get('author', '未知作者')}"
        )
        self._btn_toggle.setText("啟用" if not rec.enabled else "停用")

    def _toggle_plugin(self) -> None:
        rec = self._current_rec()
        if rec:
            self._pm.set_enabled(rec.path, not rec.enabled)
            self._refresh_installed()

    def _reload_plugin(self) -> None:
        rec = self._current_rec()
        if rec:
            self._pm.reload(rec.path)
            self._refresh_installed()

    def _remove_plugin(self) -> None:
        rec = self._current_rec()
        if not rec:
            return
        name = rec.meta.get("name", os.path.basename(rec.path))
        reply = QMessageBox.question(
            self, "確認移除", f"確定要移除插件「{name}」嗎？",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self._pm.uninstall(rec.path)
            self._refresh_installed()

    # ── 商店 Tab ──────────────────────────────────────────────────────
    def _build_store_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        self._store_status = QLabel("點「重新整理」從固定來源載入插件列表")
        self._store_status.setStyleSheet("color:#858585; font-size:12px;")
        v.addWidget(self._store_status)

        self._store_list = QListWidget()
        self._store_list.currentRowChanged.connect(self._on_store_select)
        v.addWidget(self._store_list)

        self._store_detail = QLabel("")
        self._store_detail.setWordWrap(True)
        self._store_detail.setStyleSheet("color:#858585; font-size:12px;")
        v.addWidget(self._store_detail)

        btn_row = QHBoxLayout()
        btn_refresh  = QPushButton("重新整理")
        self._btn_install = QPushButton("安裝")
        self._btn_install.setEnabled(False)
        btn_refresh.clicked.connect(self._fetch_store_index)
        self._btn_install.clicked.connect(self._install_selected)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(self._btn_install)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._store_index: list[dict] = []
        return w

    def _fetch_store_index(self) -> None:
        self._store_status.setText("連線中…")
        self._store_list.clear()
        self._btn_install.setEnabled(False)
        worker = PluginStoreWorker("fetch_index")
        worker.index_ready.connect(self._on_index_ready)
        worker.error_occurred.connect(
            lambda e: self._store_status.setText(f"錯誤：{e}"))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_index_ready(self, plugins: list) -> None:
        self._store_index = plugins
        self._store_list.clear()
        installed_names = {
            os.path.splitext(os.path.basename(r.path))[0]
            for r in self._pm.all_plugins()
        }
        for p in plugins:
            name     = p.get("name", "?")
            version  = p.get("version", "?")
            tag      = " [已安裝]" if name in installed_names else ""
            item     = QListWidgetItem(f"{name}  v{version}{tag}")
            item.setData(Qt.UserRole, p)
            if tag:
                item.setForeground(QColor("#858585"))
            self._store_list.addItem(item)
        count = len(plugins)
        self._store_status.setText(f"找到 {count} 個插件（來源：{PLUGIN_REGISTRY_URL}）")

    def _on_store_select(self) -> None:
        item = self._store_list.currentItem()
        if not item:
            self._store_detail.setText("")
            self._btn_install.setEnabled(False)
            return
        p = item.data(Qt.UserRole)
        self._store_detail.setText(
            f"{p.get('description', '無說明')}  — {p.get('author', '未知')}")
        self._btn_install.setEnabled(True)

    def _install_selected(self) -> None:
        item = self._store_list.currentItem()
        if not item:
            return
        p    = item.data(Qt.UserRole)
        url  = p.get("url", "")
        name = p.get("name", "plugin")
        if not url:
            QMessageBox.warning(self, "錯誤", "此插件沒有下載連結。")
            return

        self._btn_install.setEnabled(False)
        self._store_status.setText(f"下載中：{name}…")
        worker = PluginStoreWorker("download", url=url, dest=f"{name}.py")
        worker.download_done.connect(self._on_download_done)
        worker.error_occurred.connect(self._on_download_error)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_download_done(self, name: str, path: str) -> None:
        self._pm.reload(path)
        self._store_status.setText(f"已安裝：{name}")
        self._btn_install.setEnabled(True)
        QMessageBox.information(self, "安裝完成", f"插件「{name}」已安裝並載入。")

    def _on_download_error(self, err: str) -> None:
        self._store_status.setText(f"下載失敗：{err}")
        self._btn_install.setEnabled(True)
        QMessageBox.warning(self, "下載失敗", err)

    @staticmethod
    def _open_path(path: str) -> None:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])


# ══════════════════════════════════════════════════════════════════════
#  LSP（Language Server Protocol）
#
#  架構：
#    LspClient  (QThread) — 一個語言一個，管 JSON-RPC stdio pipe
#    LspManager           — 根據副檔名分派到對應 LspClient
#    DiagnosticOverlay    — 在 CodeEditor 上疊加波浪底線 + 側欄列表
#
#  支援語言 / server：
#    Python  → pylsp          (pip install python-lsp-server)
#    JS/TS   → typescript-language-server  (npm i -g typescript-language-server typescript)
#    C/C++   → clangd         (系統套件管理器安裝)
#    Rust    → rust-analyzer  (rustup component add rust-analyzer)
# ══════════════════════════════════════════════════════════════════════
import json
import threading

# ── LSP severity 對應 ──────────────────────────────────────────────────
LSP_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
LSP_SEVERITY_COLOR = {
    "error":   "#f48771",
    "warning": "#cca700",
    "info":    "#75beff",
    "hint":    "#b5cea8",
}

# ── 各語言 server 啟動指令 ─────────────────────────────────────────────
LSP_SERVER_CMDS: dict[str, list[str]] = {
    "python":     ["pylsp"],
    "javascript": ["typescript-language-server", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "c":          ["clangd"],
    "cpp":        ["clangd"],
    "rust":       ["rust-analyzer"],
}

# 副檔名 → language id
EXT_TO_LANG: dict[str, str] = {
    "py":   "python",
    "pyw":  "python",
    "js":   "javascript",
    "jsx":  "javascript",
    "mjs":  "javascript",
    "ts":   "typescript",
    "tsx":  "typescript",
    "c":    "c",
    "h":    "c",
    "cpp":  "cpp",
    "cxx":  "cpp",
    "cc":   "cpp",
    "hpp":  "cpp",
    "rs":   "rust",
}


class LspClient(QThread):
    """
    單一 language server 的通訊層。
    負責：
      - 啟動 server subprocess（stdio）
      - 讀寫 JSON-RPC（Content-Length header framing）
      - 把收到的 notification / response 轉成 Qt signal
    """
    # 診斷更新：uri, list of {range, severity, message}
    diagnostics_received = pyqtSignal(str, list)
    # completion 結果：request_id, list of {label, detail, insertText}
    completion_received  = pyqtSignal(int, list)
    # hover 結果：request_id, str (markdown)
    hover_received       = pyqtSignal(int, str)
    # definition 結果：request_id, list of {uri, line, character}
    definition_received  = pyqtSignal(int, list)
    # server 掛掉或找不到
    server_error         = pyqtSignal(str)

    def __init__(self, language: str, cmd: list[str],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.language    = language
        self._cmd        = cmd
        self._proc: Optional[subprocess.Popen] = None
        self._req_id     = 0
        self._lock       = threading.Lock()
        self._abort      = False
        self._initialized = False
        self._pending: dict[int, str] = {}   # request_id → method

    # ── 外部 API（主執行緒呼叫）──────────────────────────────────────────

    def initialize(self, root_uri: str) -> None:
        self._send("initialize", {
            "processId": os.getpid(),
            "rootUri":   root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {
                        "completionItem": {"snippetSupport": False}
                    },
                    "hover":      {"contentFormat": ["plaintext", "markdown"]},
                    "definition": {},
                    "publishDiagnostics": {},
                }
            },
            "initializationOptions": {},
        })

    def did_open(self, uri: str, language_id: str, text: str) -> None:
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri":        uri,
                "languageId": language_id,
                "version":    1,
                "text":       text,
            }
        })

    def did_change(self, uri: str, version: int, text: str) -> None:
        self._notify("textDocument/didChange", {
            "textDocument":   {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        })

    def did_close(self, uri: str) -> None:
        self._notify("textDocument/didClose", {
            "textDocument": {"uri": uri}
        })

    def request_completion(self, uri: str, line: int, character: int) -> int:
        return self._send("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position":     {"line": line, "character": character},
        })

    def request_hover(self, uri: str, line: int, character: int) -> int:
        return self._send("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position":     {"line": line, "character": character},
        })

    def request_definition(self, uri: str, line: int, character: int) -> int:
        return self._send("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position":     {"line": line, "character": character},
        })

    def shutdown_server(self) -> None:
        self._abort = True
        try:
            self._send("shutdown", {})
            self._notify("exit", {})
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    # ── 內部：JSON-RPC 讀寫 ───────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _send(self, method: str, params: dict) -> int:
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "id":      req_id,
            "method":  method,
            "params":  params,
        }
        self._pending[req_id] = method
        self._write(msg)
        return req_id

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, obj: dict) -> None:
        if not self._proc or self._proc.stdin is None:
            return
        body = json.dumps(obj).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            with self._lock:
                self._proc.stdin.write(header + body)
                self._proc.stdin.flush()
        except Exception:
            pass

    def _read_message(self) -> Optional[dict]:
        """從 stdout 讀一條完整的 JSON-RPC 訊息。"""
        if not self._proc or self._proc.stdout is None:
            return None
        try:
            # 讀 header
            headers = {}
            while True:
                raw = self._proc.stdout.readline()
                if not raw:
                    return None
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    break
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()

            length = int(headers.get("Content-Length", 0))
            if length == 0:
                return None
            body = self._proc.stdout.read(length)
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    # ── QThread.run：讀取 loop ────────────────────────────────────────────

    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            self.server_error.emit(
                f"找不到 {self._cmd[0]}，請確認已安裝並在 PATH 中。")
            return

        # 主讀取 loop
        while not self._abort:
            msg = self._read_message()
            if msg is None:
                break
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        """根據訊息類型分派處理。"""
        method = msg.get("method", "")
        msg_id = msg.get("id")

        # Notification（server 主動推送）
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri    = params.get("uri", "")
            diags  = [
                {
                    "line":     d["range"]["start"]["line"],
                    "char":     d["range"]["start"]["character"],
                    "end_line": d["range"]["end"]["line"],
                    "end_char": d["range"]["end"]["character"],
                    "severity": LSP_SEVERITY.get(d.get("severity", 1), "error"),
                    "message":  d.get("message", ""),
                    "source":   d.get("source", ""),
                }
                for d in params.get("diagnostics", [])
            ]
            self.diagnostics_received.emit(uri, diags)
            return

        # initialize 回應 → 送 initialized notification
        if method == "" and msg_id is not None and not self._initialized:
            self._initialized = True
            self._notify("initialized", {})
            return

        # Response
        if msg_id is not None and msg_id in self._pending:
            origin_method = self._pending.pop(msg_id)
            result        = msg.get("result")
            if result is None:
                return

            if origin_method == "textDocument/completion":
                items = result if isinstance(result, list) else result.get("items", [])
                parsed = [
                    {
                        "label":      i.get("label", ""),
                        "detail":     i.get("detail", ""),
                        "insertText": i.get("insertText") or i.get("label", ""),
                    }
                    for i in items[:80]   # 最多 80 筆避免 UI 卡頓
                ]
                self.completion_received.emit(msg_id, parsed)

            elif origin_method == "textDocument/hover":
                content = result.get("contents", "")
                if isinstance(content, dict):
                    text = content.get("value", "")
                elif isinstance(content, list):
                    text = "\n".join(
                        c.get("value", c) if isinstance(c, dict) else str(c)
                        for c in content
                    )
                else:
                    text = str(content)
                self.hover_received.emit(msg_id, text)

            elif origin_method == "textDocument/definition":
                locs = result if isinstance(result, list) else ([result] if result else [])
                parsed = [
                    {
                        "uri":       loc.get("uri", ""),
                        "line":      loc["range"]["start"]["line"],
                        "character": loc["range"]["start"]["character"],
                    }
                    for loc in locs if "range" in loc
                ]
                self.definition_received.emit(msg_id, parsed)


class LspManager:
    """
    管理所有語言的 LspClient，提供統一介面給 MainWindow 使用。
    根據副檔名自動選擇並啟動對應的 server（懶初始化）。
    """
    def __init__(self, main_window: MainWindow) -> None:
        self._mw:      MainWindow              = main_window
        self._clients: dict[str, LspClient]   = {}
        # uri → version（for didChange）
        self._versions: dict[str, int]        = {}

    def _get_client(self, language: str) -> Optional[LspClient]:
        if language not in LSP_SERVER_CMDS:
            return None
        if language not in self._clients:
            cmd    = LSP_SERVER_CMDS[language]
            client = LspClient(language, cmd)
            client.diagnostics_received.connect(self._on_diagnostics)
            client.completion_received.connect(self._on_completion)
            client.hover_received.connect(self._on_hover)
            client.definition_received.connect(self._on_definition)
            client.server_error.connect(self._on_server_error)
            client.start()
            # 用工作目錄當 rootUri
            root = QUrl.fromLocalFile(os.getcwd()).toString()
            client.initialize(root)
            self._clients[language] = client
        return self._clients[language]

    # ── 對外 API ──────────────────────────────────────────────────────────

    def open_document(self, path: str, text: str) -> None:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._get_client(lang) if lang else None
        if not client:
            return
        uri = QUrl.fromLocalFile(path).toString()
        self._versions[uri] = 1
        client.did_open(uri, lang, text)

    def change_document(self, path: str, text: str) -> None:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._clients.get(lang)
        if not client:
            return
        uri = QUrl.fromLocalFile(path).toString()
        self._versions[uri] = self._versions.get(uri, 0) + 1
        client.did_change(uri, self._versions[uri], text)

    def close_document(self, path: str) -> None:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._clients.get(lang)
        if not client:
            return
        uri = QUrl.fromLocalFile(path).toString()
        client.did_close(uri)
        self._versions.pop(uri, None)

    def request_completion(self, path: str, line: int, character: int) -> Optional[int]:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._clients.get(lang)
        if not client:
            return None
        uri = QUrl.fromLocalFile(path).toString()
        return client.request_completion(uri, line, character)

    def request_hover(self, path: str, line: int, character: int) -> Optional[int]:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._clients.get(lang)
        if not client:
            return None
        uri = QUrl.fromLocalFile(path).toString()
        return client.request_hover(uri, line, character)

    def request_definition(self, path: str, line: int, character: int) -> Optional[int]:
        lang   = EXT_TO_LANG.get(QFileInfo(path).suffix().lower())
        client = self._clients.get(lang)
        if not client:
            return None
        uri = QUrl.fromLocalFile(path).toString()
        return client.request_definition(uri, line, character)

    def shutdown_all(self) -> None:
        for client in self._clients.values():
            client.shutdown_server()
            client.wait(3000)
        self._clients.clear()

    # ── Slots（接收 LspClient 的 signal）────────────────────────────────

    def _on_diagnostics(self, uri: str, diags: list) -> None:
        path = QUrl(uri).toLocalFile()
        # 找到對應的 editor
        for tab, editor in self._mw.editor_widgets.items():
            if getattr(tab, "file_path", "") == path:
                editor.set_diagnostics(diags)
                self._mw.diagnostics_panel.update_file(path, diags)
                break

    def _on_completion(self, req_id: int, items: list) -> None:
        editor = self._mw._current_editor()
        if editor:
            editor.apply_lsp_completion(items)

    def _on_hover(self, req_id: int, text: str) -> None:
        editor = self._mw._current_editor()
        if editor and text.strip():
            from PyQt5.QtWidgets import QToolTip
            QToolTip.showText(editor.mapToGlobal(editor.cursorRect().bottomLeft()),
                              text.strip()[:500], editor)

    def _on_definition(self, req_id: int, locations: list) -> None:
        if not locations:
            return
        loc  = locations[0]
        path = QUrl(loc["uri"]).toLocalFile()
        line = loc["line"]
        char = loc["character"]
        self._mw.open_file(path)
        editor = self._mw._current_editor()
        if editor:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(line):
                cursor.movePosition(QTextCursor.Down)
            cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, char)
            editor.setTextCursor(cursor)
            editor.setFocus()

    def _on_server_error(self, msg: str) -> None:
        QMessageBox.warning(self._mw, "LSP 錯誤", msg)


# ══════════════════════════════════════════════════════════════════════
#  診斷面板（側欄列表）
# ══════════════════════════════════════════════════════════════════════
class DiagnosticsPanel(QWidget):
    """
    顯示所有開啟檔案的診斷結果（錯誤/警告清單）。
    點擊項目跳到對應位置。
    """
    jump_requested = pyqtSignal(str, int, int)   # path, line, char

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel("診斷")
        header.setStyleSheet(
            "font-weight:bold; padding:4px 8px; background:#252526; color:#cccccc;")
        layout.addWidget(header)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background:#1e1e1e; color:#d4d4d4; border:none; font-size:12px; }"
            "QListWidget::item:selected { background:#094771; }"
        )
        self._list.itemClicked.connect(self._on_clicked)
        layout.addWidget(self._list)

        # path → list of diag
        self._data: dict[str, list] = {}

    def update_file(self, path: str, diags: list) -> None:
        self._data[path] = diags
        self._rebuild()

    def clear_file(self, path: str) -> None:
        self._data.pop(path, None)
        self._rebuild()

    def _rebuild(self) -> None:
        self._list.clear()
        for path, diags in self._data.items():
            fname = QFileInfo(path).fileName()
            for d in diags:
                sev   = d.get("severity", "error")
                icon  = {"error": "✖", "warning": "⚠", "info": "ℹ", "hint": "○"}.get(sev, "•")
                color = LSP_SEVERITY_COLOR.get(sev, "#d4d4d4")
                item  = QListWidgetItem(
                    f"{icon} {fname}:{d['line']+1}:{d['char']+1}  {d['message']}")
                item.setForeground(QColor(color))
                item.setData(Qt.UserRole, (path, d["line"], d["char"]))
                self._list.addItem(item)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        path, line, char = item.data(Qt.UserRole)
        self.jump_requested.emit(path, line, char)

# ══════════════════════════════════════════════════════════════════════
#  影音播放器
# ══════════════════════════════════════════════════════════════════════
class MediaPlayerDialog(QDialog):
    """
    內建影音播放器。
    支援：影片（mp4/mkv/avi/mov）、音樂（mp3/wav/flac/ogg/aac）。
    需要 PyQt5.QtMultimedia（pip install PyQt5 時通常已包含）。
    Windows 上需要安裝 K-Lite Codec Pack 或 LAV Filters 才能播放影片。
    """
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("影音播放器")
        self.setMinimumSize(640, 480)
        self.resize(800, 560)

        if not HAS_MULTIMEDIA:
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(
                "❌ 缺少 PyQt5.QtMultimedia\n\n"
                "請執行：pip install PyQt5\n"
                "Windows 另需安裝 K-Lite Codec Pack 才能播放影片。"
            ))
            return

        self._player = QMediaPlayer(self)
        self._player.stateChanged.connect(self._on_state_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.error.connect(self._on_error)

        # ── 影像區 ──
        self._video = QVideoWidget(self)
        self._player.setVideoOutput(self._video)

        # ── 控制列 ──
        ctrl = QHBoxLayout()

        self._btn_open = QPushButton("開啟")
        self._btn_open.clicked.connect(self.open_file)
        ctrl.addWidget(self._btn_open)

        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedWidth(40)
        self._btn_play.clicked.connect(self.toggle_play)
        ctrl.addWidget(self._btn_play)

        self._btn_stop = QPushButton("■")
        self._btn_stop.setFixedWidth(40)
        self._btn_stop.clicked.connect(self.stop)
        ctrl.addWidget(self._btn_stop)

        # 進度條
        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.sliderMoved.connect(self._player.setPosition)
        ctrl.addWidget(self._seek, stretch=1)

        self._lbl_time = QLabel("00:00 / 00:00")
        self._lbl_time.setFixedWidth(110)
        ctrl.addWidget(self._lbl_time)

        # 音量
        ctrl.addWidget(QLabel("🔊"))
        self._vol = QSlider(Qt.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setValue(80)
        self._vol.setFixedWidth(80)
        self._vol.valueChanged.connect(self._player.setVolume)
        ctrl.addWidget(self._vol)
        self._player.setVolume(80)

        # ── 檔名標籤 ──
        self._lbl_file = QLabel("尚未開啟檔案")
        self._lbl_file.setStyleSheet("color: #858585; font-size: 12px;")

        # ── 錯誤標籤 ──
        self._lbl_err = QLabel("")
        self._lbl_err.setStyleSheet("color: #f48771;")
        self._lbl_err.hide()

        # ── 整體佈局 ──
        layout = QVBoxLayout(self)
        layout.addWidget(self._video, stretch=1)
        layout.addWidget(self._lbl_file)
        layout.addWidget(self._lbl_err)
        layout.addLayout(ctrl)

    # ── 開啟檔案 ──
    def open_file(self, path: Optional[str] = None) -> None:
        if not HAS_MULTIMEDIA:
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "開啟影音檔", "",
                "影音檔案 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv "
                "*.mp3 *.wav *.flac *.ogg *.aac *.m4a *.opus);;"
                "所有檔案 (*)"
            )
        if not path:
            return
        self._lbl_err.hide()
        self._lbl_file.setText(QFileInfo(path).fileName())
        self._player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        self._player.play()

    # ── 播放控制 ──
    def toggle_play(self) -> None:
        if not HAS_MULTIMEDIA:
            return
        if self._player.state() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def stop(self) -> None:
        if HAS_MULTIMEDIA:
            self._player.stop()

    # ── Slots ──
    def _on_state_changed(self, state: QMediaPlayer.State) -> None:
        self._btn_play.setText(
            "⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_duration_changed(self, duration: int) -> None:
        self._seek.setRange(0, duration)
        self._update_time_label(self._player.position(), duration)

    def _on_position_changed(self, position: int) -> None:
        self._seek.setValue(position)
        self._update_time_label(position, self._player.duration())

    def _on_error(self, error: QMediaPlayer.Error) -> None:
        msgs = {
            QMediaPlayer.ResourceError:   "找不到或無法讀取檔案。",
            QMediaPlayer.FormatError:     "不支援的格式。Windows 請安裝 K-Lite Codec Pack。",
            QMediaPlayer.NetworkError:    "網路錯誤。",
            QMediaPlayer.AccessDeniedError: "無存取權限。",
        }
        msg = msgs.get(error, self._player.errorString())
        self._lbl_err.setText(f"播放錯誤：{msg}")
        self._lbl_err.show()

    @staticmethod
    def _fmt_ms(ms: int) -> str:
        s  = ms // 1000
        m  = s  // 60
        s  = s  %  60
        return f"{m:02d}:{s:02d}"

    def _update_time_label(self, pos: int, dur: int) -> None:
        self._lbl_time.setText(f"{self._fmt_ms(pos)} / {self._fmt_ms(dur)}")

    def closeEvent(self, event) -> None:
        """關閉時確保播放器停止並釋放資源。"""
        if HAS_MULTIMEDIA:
            self._player.stop()
            self._player.setMedia(QMediaContent())
        super().closeEvent(event)

# ══════════════════════════════════════════════════════════════════════
#  Git 對話框
# ══════════════════════════════════════════════════════════════════════
class GitDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Git 操作")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setStyleSheet(
            "background:#1e1e1e; color:#d4d4d4; font-family:monospace;")

        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText(
            "輸入 Git 指令（如 status, commit -m 'msg', log --oneline）")
        self.command_input.returnPressed.connect(self.run_command)

        btn_layout = QHBoxLayout()
        for label, cmd in [("Status", "status"), ("Log", "log --oneline -10"),
                            ("Diff",  "diff"),    ("Pull", "pull"), ("Push", "push")]:
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

    def _quick_run(self, cmd: str) -> None:
        self.command_input.setText(cmd)
        self.run_command()

    def run_command(self) -> None:
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
            self.output.append(
                '<span style="color:#f48771">找不到 git，請確認已安裝 Git。</span>')

# ══════════════════════════════════════════════════════════════════════
#  主視窗
# ══════════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyEditor")
        self.setGeometry(100, 100, 1280, 800)
        self.editor_widgets: dict[QWidget, CodeEditor] = {}
        self.recent_files:   list[str]                 = []
        self.current_font_size: int                    = 13
        self.is_dark_theme: bool                       = True
        self.settings = QSettings("PyEditor", "PyEditor")
        self._load_settings()
        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._setup_autosave()
        self._apply_theme()
        # LSP manager (lazy: servers start on first file open)
        self.lsp_manager = LspManager(self)
        self.diagnostics_panel.jump_requested.connect(self._jump_to_diagnostic)
        # Plugin manager
        self.plugin_manager = PluginManager(self)

    def _build_ui(self) -> None:
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

        # Diagnostics panel (right side)
        self.diagnostics_panel = DiagnosticsPanel()
        self.diagnostics_panel.setMinimumWidth(220)
        self.diagnostics_panel.setMaximumWidth(320)
        self.diagnostics_panel.hide()   # default hidden

        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(center_splitter)
        self.splitter.addWidget(self.diagnostics_panel)
        self.splitter.setSizes([220, 860, 0])
        self.setCentralWidget(self.splitter)

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("檔案")
        self._add_action(file_menu, "新增檔案",  self.new_file,     "Ctrl+N")
        self._add_action(file_menu, "開啟檔案",  self.open_file,    "Ctrl+O")
        self.recent_menu = file_menu.addMenu("最近開啟")
        self._refresh_recent_menu()
        self._add_action(file_menu, "儲存",      self.save_file,    "Ctrl+S")
        self._add_action(file_menu, "另存新檔",  self.save_file_as, "Ctrl+Shift+S")
        file_menu.addSeparator()
        self._add_action(file_menu, "離開",      self.close,        "Ctrl+Q")

        edit_menu = menubar.addMenu("編輯")
        self._add_action(edit_menu, "復原",      self.undo,         "Ctrl+Z")
        self._add_action(edit_menu, "取消復原",  self.redo,         "Ctrl+Y")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "搜尋",      self.open_search,  "Ctrl+F")
        self._add_action(edit_menu, "取代",      self.open_replace, "Ctrl+H")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "跳到指定行", self.goto_line,   "Ctrl+L")
        self._add_action(edit_menu, "全選",
            lambda: self._current_editor() and self._current_editor().selectAll(), "Ctrl+A")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "縮排選取",
            lambda: self._current_editor() and self._current_editor()._indent_selection(False))
        self._add_action(edit_menu, "反縮排選取",
            lambda: self._current_editor() and self._current_editor()._indent_selection(True))
        self._add_action(edit_menu, "切換行註解",
            lambda: self._current_editor() and self._current_editor()._toggle_comment(), "Ctrl+/")
        self._add_action(edit_menu, "選取下一個相同文字",
            lambda: self._current_editor() and self._current_editor()._select_next_occurrence(), "Ctrl+D")

        view_menu = menubar.addMenu("檢視")
        self._add_action(view_menu, "放大字型",   self.zoom_in,       "Ctrl++")
        self._add_action(view_menu, "縮小字型",   self.zoom_out,      "Ctrl+-")
        self._add_action(view_menu, "重置字型",   self.zoom_reset,    "Ctrl+0")
        self._add_action(view_menu, "選擇字型",   self.choose_font)
        view_menu.addSeparator()
        self._add_action(view_menu, "切換亮/暗主題", self.toggle_theme, "Ctrl+T")
        view_menu.addSeparator()
        self._add_action(view_menu, "切換側邊欄", self.toggle_sidebar,  "Ctrl+B")
        self._add_action(view_menu, "切換終端機", self.toggle_terminal, "Ctrl+`")

        run_menu = menubar.addMenu("執行")
        self._add_action(run_menu, "執行 Python 檔案", self.run_python, "F5")

        git_menu = menubar.addMenu("Git")
        self._add_action(git_menu, "Git 操作面板", self.open_git_dialog, "Ctrl+Shift+G")

        lsp_menu = menubar.addMenu("LSP")
        self._add_action(lsp_menu, "切換診斷面板", self.toggle_diagnostics_panel, "Ctrl+Shift+D")
        self._add_action(lsp_menu, "跳到定義", self._goto_definition_cursor, "F12")
        self._add_action(lsp_menu, "觸發補全", self._trigger_completion, "Ctrl+Space")

        media_menu = menubar.addMenu("媒體")
        self._add_action(media_menu, "影音播放器", self.open_media_player, "Ctrl+M")

        plugin_menu = menubar.addMenu("插件")
        self._add_action(plugin_menu, "插件管理器", self.open_plugin_manager, "Ctrl+Shift+P")

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具列")
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        for label, slot in [
            ("新增", self.new_file),   ("開啟", self.open_file),  ("儲存", self.save_file),
            ("|",   None),
            ("↩",  self.undo),        ("↪",   self.redo),
            ("|",   None),
            ("搜尋", self.open_search), ("取代", self.open_replace),
            ("|",   None),
            ("▶ 執行", self.run_python), ("Git", self.open_git_dialog), ("🎵", self.open_media_player),
            ("|",   None),
            ("A+",  self.zoom_in),    ("A-",  self.zoom_out),   ("主題", self.toggle_theme),
        ]:
            if label == "|":
                toolbar.addSeparator()
            else:
                act = QAction(label, self)
                if slot:
                    act.triggered.connect(slot)
                toolbar.addAction(act)

    def _build_status_bar(self) -> None:
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label   = QLabel("行 1, 欄 1")
        self.file_label     = QLabel("未命名")
        self.encoding_label = QLabel("UTF-8")
        self.status_bar.addWidget(self.file_label)
        self.status_bar.addPermanentWidget(self.encoding_label)
        self.status_bar.addPermanentWidget(self.status_label)

    def _setup_autosave(self) -> None:
        self.autosave_timer = QTimer()
        self.autosave_timer.setInterval(30_000)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start()

    def _add_action(self, menu, name: str, slot, shortcut: Optional[str] = None) -> QAction:
        action = QAction(name, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _current_tab(self) -> Optional[QWidget]:
        return self.tabs.currentWidget()

    def _current_editor(self) -> Optional[CodeEditor]:
        tab = self._current_tab()
        return self.editor_widgets.get(tab) if tab else None

    def _new_editor(self) -> CodeEditor:
        editor = CodeEditor()
        editor.setFont(QFont("Consolas", self.current_font_size))
        editor.setStyleSheet("background:#1e1e1e; color:#d4d4d4;")
        editor.cursorPositionChanged.connect(self._update_status)
        editor.document().modificationChanged.connect(self._on_modification_changed)
        return editor

    def _update_status(self) -> None:
        editor = self._current_editor()
        if editor:
            cursor = editor.textCursor()
            self.status_label.setText(
                f"行 {cursor.blockNumber()+1}, 欄 {cursor.columnNumber()+1}")
            tab  = self._current_tab()
            path = getattr(tab, 'file_path', '未命名')
            self.file_label.setText(
                QFileInfo(path).fileName() if path != '未命名' else '未命名')

    def _on_modification_changed(self, modified: bool) -> None:
        tab = self._current_tab()
        if tab:
            idx   = self.tabs.indexOf(tab)
            title = self.tabs.tabText(idx).rstrip(" ●")
            self.tabs.setTabText(idx, title + (" ●" if modified else ""))

    def new_file(self) -> None:
        tab = QWidget()
        tab.file_path = '未命名'
        editor = self._new_editor()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        self.editor_widgets[tab] = editor
        self.tabs.addTab(tab, "未命名")
        self.tabs.setCurrentWidget(tab)

    def open_file(self, path: Optional[str] = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "開啟檔案", "",
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
        # Attach LSP
        editor.attach_lsp(self.lsp_manager, path)
        # Fire plugin on_open hooks
        self.plugin_manager.fire_open(path, content)

        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(editor)
        self.editor_widgets[tab] = editor
        self.tabs.addTab(tab, QFileInfo(path).fileName())
        self.tabs.setCurrentWidget(tab)
        self._add_recent(path)

    def open_from_tree(self, index) -> None:
        path = self.file_model.filePath(index)
        if os.path.isfile(path):
            self.open_file(path)

    def save_file(self) -> None:
        tab = self._current_tab()
        if not tab:
            return
        path = getattr(tab, 'file_path', '未命名')
        if path == '未命名':
            self.save_file_as()
        else:
            self._write_file(tab, path)

    def save_file_as(self) -> None:
        tab = self._current_tab()
        if not tab:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "另存新檔", "",
            "所有檔案 (*);;Python (*.py);;JavaScript (*.js);;HTML (*.html)")
        if path:
            tab.file_path = path
            self._write_file(tab, path)
            self.tabs.setTabText(self.tabs.currentIndex(), QFileInfo(path).fileName())
            self._add_recent(path)

    def _write_file(self, tab: QWidget, path: str) -> None:
        editor = self.editor_widgets.get(tab)
        if not editor:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(editor.toPlainText())
            editor.document().setModified(False)
            self.status_bar.showMessage(f"已儲存：{path}", 3000)
            # Fire plugin on_save hooks
            self.plugin_manager.fire_save(path, editor.toPlainText())
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"儲存失敗：{e}")

    def _autosave(self) -> None:
        for tab, editor in self.editor_widgets.items():
            path = getattr(tab, 'file_path', '未命名')
            if path != '未命名' and editor.document().isModified():
                self._write_file(tab, path)
                self.status_bar.showMessage("自動儲存完成", 2000)

    def close_tab(self, index: int) -> None:
        tab    = self.tabs.widget(index)
        editor = self.editor_widgets.get(tab)
        if editor and editor.document().isModified():
            reply = QMessageBox.question(
                self, "確認", "檔案已修改，確定要關閉嗎？",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                return
        if tab in self.editor_widgets:
            self.editor_widgets[tab].detach_lsp()
            self.diagnostics_panel.clear_file(getattr(tab, 'file_path', ''))
            del self.editor_widgets[tab]
        self.tabs.removeTab(index)

    def _add_recent(self, path: str) -> None:
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:10]
        self._refresh_recent_menu()
        self._save_settings()

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.clear()
        if not self.recent_files:
            self.recent_menu.addAction("（無紀錄）").setEnabled(False)
        for path in self.recent_files:
            action = QAction(QFileInfo(path).fileName(), self)
            action.setToolTip(path)
            action.triggered.connect(lambda _, p=path: self.open_file(p))
            self.recent_menu.addAction(action)

    def undo(self) -> None:
        editor = self._current_editor()
        if editor:
            editor.undo()

    def redo(self) -> None:
        editor = self._current_editor()
        if editor:
            editor.redo()

    def open_search(self) -> None:
        editor = self._current_editor()
        if editor:
            dlg = SearchDialog(editor, parent=self)
            dlg.show()

    def open_replace(self) -> None:
        editor = self._current_editor()
        if editor:
            dlg = ReplaceDialog(editor, parent=self)
            dlg.show()

    def goto_line(self) -> None:
        editor = self._current_editor()
        if not editor:
            return
        line, ok = QInputDialog.getInt(
            self, "跳到指定行", "行號：", 1, 1, editor.blockCount())
        if ok:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(line - 1):
                cursor.movePosition(QTextCursor.Down)
            editor.setTextCursor(cursor)
            editor.setFocus()

    def zoom_in(self) -> None:
        self.current_font_size = min(self.current_font_size + 1, 40)
        self._apply_font_size()

    def zoom_out(self) -> None:
        self.current_font_size = max(self.current_font_size - 1, 6)
        self._apply_font_size()

    def zoom_reset(self) -> None:
        self.current_font_size = 13
        self._apply_font_size()

    def _apply_font_size(self) -> None:
        for editor in self.editor_widgets.values():
            font = editor.font()
            font.setPointSize(self.current_font_size)
            editor.setFont(font)
        self._save_settings()

    def choose_font(self) -> None:
        editor = self._current_editor()
        if not editor:
            return
        font, ok = QFontDialog.getFont(editor.font(), self)
        if ok:
            self.current_font_size = font.pointSize()
            for e in self.editor_widgets.values():
                e.setFont(font)

    def toggle_theme(self) -> None:
        self.is_dark_theme = not self.is_dark_theme
        self._apply_theme()
        self._save_settings()

    def _apply_theme(self) -> None:
        if self.is_dark_theme:
            self.setStyleSheet("""
                QMainWindow, QWidget { background: #1e1e1e; color: #d4d4d4; }
                QMenuBar { background: #252526; color: #cccccc; }
                QMenuBar::item:selected { background: #094771; }
                QMenu { background: #252526; color: #cccccc; border: 1px solid #454545; }
                QMenu::item:selected { background: #094771; }
                QToolBar { background: #333333; border: none; }
                QTabWidget::pane { border: 1px solid #454545; }
                QTabBar::tab { background: #2d2d2d; color: #cccccc;
                               padding: 5px 12px; border: 1px solid #454545; }
                QTabBar::tab:selected { background: #1e1e1e; color: #ffffff;
                                        border-bottom: 2px solid #007acc; }
                QTreeView { background: #252526; color: #cccccc; border: none; }
                QStatusBar { background: #007acc; color: white; }
                QLineEdit { background: #3c3c3c; color: #d4d4d4;
                            border: 1px solid #555; padding: 2px; }
                QPushButton { background: #0e639c; color: white;
                              border: none; padding: 4px 12px; }
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
                QLineEdit { background: #ffffff; color: #000000;
                            border: 1px solid #aaa; padding: 2px; }
                QPushButton { background: #0e639c; color: white;
                              border: none; padding: 4px 12px; }
                QPushButton:hover { background: #1177bb; }
            """)
            editor_style = "background:#ffffff; color:#000000;"
        for editor in self.editor_widgets.values():
            editor.setStyleSheet(editor_style)

    def toggle_sidebar(self) -> None:
        self.tree.setVisible(not self.tree.isVisible())

    def toggle_terminal(self) -> None:
        self.terminal.setVisible(not self.terminal.isVisible())

    def run_python(self) -> None:
        tab = self._current_tab()
        if not tab:
            return
        path = getattr(tab, 'file_path', '未命名')
        if path == '未命名':
            QMessageBox.warning(self, "提示", "請先儲存檔案再執行。")
            return
        self.save_file()
        self.terminal.setVisible(True)
        # Windows 用 python，其他平台用 python3
        py = "python" if sys.platform == "win32" else "python3"
        cmd = f'{py} "{path}"\n'.encode()
        self.terminal.process.write(cmd)

    def toggle_diagnostics_panel(self) -> None:
        self.diagnostics_panel.setVisible(not self.diagnostics_panel.isVisible())

    def _goto_definition_cursor(self) -> None:
        editor = self._current_editor()
        if editor and editor._lsp_manager and editor._file_path:
            cursor = editor.textCursor()
            editor._lsp_manager.request_definition(
                editor._file_path,
                cursor.blockNumber(),
                cursor.columnNumber())

    def _trigger_completion(self) -> None:
        editor = self._current_editor()
        if editor and editor._lsp_manager and editor._file_path:
            cursor = editor.textCursor()
            req_id = editor._lsp_manager.request_completion(
                editor._file_path,
                cursor.blockNumber(),
                cursor.columnNumber())

    def _jump_to_diagnostic(self, path: str, line: int, char: int) -> None:
        self.open_file(path)
        editor = self._current_editor()
        if editor:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(line):
                cursor.movePosition(QTextCursor.Down)
            cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, char)
            editor.setTextCursor(cursor)
            editor.setFocus()

    def open_plugin_manager(self) -> None:
        dlg = PluginManagerDialog(self.plugin_manager, parent=self)
        dlg.exec_()

    def open_git_dialog(self) -> None:
        GitDialog(self).exec_()

    def open_media_player(self) -> None:
        """開啟影音播放器視窗（非 modal，可同時編輯程式碼）。"""
        if not hasattr(self, '_media_player_dlg') or self._media_player_dlg is None:
            self._media_player_dlg = MediaPlayerDialog(parent=self)
        self._media_player_dlg.show()
        self._media_player_dlg.raise_()
        self._media_player_dlg.activateWindow()

    def _save_settings(self) -> None:
        self.settings.setValue("recent_files",  self.recent_files)
        self.settings.setValue("font_size",     self.current_font_size)
        self.settings.setValue("dark_theme",    self.is_dark_theme)

    def _load_settings(self) -> None:
        self.recent_files      = self.settings.value("recent_files", []) or []
        self.current_font_size = int(self.settings.value("font_size", 13))
        self.is_dark_theme     = self.settings.value("dark_theme", True)
        if isinstance(self.is_dark_theme, str):
            self.is_dark_theme = self.is_dark_theme.lower() != 'false'

    def closeEvent(self, event) -> None:
        self._save_settings()
        self._cleanup_processes()
        super().closeEvent(event)

    def _cleanup_processes(self) -> None:
        """
        關閉視窗時統一銷毀所有子進程，避免殭屍進程殘留。
        涵蓋：終端機 shell、影音播放器、HighlightWorker 執行緒。
        """
        # 終端機 shell（用 shutdown() 正確結束，避免 Destroyed while running）
        try:
            self.terminal.shutdown()
        except Exception:
            pass

        # 影音播放器
        try:
            if hasattr(self, '_media_player_dlg') and self._media_player_dlg:
                self._media_player_dlg.close()
        except Exception:
            pass

        # LSP servers
        try:
            self.lsp_manager.shutdown_all()
        except Exception:
            pass

        # Plugins
        try:
            self.plugin_manager.shutdown()
        except Exception:
            pass

        # HighlightWorker threads
        for editor in self.editor_widgets.values():
            try:
                worker = getattr(editor.highlighter, '_worker', None)
                if worker and worker.isRunning():
                    worker.abort()
                    worker.wait(500)
            except Exception:
                pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
