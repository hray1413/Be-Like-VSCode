import sys
import re
import subprocess
import os
from PyQt5.QtCore import Qt, QDir, QRegExp, QSize, QFileInfo, QProcess
from PyQt5.QtGui import (QColor, QSyntaxHighlighter, QTextCharFormat, QPainter,
                         QTextFormat, QPalette, QTextCursor, QFont, QKeySequence)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QTreeView, QPlainTextEdit,
    QTabWidget, QFileSystemModel, QVBoxLayout, QWidget, QAction,
    QFileDialog, QMessageBox, QInputDialog, QLineEdit, QDialog, QLabel, QPushButton,
    QHBoxLayout, QTextEdit, QCompleter, QListView, QMenu, QTextBrowser, QListWidget
)

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
                        item = QListWidget.QListWidgetItem(item_text)
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

class TerminalWidget(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: black; color: white; font-family: monospace;")
        self.setReadOnly(True)
        self.process = QProcess()
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_output)
        self.process.start("bash")

    def read_output(self):
        data = self.process.readAllStandardOutput().data().decode()
        self.append(data)

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

        keywords = ['def', 'class', 'import', 'from', 'return', 'if', 'else', 'elif', 'while', 'for', 'in', 'try', 'except']
        self.completer = QCompleter(keywords)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.activated.connect(self.insert_completion)

    def insert_completion(self, text):
        tc = self.textCursor()
        tc.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
        tc.insertText(text)
        self.setTextCursor(tc)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Tab:
            self.insertPlainText("    ")
        elif event.text().isalpha():
            super().keyPressEvent(event)
            self.completer.complete()
        else:
            super().keyPressEvent(event)

    def lineNumberAreaWidth(self):
        digits = len(str(max(1, self.blockCount())))
        return 3 + self.fontMetrics().width('9') * digits

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
        painter.fillRect(event.rect(), Qt.lightGray)

        block = self.firstVisibleBlock()
        blockNumber = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(blockNumber + 1)
                painter.setPen(Qt.black)
                painter.drawText(0, top, self.lineNumberArea.width() - 2, self.fontMetrics().height(), Qt.AlignRight, number)
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            blockNumber += 1

    def highlight_current_line(self):
        extraSelections = []
        if not self.isReadOnly():
            selection = QPlainTextEdit.ExtraSelection()
            lineColor = QColor(Qt.yellow).lighter(160)
            selection.format.setBackground(lineColor)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extraSelections.append(selection)
        self.setExtraSelections(extraSelections)

class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.highlightingRules = []

        keywordFormat = QTextCharFormat()
        keywordFormat.setForeground(Qt.blue)
        keywords = [
            'def', 'class', 'if', 'elif', 'else', 'try', 'except', 'finally',
            'while', 'for', 'in', 'import', 'from', 'as', 'return', 'with', 'pass',
            'break', 'continue', 'and', 'or', 'not', 'is', 'lambda', 'True', 'False', 'None'
        ]
        for word in keywords:
            pattern = QRegExp(f"\\b{word}\\b")
            self.highlightingRules.append((pattern, keywordFormat))

        commentFormat = QTextCharFormat()
        commentFormat.setForeground(Qt.darkGreen)
        self.highlightingRules.append((QRegExp("#.*"), commentFormat))

        stringFormat = QTextCharFormat()
        stringFormat.setForeground(Qt.darkMagenta)
        self.highlightingRules.append((QRegExp('\".*\"'), stringFormat))
        self.highlightingRules.append((QRegExp("'.*'"), stringFormat))

    def highlightBlock(self, text):
        for pattern, fmt in self.highlightingRules:
            index = pattern.indexIn(text)
            while index >= 0:
                length = pattern.matchedLength()
                self.setFormat(index, length, fmt)
                index = pattern.indexIn(text, index + length)
    （由於原始程式碼太長，這裡僅補充添加 Git 操作介面部分的內容。完整程式碼維持原樣，請將以下程式碼加入 `MainWindow` 中相對應的功能區塊以擴充 Git 操作介面）

class GitDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Git 操作")
        self.setMinimumWidth(400)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("輸入 Git 指令（如 status, commit -m 'msg'）")

        run_button = QPushButton("執行")
        run_button.clicked.connect(self.run_command)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Git 指令："))
        layout.addWidget(self.command_input)
        layout.addWidget(run_button)
        layout.addWidget(QLabel("輸出："))
        layout.addWidget(self.output)
        self.setLayout(layout)

    def run_command(self):
        command = self.command_input.text().strip()
        if not command:
            return

    full_cmd = ["git"] + command.split()
        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
            self.output.append("$ git " + command)
            self.output.append(result.stdout)
        except subprocess.CalledProcessError as e:
            self.output.append("$ git " + command)
            self.output.append(e.stderr)

        git_action = QAction("Git 操作", self)
        git_action.setShortcut("Ctrl+G")
        git_action.triggered.connect(self.open_git_dialog)
        toolbar.addAction(git_action)

    def open_git_dialog(self):
        dlg = GitDialog(self)
        dlg.exec_()

