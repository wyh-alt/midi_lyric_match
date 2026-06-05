import os
import re
import sys
import traceback

from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFileDialog, 
    QStackedWidget, QHeaderView, QTableWidgetItem
)
from PyQt6.QtGui import QFont, QIcon

from qfluentwidgets import (
    FluentWindow, setTheme, Theme,
    LineEdit, PushButton, PrimaryPushButton, SpinBox, ComboBox, CheckBox,
    TextEdit, MessageBox, InfoBar, TableWidget,
    Pivot, ScrollArea, CardWidget, BodyLabel, TitleLabel, 
    StrongBodyLabel, FluentIcon
)

import mido
import matplotlib
matplotlib.use('QtAgg')


def _app_base_dir() -> str:
    """源码运行时为脚本目录；PyInstaller 单文件为 _MEIPASS 解压目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def load_app_icon() -> QIcon:
    icon_path = os.path.join(_app_base_dir(), "icon.ico")
    if os.path.exists(icon_path):
        return QIcon(icon_path)
    return QIcon()


def _set_windows_app_user_model_id():
    """避免 Windows 任务栏将 PyInstaller 程序归为 python.exe，导致图标不显示。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MIDIAnalysisTool.LyricMatch.Calibrator.1"
        )
    except Exception:
        pass

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from midi_lyric_aligner import align_lyrics_to_midi


class AlignmentWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, midi_path, lyric_path, output_path, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw):
        super().__init__()
        self.args = (midi_path, lyric_path, output_path, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw)

    def run(self):
        try:
            report = align_lyrics_to_midi(*self.args)
            self.finished.emit(report)
        except Exception as e:
            self.error.emit(f"歌词对齐失败: {str(e)}\n\n{traceback.format_exc()}")


class BatchWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, midi_files, lyric_files, output_dir, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw):
        super().__init__()
        self.midi_files = midi_files
        self.lyric_files = lyric_files
        self.output_dir = output_dir
        self.tol = tol
        self.track = track
        self.clear = clear
        self.sustain = sustain
        self.split = split
        self.algorithm = algorithm
        self.rm_paren = rm_paren
        self.ignore_kw = ignore_kw

    def _extract_song_id(self, file_path):
        stem = os.path.splitext(os.path.basename(file_path))[0].strip()
        m = re.match(r"^(.+?)[-_](?:midi|歌词|lyric|lyrics)$", stem, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
        if "-" in stem:
            return stem.split("-", 1)[0].strip()
        if "_" in stem:
            return stem.split("_", 1)[0].strip()
        return stem

    def _build_id_map(self, paths):
        id_map = {}
        for p in paths:
            song_id = self._extract_song_id(p)
            if song_id and song_id not in id_map:
                id_map[song_id] = p
        return id_map

    def run(self):
        try:
            midi_map = self._build_id_map(self.midi_files)
            lyric_map = self._build_id_map(self.lyric_files)
            reports = []
            missing_lyrics = []
            errors = []

            for song_id, midi_path in midi_map.items():
                lyric_path = lyric_map.get(song_id)
                if not lyric_path:
                    missing_lyrics.append(song_id)
                    continue

                midi_name, midi_ext = os.path.splitext(os.path.basename(midi_path))
                output_path = os.path.join(self.output_dir, f"{midi_name}_with_lyrics{midi_ext or '.mid'}")

                try:
                    report = align_lyrics_to_midi(
                        midi_file_path=midi_path,
                        lyric_file_path=lyric_path,
                        output_midi_path=output_path,
                        tolerance_ms=self.tol,
                        target_track=self.track,
                        clear_existing_lyrics=self.clear,
                        fill_sustain_dash=self.sustain,
                        split_units=self.split,
                        alignment_algorithm=self.algorithm,
                        remove_parentheses=self.rm_paren,
                        ignore_keywords=self.ignore_kw,
                    )
                    report["song_id"] = song_id
                    report["midi_file"] = midi_path
                    report["lyric_file"] = lyric_path
                    reports.append(report)
                except Exception as e:
                    errors.append(f"{song_id}: {str(e)}")

            result = {
                "reports": reports,
                "missing_lyrics": missing_lyrics,
                "errors": errors,
                "total_midi": len(midi_map),
                "total_lyric": len(lyric_map),
                "matched_pairs": len(reports),
            }
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"匹配失败: {str(e)}\n\n{traceback.format_exc()}")


class AnalyzeWorker(QThread):
    finished = pyqtSignal(list, list, list)
    error = pyqtSignal(str)

    def __init__(self, midi_path):
        super().__init__()
        self.midi_path = midi_path
        
    def decode_text(self, text):
        try:
            raw_bytes = text.encode('latin1')
            try:
                return raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return raw_bytes.decode('gbk')
                except UnicodeDecodeError:
                    return text
        except UnicodeEncodeError:
            return text

    def get_tempo(self, midi_file):
        default_tempo = 500000
        for track in midi_file.tracks:
            for msg in track:
                if msg.type == 'set_tempo':
                    return msg.tempo
        return default_tempo
        
    def run(self):
        try:
            midi_file = mido.MidiFile(self.midi_path)
            notes = []
            lyrics = []
            text_events = []
            
            for i, track in enumerate(midi_file.tracks):
                track_time = 0
                track_notes = []
                track_lyrics = []
                track_text_events = []
                
                for msg in track:
                    track_time += msg.time
                    if msg.type == 'note_on' and msg.velocity > 0:
                        note_info = {
                            'track': i,
                            'time': track_time,
                            'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                            'note': msg.note,
                            'velocity': msg.velocity,
                            'channel': msg.channel
                        }
                        track_notes.append(note_info)
                    elif msg.type == 'lyrics':
                        lyric_info = {
                            'track': i,
                            'time': track_time,
                            'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                            'text': self.decode_text(msg.text),
                            'type': 'lyrics'
                        }
                        track_lyrics.append(lyric_info)
                    elif msg.type == 'text':
                        text_info = {
                            'track': i,
                            'time': track_time,
                            'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                            'text': self.decode_text(msg.text),
                            'type': 'text'
                        }
                        track_text_events.append(text_info)
                
                notes.extend(track_notes)
                lyrics.extend(track_lyrics)
                text_events.extend(track_text_events)
            
            notes.sort(key=lambda x: x['time'])
            lyrics.sort(key=lambda x: x['time'])
            text_events.sort(key=lambda x: x['time'])
            
            self.finished.emit(notes, lyrics, text_events)
            
        except Exception as e:
            self.error.emit(f"分析过程中出错: {str(e)}\n\n{traceback.format_exc()}")


class DragLineEdit(LineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            paths = [url.toLocalFile() for url in urls]
            current = self.text().strip()
            new_text = ";".join(paths)
            if current:
                self.setText(current + ";" + new_text)
            else:
                self.setText(new_text)


class ProcessInterface(ScrollArea):
    analysisRequested = pyqtSignal(str)
    singleProcessRequested = pyqtSignal(str, str, str, int, object, bool, bool, bool, str, bool, str)
    batchProcessRequested = pyqtSignal(list, list, str, int, object, bool, bool, bool, str, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("ProcessInterface")
        self.view = QWidget(self)
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setContentsMargins(24, 24, 24, 24)
        self.vBoxLayout.setSpacing(16)
        
        title = TitleLabel("MIDI 歌词匹配", self.view)
        self.vBoxLayout.addWidget(title)
        
        # 将输入和输出合并到同一个栏目
        self.ioCard = CardWidget(self.view)
        ioLayout = QVBoxLayout(self.ioCard)
        ioLayout.addWidget(StrongBodyLabel("文件路径设置 (支持拖拽文件或文件夹)", self.ioCard))
        
        # MIDI 输入路径
        ioLayout.addWidget(BodyLabel("MIDI 输入路径:", self.ioCard))
        midiInLayout = QHBoxLayout()
        self.midiInDirEdit = DragLineEdit(self.ioCard)
        self.midiInDirEdit.setPlaceholderText("拖拽或选择 MIDI 文件/文件夹 (多路径用分号隔开)...")
        self.midiInDirEdit.textChanged.connect(self.parse_midi_path)
        self.midiInFileBtn = PushButton("选文件", self.ioCard)
        self.midiInFileBtn.clicked.connect(self.browse_midi_files)
        self.midiInDirBtn = PushButton("选目录", self.ioCard)
        self.midiInDirBtn.clicked.connect(self.browse_midi_folder)
        midiInLayout.addWidget(self.midiInDirEdit, 1)
        midiInLayout.addWidget(self.midiInFileBtn)
        midiInLayout.addWidget(self.midiInDirBtn)
        ioLayout.addLayout(midiInLayout)
        
        self.midiCountLabel = BodyLabel("", self.ioCard)
        self.midiCountLabel.setStyleSheet("color: #009688; margin-bottom: 4px;")
        self.midiCountLabel.hide()
        ioLayout.addWidget(self.midiCountLabel)
        
        # 歌词 输入路径
        ioLayout.addWidget(BodyLabel("歌词 输入路径:", self.ioCard))
        lyricInLayout = QHBoxLayout()
        self.lyricInDirEdit = DragLineEdit(self.ioCard)
        self.lyricInDirEdit.setPlaceholderText("拖拽或选择歌词 文件/文件夹 (多路径用分号隔开)...")
        self.lyricInDirEdit.textChanged.connect(self.parse_lyric_path)
        self.lyricInFileBtn = PushButton("选文件", self.ioCard)
        self.lyricInFileBtn.clicked.connect(self.browse_lyric_files)
        self.lyricInDirBtn = PushButton("选目录", self.ioCard)
        self.lyricInDirBtn.clicked.connect(self.browse_lyric_folder)
        lyricInLayout.addWidget(self.lyricInDirEdit, 1)
        lyricInLayout.addWidget(self.lyricInFileBtn)
        lyricInLayout.addWidget(self.lyricInDirBtn)
        ioLayout.addLayout(lyricInLayout)

        self.lyricCountLabel = BodyLabel("", self.ioCard)
        self.lyricCountLabel.setStyleSheet("color: #009688; margin-bottom: 4px;")
        self.lyricCountLabel.hide()
        ioLayout.addWidget(self.lyricCountLabel)
        
        # 输出路径
        ioLayout.addWidget(BodyLabel("文件输出路径:", self.ioCard))
        outLayout = QHBoxLayout()
        self.outDirEdit = DragLineEdit(self.ioCard)
        self.outDirEdit.setPlaceholderText("选择生成文件的保存目录...")
        self.outDirBrowseBtn = PushButton("浏览", self.ioCard)
        self.outDirBrowseBtn.clicked.connect(self.browse_out_dir)
        outLayout.addWidget(self.outDirEdit, 1)
        outLayout.addWidget(self.outDirBrowseBtn)
        ioLayout.addLayout(outLayout)
        
        self.vBoxLayout.addWidget(self.ioCard)
        
        self.paramCard = CardWidget(self.view)
        paramLayout = QVBoxLayout(self.paramCard)
        paramLayout.addWidget(StrongBodyLabel("对齐参数", self.paramCard))
        
        hLayout3 = QHBoxLayout()
        hLayout3.addWidget(BodyLabel("吸附容差 (毫秒):", self.paramCard))
        self.toleranceSpin = SpinBox(self.paramCard)
        self.toleranceSpin.setRange(20, 2000)
        self.toleranceSpin.setSingleStep(10)
        self.toleranceSpin.setValue(220)
        hLayout3.addWidget(self.toleranceSpin)
        hLayout3.addStretch(1)
        
        hLayout3.addWidget(BodyLabel("目标轨道:", self.paramCard))
        self.trackCombo = ComboBox(self.paramCard)
        self.trackCombo.addItems(["auto", "0", "1", "2", "3", "4", "5", "6", "7"])
        hLayout3.addWidget(self.trackCombo)
        hLayout3.addStretch(1)

        hLayout3.addWidget(BodyLabel("对齐参照算法:", self.paramCard))
        self.algorithmCombo = ComboBox(self.paramCard)
        self.algorithmCombo.addItem("逐句首字差值", userData="phrase_start")
        self.algorithmCombo.addItem("全局平均差值", userData="global_average")
        self.algorithmCombo.setCurrentIndex(1)  # 默认选择"全局平均差值"
        hLayout3.addWidget(self.algorithmCombo)
        hLayout3.addStretch(1)
        
        paramLayout.addLayout(hLayout3)
        
        self.clearCheck = CheckBox("写入前清除原有 lyrics/text", self.paramCard)
        self.clearCheck.setChecked(True)
        paramLayout.addWidget(self.clearCheck)
        
        self.sustainCheck = CheckBox("自动将延音填充为 '-'", self.paramCard)
        self.sustainCheck.setChecked(True)
        paramLayout.addWidget(self.sustainCheck)
        
        self.splitCheck = CheckBox("逐字匹配（将每行歌词拆分为字/词分别吸附）", self.paramCard)
        self.splitCheck.setChecked(True)
        paramLayout.addWidget(self.splitCheck)
        
        self.vBoxLayout.addWidget(self.paramCard)

        self.filterCard = CardWidget(self.view)
        filterLayout = QVBoxLayout(self.filterCard)
        filterLayout.addWidget(StrongBodyLabel("歌词文本过滤 (防止和声/Rap等误吸附)", self.filterCard))
        
        self.removeParenCheck = CheckBox("自动过滤括号内的文本 (如: (伴唱), 【和声】 等)", self.filterCard)
        self.removeParenCheck.setChecked(False)
        filterLayout.addWidget(self.removeParenCheck)
        
        hLayoutKw = QHBoxLayout()
        hLayoutKw.addWidget(BodyLabel("忽略包含以下关键词的整句歌词 (逗号分隔):", self.filterCard))
        self.ignoreKwEdit = LineEdit(self.filterCard)
        self.ignoreKwEdit.setText("过滤,伴唱,和声,合唱,说唱")
        self.ignoreKwEdit.setPlaceholderText("例如: 伴唱,和声,说唱,合")
        hLayoutKw.addWidget(self.ignoreKwEdit, 1)
        filterLayout.addLayout(hLayoutKw)
        
        self.vBoxLayout.addWidget(self.filterCard)
        
        runLayout = QHBoxLayout()
        self.runBtn = PrimaryPushButton("开始匹配", self.view)
        self.runBtn.clicked.connect(self.run_process)
        
        runLayout.addStretch(1)
        runLayout.addWidget(self.runBtn)
        
        self.vBoxLayout.addLayout(runLayout)
        self.vBoxLayout.addStretch(1)
        
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        
        self.midi_files = []
        self.lyric_files = []

    def _dedupe_paths(self, paths):
        seen = set()
        out = []
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen: continue
            seen.add(key)
            out.append(os.path.abspath(p))
        return out

    def _update_counts(self):
        if not self.outDirEdit.text() and self.midi_files:
            # 自动设置输出目录为第一个 MIDI 文件所在的目录
            self.outDirEdit.setText(os.path.dirname(self.midi_files[0]))

        if len(self.midi_files) > 0:
            self.midiCountLabel.setText(f"已发现: {len(self.midi_files)} 个 MIDI 文件")
            self.midiCountLabel.show()
        else:
            self.midiCountLabel.hide()
            
        if len(self.lyric_files) > 0:
            self.lyricCountLabel.setText(f"已发现: {len(self.lyric_files)} 个 歌词文件")
            self.lyricCountLabel.show()
        else:
            self.lyricCountLabel.hide()

    def parse_midi_path(self, path_text):
        self.midi_files = []
        path_text = path_text.strip()
        if not path_text:
            self._update_counts()
            return
            
        midi_discovered = []
        for path in path_text.split(';'):
            path = path.strip(' "\'')
            if not path or not os.path.exists(path):
                continue
                
            if os.path.isfile(path):
                if path.lower().endswith(('.mid', '.midi')):
                    midi_discovered.append(path)
            elif os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for name in files:
                        full_path = os.path.join(root, name)
                        if name.lower().endswith(('.mid', '.midi')):
                            midi_discovered.append(full_path)
                            
        self.midi_files = self._dedupe_paths(midi_discovered)
        self._update_counts()

    def parse_lyric_path(self, path_text):
        self.lyric_files = []
        path_text = path_text.strip()
        if not path_text:
            self._update_counts()
            return
            
        lyric_discovered = []
        for path in path_text.split(';'):
            path = path.strip(' "\'')
            if not path or not os.path.exists(path):
                continue
                
            if os.path.isfile(path):
                if path.lower().endswith(('.lrc', '.txt', '.csv')):
                    lyric_discovered.append(path)
            elif os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for name in files:
                        full_path = os.path.join(root, name)
                        if name.lower().endswith(('.lrc', '.txt', '.csv')):
                            lyric_discovered.append(full_path)
                            
        self.lyric_files = self._dedupe_paths(lyric_discovered)
        self._update_counts()

    def browse_midi_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择MIDI文件", "", "MIDI文件 (*.mid *.midi);;所有文件 (*.*)")
        if paths:
            current = self.midiInDirEdit.text().strip()
            new_text = ";".join(paths)
            if current:
                self.midiInDirEdit.setText(current + ";" + new_text)
            else:
                self.midiInDirEdit.setText(new_text)

    def browse_midi_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择MIDI文件夹")
        if folder:
            current = self.midiInDirEdit.text().strip()
            if current:
                self.midiInDirEdit.setText(current + ";" + folder)
            else:
                self.midiInDirEdit.setText(folder)

    def browse_lyric_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择歌词文件", "", "歌词文件 (*.lrc *.txt *.csv);;文本文件 (*.txt *.csv);;所有文件 (*.*)")
        if paths:
            current = self.lyricInDirEdit.text().strip()
            new_text = ";".join(paths)
            if current:
                self.lyricInDirEdit.setText(current + ";" + new_text)
            else:
                self.lyricInDirEdit.setText(new_text)

    def browse_lyric_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择歌词文件夹")
        if folder:
            current = self.lyricInDirEdit.text().strip()
            if current:
                self.lyricInDirEdit.setText(current + ";" + folder)
            else:
                self.lyricInDirEdit.setText(folder)

    def browse_out_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if folder:
            self.outDirEdit.setText(folder)

    def run_process(self):
        if not self.midi_files or not self.lyric_files:
            InfoBar.warning("警告", "请先添加 MIDI 文件和歌词文件", parent=self.window())
            return
        out_dir = self.outDirEdit.text().strip()
        if not out_dir:
            InfoBar.warning("警告", "请选择或输入输出目录", parent=self.window())
            return
            
        tol = self.toleranceSpin.value()
        track_raw = self.trackCombo.currentText()
        track = None if track_raw == "auto" else int(track_raw)
        
        clear = self.clearCheck.isChecked()
        sustain = self.sustainCheck.isChecked()
        split = self.splitCheck.isChecked()
        algorithm = self.algorithmCombo.currentData()
        rm_paren = self.removeParenCheck.isChecked()
        ignore_kw = self.ignoreKwEdit.text().strip()
        
        os.makedirs(out_dir, exist_ok=True)
        
        # 智能判断是单文件还是多文件
        if len(self.midi_files) == 1 and len(self.lyric_files) == 1:
            midi_path = self.midi_files[0]
            lyric_path = self.lyric_files[0]
            midi_name, midi_ext = os.path.splitext(os.path.basename(midi_path))
            output_path = os.path.join(out_dir, f"{midi_name}_with_lyrics{midi_ext or '.mid'}")
            self.singleProcessRequested.emit(midi_path, lyric_path, output_path, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw)
        else:
            self.batchProcessRequested.emit(self.midi_files, self.lyric_files, out_dir, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw)


class MidiAnalysisInterface(ScrollArea):
    analysisRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("MidiAnalysisInterface")
        self.view = QWidget(self)
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setContentsMargins(24, 24, 24, 24)
        self.vBoxLayout.setSpacing(16)
        
        title = TitleLabel("MIDI 文件分析", self.view)
        self.vBoxLayout.addWidget(title)
        
        self.inputCard = CardWidget(self.view)
        inputLayout = QVBoxLayout(self.inputCard)
        inputLayout.addWidget(StrongBodyLabel("选择 MIDI 文件 (仅支持单个文件)", self.inputCard))
        
        inLayout = QHBoxLayout()
        self.midiInDirEdit = DragLineEdit(self.inputCard)
        self.midiInDirEdit.setPlaceholderText("拖拽或选择 MIDI 文件...")
        self.midiInBrowseBtn = PushButton("浏览", self.inputCard)
        self.midiInBrowseBtn.clicked.connect(self.browse_midi)
        self.analyzeBtn = PrimaryPushButton("开始分析", self.inputCard)
        self.analyzeBtn.clicked.connect(self.run_analysis)
        
        inLayout.addWidget(self.midiInDirEdit, 1)
        inLayout.addWidget(self.midiInBrowseBtn)
        inLayout.addWidget(self.analyzeBtn)
        inputLayout.addLayout(inLayout)
        
        self.vBoxLayout.addWidget(self.inputCard)
        
        self.resultCard = CardWidget(self.view)
        resultLayout = QVBoxLayout(self.resultCard)
        resultLayout.addWidget(StrongBodyLabel("分析结果", self.resultCard))
        
        self.table = TableWidget(self.resultCard)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["类型", "轨道", "时间(毫秒)", "时间(秒)", "事件", "详细信息"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 150)
        resultLayout.addWidget(self.table)
        
        self.vBoxLayout.addWidget(self.resultCard)
        self.setWidget(self.view)
        self.setWidgetResizable(True)

    def browse_midi(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择MIDI文件", "", "MIDI文件 (*.mid *.midi);;所有文件 (*.*)")
        if path:
            self.midiInDirEdit.setText(path)

    def run_analysis(self):
        path = self.midiInDirEdit.text().strip()
        if not path or not os.path.isfile(path):
            InfoBar.warning("警告", "请先选择一个有效的 MIDI 文件", parent=self.window())
            return
        self.analysisRequested.emit(path)

    def update_results(self, notes, lyrics, text_events):
        combined = []
        for n in notes:
            combined.append({
                'category': '音符',
                'track': n['track'],
                'time_seconds': n['time_seconds'],
                'event': f"Note {n['note']}",
                'details': f"力度: {n['velocity']}, 通道: {n['channel']}"
            })
        for l in lyrics:
            combined.append({
                'category': '歌词',
                'track': l['track'],
                'time_seconds': l['time_seconds'],
                'event': l['type'],
                'details': l['text']
            })
        for t in text_events:
            combined.append({
                'category': '文本',
                'track': t['track'],
                'time_seconds': t['time_seconds'],
                'event': t['type'],
                'details': t['text']
            })
            
        combined.sort(key=lambda x: x['time_seconds'])
        
        self.table.setRowCount(len(combined))
        for row, item in enumerate(combined):
            ms = int(item['time_seconds'] * 1000)
            self.table.setItem(row, 0, QTableWidgetItem(item['category']))
            self.table.setItem(row, 1, QTableWidgetItem(str(item['track'])))
            self.table.setItem(row, 2, QTableWidgetItem(str(ms)))
            self.table.setItem(row, 3, QTableWidgetItem(f"{item['time_seconds']:.3f}"))
            self.table.setItem(row, 4, QTableWidgetItem(item['event']))
            self.table.setItem(row, 5, QTableWidgetItem(item['details']))


from lyric_calibrator_gui import LyricCalibratorWidget


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIDI & 歌词处理工具箱")
        
        self.setWindowIcon(load_app_icon())
            
        self.resize(1100, 750)
        
        # 居中
        desktop = QApplication.primaryScreen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w//2 - self.width()//2, h//2 - self.height()//2)
        
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei']
        plt.rcParams['axes.unicode_minus'] = False
        
        # 1. 对齐吸附界面
        self.processInterface = ProcessInterface(self)
        self.processInterface.setObjectName("ProcessInterface")
        self.addSubInterface(self.processInterface, FluentIcon.DOCUMENT, "MIDI 歌词吸附对齐")
        
        # 2. 歌词时间轴校准界面
        self.calibratorInterface = LyricCalibratorWidget(self)
        self.calibratorInterface.setObjectName("CalibratorInterface")
        self.addSubInterface(self.calibratorInterface, FluentIcon.EDIT, "歌词时间轴打点校准")
        
        # 3. 分析结果界面
        self.analysisInterface = MidiAnalysisInterface(self)
        self.analysisInterface.setObjectName("AnalysisInterface")
        self.addSubInterface(self.analysisInterface, FluentIcon.PIE_SINGLE, "MIDI 分析")
        
        self.processInterface.singleProcessRequested.connect(self.start_alignment)
        self.processInterface.batchProcessRequested.connect(self.start_batch)
        self.analysisInterface.analysisRequested.connect(self.start_analysis)

    def start_analysis(self, path):
        self.analyze_worker = AnalyzeWorker(path)
        self.analyze_worker.finished.connect(self.on_analysis_finished)
        self.analyze_worker.error.connect(self.on_error)
        self.analyze_worker.start()
        InfoBar.info("分析中", "正在分析 MIDI 文件...", parent=self)

    def on_analysis_finished(self, notes, lyrics, text_events):
        self.analysisInterface.update_results(notes, lyrics, text_events)
        InfoBar.success("分析完成", "MIDI 文件分析完毕", parent=self)

    def start_alignment(self, midi, lyric, out, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw):
        self.align_worker = AlignmentWorker(midi, lyric, out, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw)
        self.align_worker.finished.connect(self.on_align_finished)
        self.align_worker.error.connect(self.on_error)
        self.align_worker.start()
        InfoBar.info("处理中", "正在对齐歌词并写入 MIDI...", parent=self)

    def on_align_finished(self, report):
        summary = (
            f"输出文件: {report['output_midi_path']}\n"
            f"目标轨道: {report['target_track']}\n"
            f"歌词总数: {report['total_lyrics']}\n"
            f"音符总数: {report['total_notes']}\n"
            f"成功匹配: {report['matched_lyrics']}\n"
            f"未匹配: {report['unmatched_lyrics']}\n"
            f"自动填充延音'-': {report.get('sustain_dash_inserted', 0)}\n"
            f"吸附容差: {report['tolerance_ms']}ms"
        )
        if report.get("diagnosis"):
            summary += f"\n诊断: {report['diagnosis']}"
            
        MessageBox("对齐完成", summary, self).exec()

    def start_batch(self, midis, lyrics, out_dir, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw):
        self.batch_worker = BatchWorker(midis, lyrics, out_dir, tol, track, clear, sustain, split, algorithm, rm_paren, ignore_kw)
        self.batch_worker.finished.connect(self.on_batch_finished)
        self.batch_worker.error.connect(self.on_error)
        self.batch_worker.start()
        InfoBar.info("处理中", "正在按 ID 匹配歌词...", parent=self)

    def on_batch_finished(self, result):
        total_matched_lyrics = sum(r["matched_lyrics"] for r in result["reports"])
        total_unmatched_lyrics = sum(r["unmatched_lyrics"] for r in result["reports"])
        total_sustain_dash = sum(r.get("sustain_dash_inserted", 0) for r in result["reports"])

        summary = (
            f"MIDI文件数: {result['total_midi']}\n"
            f"歌词文件数: {result['total_lyric']}\n"
            f"成功配对并输出: {result['matched_pairs']}\n"
            f"缺少对应歌词ID: {len(result['missing_lyrics'])}\n"
            f"处理错误: {len(result['errors'])}\n"
            f"累计成功匹配歌词: {total_matched_lyrics}\n"
            f"累计未匹配歌词: {total_unmatched_lyrics}\n"
            f"累计自动延音'-': {total_sustain_dash}"
        )
        
        MessageBox("匹配完成", summary, self).exec()

    def on_error(self, err_msg):
        MessageBox("错误", err_msg, self).exec()


def main():
    _set_windows_app_user_model_id()

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app_icon = load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
        
    setTheme(Theme.AUTO)
    
    w = MainWindow()
    if not app_icon.isNull():
        w.setWindowIcon(app_icon)
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
