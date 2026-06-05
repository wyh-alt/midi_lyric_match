import sys
import os
import traceback

# 启用 ASIO 支持（必须在导入 sounddevice 之前设置）
os.environ["SD_ENABLE_ASIO"] = "1"

import numpy as np
import soundfile as sf
import sounddevice as sd

from PyQt6.QtCore import Qt, pyqtSignal, QThread, QUrl, QTimer, QRectF, QPointF, QEvent
from PyQt6.QtGui import (
    QPen, QColor, QFont, QPainter, QPainterPath, QBrush,
    QDragEnterEvent, QDropEvent, QLinearGradient, QKeySequence, QShortcut,
    QIcon, QPixmap, QTransform,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFileDialog, QScrollArea, QDialog, QFormLayout, QInputDialog
)

from qfluentwidgets import (
    PushButton, PrimaryPushButton, LineEdit, 
    CardWidget, InfoBar, BodyLabel, TitleLabel, SubtitleLabel, MessageBoxBase,
    FluentIcon as FIF, Slider, CheckBox, TransparentToolButton, ComboBox, SpinBox
)

from midi_lyric_aligner import parse_lyric_file, LyricEvent, parse_time_to_seconds


def clone_lyrics(lyrics):
    """深拷贝歌词状态，用于撤销/重做"""
    return [LyricEvent(time_seconds=e.time_seconds, text=e.text, source_line=e.source_line) for e in lyrics]


def make_filter_anchor_icon(is_start: bool, size: int = 16) -> QIcon:
    """绘制 |←（起始）或 →|（结束）过滤锚点图标，尺寸与工具栏 Fluent 图标一致"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(220, 220, 220), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    margin = 2
    mid = size // 2
    bar_x = margin + 1 if is_start else size - margin - 2
    painter.drawLine(bar_x, margin, bar_x, size - margin)

    arrow_tip = size - margin - 1 if is_start else margin + 1
    arrow_head = bar_x + (2 if is_start else -2)
    wing = 3

    if is_start:
        painter.drawLine(arrow_tip, mid, arrow_head, mid)
        painter.drawLine(arrow_head, mid, arrow_head + wing, mid - wing)
        painter.drawLine(arrow_head, mid, arrow_head + wing, mid + wing)
    else:
        painter.drawLine(arrow_tip, mid, arrow_head, mid)
        painter.drawLine(arrow_head, mid, arrow_head - wing, mid - wing)
        painter.drawLine(arrow_head, mid, arrow_head - wing, mid + wing)

    painter.end()
    return QIcon(pm)


def make_redo_icon(size: int = 16) -> QIcon:
    """镜像撤销图标作为重做图标"""
    undo_pm = FIF.CANCEL.icon().pixmap(size, size)
    return QIcon(undo_pm.transformed(QTransform().scale(-1, 1)))


class SDAudioPlayer:
    def __init__(self):
        self.audio_data = None
        self.sr = 44100
        self.pos = 0.0 # float index
        self.speed = 1.0
        self.volume = 1.0
        # 优先使用 WASAPI 设备（最低延迟），否则使用系统默认
        self.device_id = self._find_best_output_device()
        self.stream = None
        self.playing = False
    
    def _find_best_output_device(self):
        """查找最佳输出设备，优先匹配 Windows 默认设备，然后按 ASIO > WASAPI > WDM-KS 选择"""
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            
            # 获取 Windows 默认设备名称
            default_id = sd.default.device[1]
            default_dev = devices[default_id]
            default_name = default_dev['name']
            
            # 提取设备品牌/型号关键词（如 "ZenGo SC"）
            # 去掉常见后缀如 "Playback", "ASIO Driver", 通道号等
            def extract_device_key(name):
                key = name.split('(')[0].strip()
                for suffix in ['Playback', 'ASIO Driver', 'Driver', '1/2', '3/4', '5/6', '7/8']:
                    key = key.replace(suffix, '').strip()
                return key
            
            default_key = extract_device_key(default_name)
            
            # 按优先级查找：ASIO > WASAPI > WDM-KS
            for priority_api in ['ASIO', 'Windows WASAPI', 'Windows WDM-KS']:
                matched = []
                
                for i, dev in enumerate(devices):
                    if dev['max_output_channels'] > 0:
                        api_name = hostapis[dev['hostapi']]['name']
                        if api_name == priority_api:
                            latency = dev['default_low_output_latency']
                            dev_name = dev['name']
                            dev_key = extract_device_key(dev_name)
                            
                            # 检查关键词是否匹配
                            if default_key and dev_key and (default_key in dev_key or dev_key in default_key):
                                matched.append((i, latency, dev_name))
                
                if matched:
                    # 对于非 ASIO 设备，优先选择通道 1/2（主输出）
                    if priority_api != 'ASIO':
                        for dev_id, latency, name in matched:
                            if '1/2' in name or 'Playback 1' in name:
                                return dev_id
                    # 否则选择延迟最低的
                    matched.sort(key=lambda x: x[1])
                    return matched[0][0]
            
            # 都没匹配到，返回 Windows 默认
            return default_id
        except:
            return None

    def load(self, audio_data, sr):
        self.stop()
        self.audio_data = audio_data
        self.sr = int(sr)
        self.pos = 0.0

    def set_device(self, device_id):
        was_playing = self.playing
        self.stop()
        self.device_id = device_id
        if was_playing:
            self.play()

    def set_stream_params(self, latency, blocksize):
        # 兼容旧的方法调用，但现在这些参数不再生效，固定使用稳定参数
        pass

    def set_wasapi_exclusive(self, enabled):
        pass

    def set_volume(self, v):
        self.volume = v

    def play(self):
        if self.audio_data is None:
            return
        self.stop()
        self.playing = True
        try:
            # 让驱动自己决定 buffer 大小（ASIO 会使用控制面板中设置的值）
            self.stream = sd.OutputStream(
                samplerate=self.sr,
                channels=self.audio_data.shape[1],
                device=self.device_id,
                callback=self._callback,
                latency='low'
            )
            self.stream.start()
        except Exception as e:
            print("Stream error:", e)
            self.playing = False

    def stop(self):
        self.playing = False
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def pause(self):
        self.playing = False
        if self.stream is not None:
            self.stream.stop()

    def seek(self, sec):
        if self.audio_data is None: return
        self.pos = max(0.0, min(sec * self.sr, len(self.audio_data) - 1))

    def _callback(self, outdata, frames, time, status):
        if not self.playing or self.audio_data is None:
            outdata.fill(0)
            raise sd.CallbackStop

        speed = float(self.speed)
        volume = float(self.volume)

        if speed == 1.0:
            start = int(self.pos)
            end = start + frames
            if end > len(self.audio_data):
                valid = len(self.audio_data) - start
                outdata[:valid] = self.audio_data[start:end] * volume
                outdata[valid:].fill(0)
                self.pos = len(self.audio_data)
                self.playing = False
                raise sd.CallbackStop
            else:
                outdata[:] = self.audio_data[start:end] * volume
                self.pos += frames
        else:
            idx = self.pos + np.arange(frames, dtype=np.float32) * speed
            max_idx = len(self.audio_data) - 1
            
            valid_mask = idx < max_idx
            valid_len = np.count_nonzero(valid_mask)
            
            if valid_len == 0:
                outdata.fill(0)
                self.playing = False
                raise sd.CallbackStop
                
            valid_idx = idx[:valid_len]
            idx_int = valid_idx.astype(np.int32)
            idx_frac = valid_idx - idx_int
            
            if self.audio_data.ndim == 2 and self.audio_data.shape[1] > 1:
                idx_frac = idx_frac.reshape(-1, 1)
                
            val = self.audio_data[idx_int] * (1.0 - idx_frac) + self.audio_data[idx_int + 1] * idx_frac
            outdata[:valid_len] = val * volume
            
            if valid_len < frames:
                outdata[valid_len:].fill(0)
                self.playing = False
                raise sd.CallbackStop
                
            self.pos += frames * speed
            
    def position_sec(self):
        return self.pos / self.sr if self.sr else 0.0


class AudioSettingsDialog(MessageBoxBase):
    def __init__(self, current_device_id, parent=None):
        super().__init__(parent)
        if hasattr(self, 'titleLabel'):
            self.titleLabel.setText("音频设备设置")
        else:
            self.setWindowTitle("音频设备设置")
        
        self.device_combo = ComboBox(self)
        self.info_label = BodyLabel("", self)
        
        self.devices = sd.query_devices()
        self.hostapis = sd.query_hostapis()
        
        # 收集所有输出设备，按延迟排序（低延迟优先）
        output_devices = []
        for i, dev in enumerate(self.devices):
            if dev['max_output_channels'] > 0:
                api_name = self.hostapis[dev['hostapi']]['name']
                latency_ms = dev['default_low_output_latency'] * 1000
                output_devices.append((i, api_name, dev['name'], latency_ms))
        
        # 按延迟排序
        output_devices.sort(key=lambda x: x[3])
        
        self.valid_devices = []
        current_idx = -1
        
        for i, api_name, dev_name, latency_ms in output_devices:
            self.valid_devices.append(i)
            # 显示延迟信息
            name = f"[{api_name}] {dev_name} ({latency_ms:.1f}ms)"
            self.device_combo.addItem(name, userData=i)
            if i == current_device_id:
                current_idx = len(self.valid_devices) - 1
                    
        if current_idx >= 0:
            self.device_combo.setCurrentIndex(current_idx)
        elif self.device_combo.count() > 0:
            self.device_combo.setCurrentIndex(0)
            
        self.device_combo.currentIndexChanged.connect(self.update_info)
        
        self.viewLayout.addWidget(SubtitleLabel("输出设备:", self))
        self.viewLayout.addWidget(self.device_combo)
        self.viewLayout.addWidget(self.info_label)
        
        self.update_info()
        
    def update_info(self):
        idx = self.device_combo.currentIndex()
        if idx < 0: return
        dev_id = self.device_combo.itemData(idx)
        dev = self.devices[dev_id]
        
        sr = int(dev['default_samplerate'])
        lat_low = dev['default_low_output_latency'] * 1000
        lat_high = dev['default_high_output_latency'] * 1000
        
        info = f"默认采样率: {sr} Hz\n"
        info += f"预计输出延迟 (Low): {lat_low:.1f} ms\n"
        info += f"预计输出延迟 (High): {lat_high:.1f} ms\n"
        
        self.info_label.setText(info)


class TutorialIllustration(QWidget):
    """绘制教程图示的自定义组件"""
    def __init__(self, illustration_type: str, parent=None):
        super().__init__(parent)
        self.illustration_type = illustration_type
        self.setFixedSize(280, 100)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w, h = self.width(), self.height()
        
        # 背景
        painter.fillRect(0, 0, w, h, QColor(35, 35, 38))
        painter.setPen(QPen(QColor(60, 60, 65), 1))
        painter.drawRect(0, 0, w - 1, h - 1)
        
        if self.illustration_type == "zoom":
            self._draw_zoom_illustration(painter, w, h)
        elif self.illustration_type == "drag":
            self._draw_drag_illustration(painter, w, h)
        elif self.illustration_type == "snap":
            self._draw_snap_illustration(painter, w, h)
        elif self.illustration_type == "select":
            self._draw_select_illustration(painter, w, h)
        elif self.illustration_type == "batch":
            self._draw_batch_illustration(painter, w, h)
            
    def _draw_waveform(self, painter, x, y, w, h, zoom=1.0):
        """绘制简化的波形"""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 150, 255, 150)))
        
        mid_y = y + h / 2
        points = []
        for i in range(int(w)):
            amp = np.sin(i * 0.15 / zoom) * 0.3 + np.sin(i * 0.08 / zoom) * 0.5
            amp *= h * 0.4
            points.append((x + i, mid_y - amp, mid_y + amp))
        
        path = QPainterPath()
        path.moveTo(points[0][0], points[0][1])
        for px, py_top, _ in points[1:]:
            path.lineTo(px, py_top)
        for px, _, py_bot in reversed(points):
            path.lineTo(px, py_bot)
        path.closeSubpath()
        painter.drawPath(path)
        
    def _draw_lyric_marker(self, painter, x, y, h, text, color=QColor(255, 140, 0), selected=False):
        """绘制歌词标记"""
        # 竖线
        line_color = QColor(0, 200, 255) if selected else color
        painter.setPen(QPen(line_color, 2 if selected else 1))
        painter.drawLine(int(x), int(y), int(x), int(y + h))
        
        # 标签
        font = QFont("Microsoft YaHei", 7)
        painter.setFont(font)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        
        bg_color = QColor(0, 150, 200, 200) if selected else QColor(60, 60, 65, 220)
        rect_bg = QRectF(x + 3, y + 5, tw + 8, th + 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(rect_bg, 3, 3)
        
        if selected:
            painter.setPen(QPen(QColor(0, 200, 255, 200), 1))
            painter.drawRoundedRect(rect_bg, 3, 3)
        
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(rect_bg, Qt.AlignmentFlag.AlignCenter, text)
        
    def _draw_mouse_cursor(self, painter, x, y, click=False):
        """绘制鼠标指针"""
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x, y + 14)
        path.lineTo(x + 4, y + 11)
        path.lineTo(x + 7, y + 17)
        path.lineTo(x + 9, y + 16)
        path.lineTo(x + 6, y + 10)
        path.lineTo(x + 10, y + 10)
        path.closeSubpath()
        
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        painter.drawPath(path)
        
        if click:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(255, 200, 0, 150)))
            painter.drawEllipse(int(x - 8), int(y - 8), 16, 16)
        
    def _draw_zoom_illustration(self, painter, w, h):
        """缩放操作图示"""
        # 左侧：缩小的波形
        self._draw_waveform(painter, 10, 20, 80, 60, zoom=0.5)
        
        # 中间：箭头
        painter.setPen(QPen(QColor(150, 150, 150), 2))
        painter.drawLine(100, 50, 130, 50)
        painter.drawLine(125, 45, 130, 50)
        painter.drawLine(125, 55, 130, 50)
        
        # 右侧：放大的波形
        self._draw_waveform(painter, 140, 20, 130, 60, zoom=2.0)
        
        # 鼠标+滚轮图示
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        painter.setBrush(QBrush(QColor(80, 80, 85)))
        painter.drawRoundedRect(115, 70, 24, 20, 4, 4)
        painter.setPen(QPen(QColor(0, 200, 255), 2))
        painter.drawLine(127, 73, 127, 87)  # 滚轮
        
        # Ctrl 标签
        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(95, 85, "Ctrl+")
        
    def _draw_drag_illustration(self, painter, w, h):
        """拖拽操作图示"""
        self._draw_waveform(painter, 10, 20, w - 20, 60, zoom=1.5)
        
        # 歌词标记（正在被拖拽）
        self._draw_lyric_marker(painter, 80, 20, 60, "歌词", QColor(255, 140, 0))
        
        # 虚线箭头表示移动
        painter.setPen(QPen(QColor(255, 200, 0), 2, Qt.PenStyle.DashLine))
        painter.drawLine(90, 40, 150, 40)
        painter.drawLine(145, 35, 150, 40)
        painter.drawLine(145, 45, 150, 40)
        
        # 目标位置（虚线）
        painter.setPen(QPen(QColor(255, 140, 0, 100), 1, Qt.PenStyle.DashLine))
        painter.drawLine(160, 20, 160, 80)
        
        # 鼠标
        self._draw_mouse_cursor(painter, 85, 45, click=True)
        
    def _draw_snap_illustration(self, painter, w, h):
        """双击吸附图示"""
        self._draw_waveform(painter, 10, 20, w - 20, 60, zoom=1.5)
        
        # 远处的歌词标记
        self._draw_lyric_marker(painter, 50, 20, 60, "词", QColor(200, 200, 200, 150))
        
        # 闪电/吸附效果
        painter.setPen(QPen(QColor(255, 200, 0), 2))
        painter.drawLine(75, 40, 140, 40)
        
        # 目标位置
        painter.setPen(QPen(QColor(255, 140, 0), 2))
        painter.drawLine(180, 20, 180, 80)
        
        # 吸附后位置
        self._draw_lyric_marker(painter, 180, 20, 60, "词", QColor(255, 140, 0))
        
        # 双击图示
        self._draw_mouse_cursor(painter, 175, 50, click=True)
        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        painter.setPen(QColor(255, 200, 0))
        painter.drawText(195, 60, "×2")
        
    def _draw_select_illustration(self, painter, w, h):
        """框选操作图示"""
        self._draw_waveform(painter, 10, 20, w - 20, 60, zoom=1.5)
        
        # 多个歌词标记
        self._draw_lyric_marker(painter, 60, 20, 60, "A", selected=True)
        self._draw_lyric_marker(painter, 120, 20, 60, "B", selected=True)
        self._draw_lyric_marker(painter, 180, 20, 60, "C", selected=True)
        self._draw_lyric_marker(painter, 240, 20, 60, "D")
        
        # 框选矩形
        painter.setPen(QPen(QColor(0, 200, 255), 2, Qt.PenStyle.DashLine))
        painter.setBrush(QBrush(QColor(0, 200, 255, 30)))
        painter.drawRect(50, 15, 145, 75)
        
        # 右键标识
        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(200, 85, "右键拖拽")
        
    def _draw_batch_illustration(self, painter, w, h):
        """批量移动图示"""
        self._draw_waveform(painter, 10, 20, w - 20, 60, zoom=1.5)
        
        # 多个选中的歌词标记
        self._draw_lyric_marker(painter, 40, 20, 60, "A", selected=True)
        self._draw_lyric_marker(painter, 80, 20, 60, "B", selected=True)
        
        # 箭头表示整体移动
        painter.setPen(QPen(QColor(0, 200, 255), 2))
        painter.drawLine(110, 50, 150, 50)
        painter.drawLine(145, 45, 150, 50)
        painter.drawLine(145, 55, 150, 50)
        
        # 移动后的位置
        self._draw_lyric_marker(painter, 170, 20, 60, "A", selected=True)
        self._draw_lyric_marker(painter, 210, 20, 60, "B", selected=True)
        
        # 鼠标
        self._draw_mouse_cursor(painter, 165, 50, click=True)


class TutorialDialog(MessageBoxBase):
    """首次加载后的功能引导对话框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        
        if hasattr(self, 'titleLabel'):
            self.titleLabel.setText("欢迎使用歌词时间轴校准")
        
        # 主内容区
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(16)
        
        # 说明文字
        intro = BodyLabel("以下是核心操作方式，助你快速上手：")
        intro.setStyleSheet("color: #aaa;")
        layout.addWidget(intro)
        
        # 操作说明项
        tutorials = [
            ("zoom", "Ctrl + 滚轮", "缩放波形，聚焦细节"),
            ("drag", "拖拽橙色标记", "精确调整歌词时间位置"),
            ("snap", "双击目标位置", "瞬间吸附最近歌词到该位置"),
            ("select", "右键拖拽框选", "选择多个歌词标记"),
            ("batch", "拖拽选中项", "批量移动所有选中的歌词"),
        ]
        
        for ill_type, shortcut, desc in tutorials:
            item_widget = QWidget()
            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(12)
            
            # 图示
            illustration = TutorialIllustration(ill_type)
            item_layout.addWidget(illustration)
            
            # 文字说明
            text_widget = QWidget()
            text_layout = QVBoxLayout(text_widget)
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(2)
            
            shortcut_label = SubtitleLabel(shortcut)
            shortcut_label.setStyleSheet("color: #0af; font-weight: bold;")
            text_layout.addWidget(shortcut_label)
            
            desc_label = BodyLabel(desc)
            desc_label.setStyleSheet("color: #ccc;")
            text_layout.addWidget(desc_label)
            
            text_layout.addStretch()
            item_layout.addWidget(text_widget, 1)
            
            layout.addWidget(item_widget)
        
        # 额外提示
        tips = BodyLabel("提示: Ctrl+A 全选 | Esc 取消选择 | Ctrl+Z/Y 撤销/重做")
        tips.setStyleSheet("color: #888; font-size: 11px; margin-top: 8px;")
        layout.addWidget(tips)
        
        self.viewLayout.addWidget(content)
        
        # 调整按钮文字
        self.yesButton.setText("开始使用")
        self.cancelButton.hide()
        
        # 调整窗口大小
        self.widget.setMinimumWidth(480)


class AudioLoaderThread(QThread):
    finished = pyqtSignal(np.ndarray, np.ndarray, float, np.ndarray, int)  # env_min, env_max, duration, raw_data, sr
    error = pyqtSignal(str)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath
        
    def run(self):
        try:
            # 极速读取音频
            data, sr = sf.read(self.filepath, always_2d=True, dtype='float32')
            if data.shape[1] > 1:
                mono_data = np.mean(data, axis=1)
            else:
                mono_data = data[:, 0]
                
            duration = len(data) / sr
            
            # 降采样到 1000Hz (1ms精度)，足以应对所有可视化需求
            target_sr = 1000
            factor = sr // target_sr
            if factor > 0:
                pad_size = factor - (len(mono_data) % factor)
                if pad_size != factor:
                    mono_data = np.pad(mono_data, (0, pad_size))
                mono_data = mono_data.reshape(-1, factor)
                env_max = np.max(mono_data, axis=1)
                env_min = np.min(mono_data, axis=1)
            else:
                env_max = mono_data
                env_min = mono_data
                
            self.finished.emit(env_min, env_max, duration, data, sr)
        except Exception as e:
            self.error.emit(f"音频加载失败: {str(e)}\n{traceback.format_exc()}")


class FilterAnchor:
    def __init__(self, time_seconds, is_start=True):
        self.time_seconds = time_seconds
        self.is_start = is_start

def clone_anchors(anchors):
    return [FilterAnchor(a.time_seconds, a.is_start) for a in anchors]


def clone_editor_state(state):
    """深拷贝 (歌词, 过滤锚点) 编辑状态"""
    if isinstance(state, tuple) and len(state) == 2:
        lyrics, anchors = state
        return clone_lyrics(lyrics), clone_anchors(anchors)
    return clone_lyrics(state), []


class PlayheadOverlay(QWidget):
    """仅绘制播放指针，高帧率局部刷新，不触发波形重绘"""

    STRIP_MARGIN = 18

    def __init__(self, timeline: "AudioTimelineWidget"):
        super().__init__(timeline)
        self._timeline = timeline
        self._playhead_sec = 0.0
        self._last_draw_x = -1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        timeline.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._timeline and event.type() == QEvent.Type.Resize:
            self.setGeometry(0, 0, self._timeline.width(), self._timeline.height())
            self.raise_()
            self._last_draw_x = -1.0
            self.update()
        return super().eventFilter(obj, event)

    def set_playhead(self, sec: float, force_full: bool = False):
        old_x = self._last_draw_x
        self._playhead_sec = max(0.0, sec)
        new_x = self._playhead_sec * self._timeline.zoom
        if force_full or old_x < 0:
            self._last_draw_x = new_x
            self.update()
            return
        self._repaint_strip(old_x, new_x)

    def _repaint_strip(self, old_x: float, new_x: float):
        h = max(1, self.height())
        left = int(min(old_x, new_x)) - self.STRIP_MARGIN
        width = int(abs(new_x - old_x)) + 2 * self.STRIP_MARGIN + 2
        self.update(max(0, left), 0, max(1, width), h)
        self._last_draw_x = new_x

    def paintEvent(self, event):
        zoom = self._timeline.zoom
        if zoom <= 0:
            return
        px = self._playhead_sec * zoom
        h = self.height()
        ruler_h = 26

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(255, 50, 50), 2))
        painter.drawLine(QPointF(px, 0), QPointF(px, h))

        path_tri = QPainterPath()
        path_tri.moveTo(px - 6, 0)
        path_tri.lineTo(px + 6, 0)
        path_tri.lineTo(px, ruler_h)
        path_tri.closeSubpath()
        painter.setBrush(QColor(255, 50, 50))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path_tri)


class AudioTimelineWidget(QWidget):
    """
    音频时间轴与歌词渲染组件
    """
    time_changed = pyqtSignal(float)
    zoom_changed = pyqtSignal(float, float) # old_x, new_x
    lyric_changed = pyqtSignal()
    state_committed = pyqtSignal(object, object) # old_state, new_state (用于撤销重做)
    
    def __init__(self):
        super().__init__()
        self.env_min = np.array([])
        self.env_max = np.array([])
        self.duration = 0.0
        
        self.zoom = 100.0  # pixels per second
        self.playhead_sec = 0.0
        self.lyrics = []
        
        self.dragging_idx = -1
        self.hover_idx = -1
        self.dragging_anchor_idx = -1
        self.hover_anchor_idx = -1
        self._drag_start_state = None
        
        # 多过滤锚点支持
        self.filter_anchors = []
        
        # 框选多选支持
        self.selected_indices = set()  # 选中的歌词索引
        self.is_box_selecting = False  # 是否正在框选
        self.box_select_start = None   # 框选起始点 (x, y)
        self.box_select_rect = None    # 当前框选矩形
        self._batch_drag_offsets = {}  # 批量拖拽时各歌词的初始偏移
        
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(250)
        
        self.font = QFont("Microsoft YaHei", 9, QFont.Weight.Bold)
    
    def set_filter_start(self, sec):
        old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
        val = max(0.0, min(self.duration, sec)) if self.duration > 0 else sec
        self.filter_anchors.append(FilterAnchor(val, True))
        self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
        self.update()
    
    def set_filter_end(self, sec):
        old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
        val = max(0.0, min(self.duration, sec)) if self.duration > 0 else sec
        self.filter_anchors.append(FilterAnchor(val, False))
        self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
        self.update()
    
    def clear_filter_anchors(self):
        old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
        self.filter_anchors.clear()
        self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
        self.update()
        
    def set_audio_data(self, env_min, env_max, duration):
        self.env_min = env_min
        self.env_max = env_max
        self.duration = duration
        self._update_size()
        
    def set_lyrics(self, lyrics):
        self.lyrics = lyrics
        self.update()

    def set_editor_state(self, lyrics, anchors):
        self.lyrics = lyrics
        self.filter_anchors = anchors
        self.hover_anchor_idx = -1
        self.hover_idx = -1
        self.dragging_anchor_idx = -1
        self.dragging_idx = -1
        self.update()

    def remove_filter_anchor(self, index: int):
        if index < 0 or index >= len(self.filter_anchors):
            return
        old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
        self.filter_anchors.pop(index)
        self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
        self.update()
        
    def set_playhead(self, sec, repaint: bool = True):
        """更新播放头时间；repaint=False 时由 PlayheadOverlay 负责绘制"""
        self.playhead_sec = sec
        if repaint:
            self.update()
    
    def _update_size(self):
        if self.duration > 0:
            self.setFixedSize(int(self.duration * self.zoom) + 100, self.parent().height() - 20)
        self.update()
        
    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            zoom_factor = 1.2 if delta > 0 else 1 / 1.2
            
            new_zoom = self.zoom * zoom_factor
            new_zoom = max(10.0, min(5000.0, new_zoom))
            
            if new_zoom != self.zoom:
                # 鼠标在 widget 上的位置（event.position() 已是 widget 坐标）
                mouse_x = event.position().x()
                # 鼠标下方的时间点（秒）
                time_under_mouse = mouse_x / self.zoom
                # 缩放后该时间点在 widget 上的新位置
                new_x = time_under_mouse * new_zoom
                
                self.zoom = new_zoom
                self._update_size()
                # 发送旧位置和新位置的差值信息
                self.zoom_changed.emit(mouse_x, new_x)
                event.accept()
        else:
            super().wheelEvent(event)
            
    def _get_hit_anchor(self, x_pos):
        hit_range = 8.0
        closest_idx = -1
        min_dist = hit_range
        for i, a in enumerate(self.filter_anchors):
            ax = a.time_seconds * self.zoom
            dist = abs(ax - x_pos)
            if dist < min_dist:
                min_dist = dist
                closest_idx = i
        return closest_idx

    def _get_hit_lyric(self, x_pos):
        hit_range = 8.0 # 像素容差
        closest_idx = -1
        min_dist = hit_range
        
        for i, ev in enumerate(self.lyrics):
            lyric_x = ev.time_seconds * self.zoom
            dist = abs(lyric_x - x_pos)
            if dist < min_dist:
                min_dist = dist
                closest_idx = i
        return closest_idx

    def _get_lyric_label_rect(self, idx):
        """计算歌词文本块矩形，用于双击编辑命中测试"""
        if idx < 0 or idx >= len(self.lyrics):
            return QRectF()
        ev = self.lyrics[idx]
        lx = ev.time_seconds * self.zoom
        y_offset = 26 + 5 + (idx % 5) * 28
        display_text = ev.text if ev.text else " "
        fm = self.fontMetrics()
        tw = fm.horizontalAdvance(display_text)
        th = fm.height()
        return QRectF(lx + 4, y_offset, tw + 12, th + 8)

    def _get_hit_lyric_label(self, x_pos, y_pos):
        """命中文本块（而不是竖线）"""
        for i, _ev in enumerate(self.lyrics):
            if self._get_lyric_label_rect(i).contains(x_pos, y_pos):
                return i
        return -1

    def _contains_cjk_char(self, text: str) -> bool:
        """是否包含 CJK/日文/韩文字符"""
        for ch in text:
            code = ord(ch)
            if (
                0x3400 <= code <= 0x4DBF or   # CJK 扩展 A
                0x4E00 <= code <= 0x9FFF or   # CJK 基本区
                0xF900 <= code <= 0xFAFF or   # CJK 兼容汉字
                0x3040 <= code <= 0x309F or   # 平假名
                0x30A0 <= code <= 0x30FF or   # 片假名
                0xAC00 <= code <= 0xD7AF      # 韩文
            ):
                return True
        return False

    def _edit_lyric_text(self, idx):
        if idx < 0 or idx >= len(self.lyrics):
            return
        old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
        old_text = self.lyrics[idx].text
        text, ok = QInputDialog.getText(self, "编辑歌词锚点", "请输入一个字符或一个单词：", text=old_text)
        if not ok:
            return

        new_text = text.strip()
        # 限制：仅允许一个字符或一个单词（无空白分隔）
        if not new_text:
            self.lyrics[idx].text = ""
        elif any(ch.isspace() for ch in new_text):
            InfoBar.warning("输入无效", "仅允许输入一个字或一个单词（不能包含空格）", parent=self.window())
            return
        elif self._contains_cjk_char(new_text) and len(new_text) != 1:
            InfoBar.warning("输入无效", "包含中文/日文/韩文时，仅允许输入 1 个字符", parent=self.window())
            return
        else:
            self.lyrics[idx].text = new_text

        self.lyric_changed.emit()
        self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
        self.update()
        
    def mousePressEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        
        # 右键开始框选
        if event.button() == Qt.MouseButton.RightButton:
            self.is_box_selecting = True
            self.box_select_start = (x, y)
            self.box_select_rect = None
            return
        
        if event.button() == Qt.MouseButton.LeftButton:
            # 双击歌词文本块：编辑文本；双击空白：吸附最近歌词
            if event.type() == QEvent.Type.MouseButtonDblClick:
                # 优先判断是否双击在某个歌词文本块上
                hit_label_idx = self._get_hit_lyric_label(x, y)
                if hit_label_idx != -1:
                    self._edit_lyric_text(hit_label_idx)
                    return
                time_sec = x / self.zoom
                if self.lyrics:
                    old_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
                    closest_idx = min(range(len(self.lyrics)), key=lambda i: abs(self.lyrics[i].time_seconds - time_sec))
                    self.lyrics[closest_idx].time_seconds = time_sec
                    self.lyric_changed.emit()
                    self.state_committed.emit(old_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
                    self.update()
                return

            # 优先判断是否点中了过滤锚点句柄
            idx_a = self._get_hit_anchor(x)
            if idx_a != -1:
                self.dragging_anchor_idx = idx_a
                self._drag_start_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
                self.selected_indices.clear()  # 清除歌词选择
                self.update()
                return

            # 判断是否点中了歌词句柄
            idx = self._get_hit_lyric_label(x, y)
            if idx == -1:
                idx = self._get_hit_lyric(x)
            if idx != -1:
                # Ctrl+点击切换选择状态
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    if idx in self.selected_indices:
                        self.selected_indices.discard(idx)
                    else:
                        self.selected_indices.add(idx)
                    self.update()
                    return
                
                # 如果点击的是已选中的歌词，准备批量拖拽
                if idx in self.selected_indices and len(self.selected_indices) > 1:
                    self.dragging_idx = idx
                    self._drag_start_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
                    # 记录所有选中歌词相对于拖拽歌词的时间偏移
                    base_time = self.lyrics[idx].time_seconds
                    self._batch_drag_offsets = {i: self.lyrics[i].time_seconds - base_time for i in self.selected_indices}
                else:
                    # 点击未选中的歌词，清除选择并单独拖拽
                    self.selected_indices.clear()
                    self.selected_indices.add(idx)
                    self.dragging_idx = idx
                    self._drag_start_state = (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors))
                    self._batch_drag_offsets = {idx: 0.0}
                self.update()
            else:
                # 点击空白处，清除选择并移动播放头
                self.selected_indices.clear()
                self.update()
                time_sec = max(0.0, min(self.duration, x / self.zoom))
                self.time_changed.emit(time_sec)
                
    def mouseMoveEvent(self, event):
        x = event.position().x()
        y = event.position().y()
        
        # 框选中
        if self.is_box_selecting and self.box_select_start:
            sx, sy = self.box_select_start
            self.box_select_rect = (min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))
            self.update()
            return
        
        if self.dragging_anchor_idx != -1:
            time_sec = max(0.0, min(self.duration, x / self.zoom))
            self.filter_anchors[self.dragging_anchor_idx].time_seconds = time_sec
            self.update()
        elif self.dragging_idx != -1:
            # 批量拖拽所有选中的歌词
            base_time = max(0.0, min(self.duration, x / self.zoom))
            for idx, offset in self._batch_drag_offsets.items():
                if 0 <= idx < len(self.lyrics):
                    new_time = max(0.0, min(self.duration, base_time + offset))
                    self.lyrics[idx].time_seconds = new_time
            self.lyric_changed.emit()
            self.update()
        else:
            # Hover 效果
            idx_a = self._get_hit_anchor(x)
            idx_l = -1
            if idx_a == -1:
                idx_l = self._get_hit_lyric_label(x, y)
                if idx_l == -1:
                    idx_l = self._get_hit_lyric(x)

            if idx_a != self.hover_anchor_idx or idx_l != self.hover_idx:
                self.hover_anchor_idx = idx_a
                self.hover_idx = idx_l
                self.update()
                
            if event.buttons() & Qt.MouseButton.LeftButton:
                # 拖动播放头
                time_sec = max(0.0, min(self.duration, x / self.zoom))
                self.time_changed.emit(time_sec)
                
    def mouseReleaseEvent(self, event):
        # 框选结束
        if event.button() == Qt.MouseButton.RightButton and self.is_box_selecting:
            if self.box_select_rect:
                rx, ry, rw, rh = self.box_select_rect
                # 计算框选范围内的歌词（基于 x 坐标，y 轴不限制）
                # 如果没有按住 Ctrl，清除之前的选择
                if event.modifiers() != Qt.KeyboardModifier.ControlModifier:
                    self.selected_indices.clear()
                
                for i, ev in enumerate(self.lyrics):
                    lyric_x = ev.time_seconds * self.zoom
                    if rx <= lyric_x <= rx + rw:
                        self.selected_indices.add(i)
            
            self.is_box_selecting = False
            self.box_select_start = None
            self.box_select_rect = None
            self.update()
            return
        
        if self.dragging_anchor_idx != -1 or self.dragging_idx != -1:
            if self._drag_start_state is not None:
                # 检查是否有实质性的修改
                changed = False
                old_lyrics, old_anchors = self._drag_start_state
                
                if len(old_lyrics) == len(self.lyrics):
                    for old_l, new_l in zip(old_lyrics, self.lyrics):
                        if old_l.time_seconds != new_l.time_seconds:
                            changed = True
                            break
                if len(old_anchors) != len(self.filter_anchors):
                    changed = True
                elif not changed:
                    for old_a, new_a in zip(old_anchors, self.filter_anchors):
                        if old_a.time_seconds != new_a.time_seconds:
                            changed = True
                            break
                            
                if changed:
                    self.state_committed.emit(self._drag_start_state, (clone_lyrics(self.lyrics), clone_anchors(self.filter_anchors)))
            
            self.dragging_anchor_idx = -1
            self.dragging_idx = -1
            self._drag_start_state = None
            self._batch_drag_offsets = {}

    def keyPressEvent(self, event):
        # Delete/Backspace 删除悬停的锚点
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.hover_anchor_idx >= 0:
            self.remove_filter_anchor(self.hover_anchor_idx)
            event.accept()
            return
        
        # Escape 清除选择
        if event.key() == Qt.Key.Key_Escape:
            if self.selected_indices:
                self.selected_indices.clear()
                self.update()
                event.accept()
                return
        
        # Ctrl+A 全选歌词
        if event.key() == Qt.Key.Key_A and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.selected_indices = set(range(len(self.lyrics)))
            self.update()
            event.accept()
            return
        
        super().keyPressEvent(event)
        
    def paintEvent(self, event):
        if self.duration <= 0 or len(self.env_max) == 0:
            return
        
        rect = event.rect()
        h = self.height()
        ruler_h = 26
        zoom = self.zoom
        
        start_x = rect.left()
        end_x = rect.right()
        start_time = max(0.0, start_x / zoom)
        end_time = min(self.duration, end_x / zoom)
        
        painter = QPainter(self)
        # 关闭抗锯齿以提升性能，减少对音频线程的影响
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        
        # --- 1. 背景 ---
        painter.fillRect(rect, QColor(30, 30, 32))
        
        # --- 2. 标尺 ---
        painter.fillRect(QRectF(start_x, 0, end_x - start_x, ruler_h), QColor(45, 45, 48))
        painter.setPen(QPen(QColor(80, 80, 85), 1))
        painter.drawLine(int(start_x), ruler_h, int(end_x), ruler_h)
        
        # --- 3. 时间网格 ---
        grid_step = 1.0
        if zoom > 150: grid_step = 0.5
        if zoom > 300: grid_step = 0.1
        if zoom > 1000: grid_step = 0.05
        
        t = (start_time // grid_step) * grid_step
        painter.setFont(QFont("Segoe UI", 8))
        while t <= end_time:
            x = t * zoom
            is_major = abs(t % 1.0) < 0.01
            
            pen_color = QColor(80, 80, 85) if is_major else QColor(50, 50, 55)
            painter.setPen(QPen(pen_color, 1, Qt.PenStyle.SolidLine if is_major else Qt.PenStyle.DashLine))
            painter.drawLine(int(x), ruler_h, int(x), h)
            
            painter.setPen(QColor(150, 150, 150))
            painter.drawLine(int(x), ruler_h - (6 if is_major else 3), int(x), ruler_h)
            if is_major or grid_step < 0.5:
                painter.drawText(int(x) + 3, ruler_h - 6, f"{t:.1f}s")
            t += grid_step

        # --- 4. 波形（固定 500 个点，无论缩放比例）---
        start_idx = max(0, int(start_time * 1000))
        end_idx = min(len(self.env_min), int(end_time * 1000) + 1)
        num_samples = end_idx - start_idx
        
        if num_samples > 0:
            mid_y = ruler_h + (h - ruler_h) / 2
            amp = (h - ruler_h) * 0.45
            
            # 固定 500 个点，确保绑定性能稳定
            num_points = min(500, num_samples)
            step = max(1, num_samples // num_points)
            
            # 使用 numpy 向量化计算，避免 Python 循环
            indices = np.arange(start_idx, end_idx, step)
            if len(indices) > 0:
                x_coords = indices / 1000.0 * zoom
                y_max = mid_y - self.env_max[indices] * amp
                y_min = mid_y - self.env_min[indices] * amp
                
                # 构建路径
                path = QPainterPath()
                path.moveTo(x_coords[0], y_max[0])
                for i in range(1, len(indices)):
                    path.lineTo(x_coords[i], y_max[i])
                for i in range(len(indices) - 1, -1, -1):
                    path.lineTo(x_coords[i], y_min[i])
                path.closeSubpath()
                
                # 简化填充（不用渐变）
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(0, 150, 255, 180)))
                painter.drawPath(path)
            
            # 中心线
            painter.setPen(QPen(QColor(255, 255, 255, 50), 1))
            painter.drawLine(int(start_x), int(mid_y), int(end_x), int(mid_y))

        # --- 5. 绘制过滤区间背景（在歌词下面）---
        sorted_anchors = sorted(self.filter_anchors, key=lambda x: x.time_seconds)
        intervals = []
        active_start = None
        for a in sorted_anchors:
            if a.is_start:
                if active_start is None:
                    active_start = a.time_seconds
            else:
                if active_start is not None:
                    intervals.append((active_start, a.time_seconds))
                    active_start = None
        # 未配对的起始锚点不绘制区间底色（保存时仍会视为过滤至曲末）

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(120, 120, 0, 80)))
        for s_sec, e_sec in intervals:
            sx = s_sec * zoom
            ex = e_sec * zoom
            if ex > start_x and sx < end_x:
                painter.drawRect(QRectF(max(sx, start_x), ruler_h, min(ex, end_x) - max(sx, start_x), h - ruler_h))

        # --- 3. 绘制歌词标记 ---
        painter.setFont(self.font)
        for i, ev in enumerate(self.lyrics):
            lx = ev.time_seconds * zoom
            if lx < start_x - 100 or lx > end_x + 100:
                continue 
                
            is_hover = (i == self.hover_idx)
            is_drag = (i == self.dragging_idx)
            is_selected = (i in self.selected_indices)
            
            # 选中状态使用青色，悬停/拖拽使用橙色
            if is_drag or is_hover:
                line_color = QColor(255, 140, 0)
            elif is_selected:
                line_color = QColor(0, 200, 255)
            else:
                line_color = QColor(200, 200, 200, 150)
            
            painter.setPen(QPen(line_color, 2 if (is_hover or is_drag or is_selected) else 1))
            painter.drawLine(int(lx), ruler_h, int(lx), h)
            
            y_offset = ruler_h + 5 + (i % 5) * 28
            text = ev.text if ev.text else " "
            if is_drag:
                text = f"{text} [{ev.time_seconds:.3f}s]"
                
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            
            # 背景颜色：拖拽>悬停>选中>普通
            if is_drag:
                bg_color = QColor(255, 140, 0, 230)
            elif is_hover:
                bg_color = QColor(255, 160, 50, 200)
            elif is_selected:
                bg_color = QColor(0, 150, 200, 200)
            else:
                bg_color = QColor(60, 60, 65, 200)
            
            text_color = QColor(255, 255, 255) if (is_drag or is_hover or is_selected) else QColor(220, 220, 220)
            
            rect_bg = QRectF(lx + 4, y_offset, tw + 12, th + 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg_color))
            painter.drawRoundedRect(rect_bg, 4, 4)
            
            # 选中状态加粗边框
            if is_selected:
                painter.setPen(QPen(QColor(0, 200, 255, 200), 2))
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
            painter.drawRoundedRect(rect_bg, 4, 4)
            
            painter.setPen(text_color)
            painter.drawText(rect_bg, Qt.AlignmentFlag.AlignCenter, text)

        # --- 4. 绘制锚点句柄 ---
        for i, a in enumerate(self.filter_anchors):
            ax = a.time_seconds * zoom
            if ax < start_x - 100 or ax > end_x + 100:
                continue
                
            is_hover = (i == self.hover_anchor_idx)
            is_drag = (i == self.dragging_anchor_idx)
            
            line_color = QColor(180, 0, 0) if (is_hover or is_drag) else QColor(220, 180, 0, 150)
            painter.setPen(QPen(line_color, 2 if (is_hover or is_drag) else 1))
            painter.drawLine(int(ax), ruler_h, int(ax), h)
            
            text = "[ 起始" if a.is_start else "] 结束"
            if is_drag:
                text = f"{text} [{a.time_seconds:.3f}s]"
                
            y_offset = h - 35
            
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            
            bg_color = QColor(180, 0, 0, 230) if is_drag else (QColor(180, 0, 0, 200) if is_hover else QColor(220, 180, 0, 200))
            text_color = QColor(255, 255, 255)
            
            rect_bg = QRectF(ax + (4 if a.is_start else -tw-12), y_offset, tw + 12, th + 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg_color))
            painter.drawRoundedRect(rect_bg, 4, 4)
            
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
            painter.drawRoundedRect(rect_bg, 4, 4)
            
            painter.setPen(text_color)
            painter.drawText(rect_bg, Qt.AlignmentFlag.AlignCenter, text)

        # --- 5. 绘制框选矩形 ---
        if self.is_box_selecting and self.box_select_rect:
            rx, ry, rw, rh = self.box_select_rect
            painter.setPen(QPen(QColor(0, 200, 255), 2, Qt.PenStyle.DashLine))
            painter.setBrush(QBrush(QColor(0, 200, 255, 40)))
            painter.drawRect(QRectF(rx, ry, rw, rh))

        # 播放头由 PlayheadOverlay 绘制，避免每帧重绘整条波形


class LyricCalibratorWidget(QWidget):
    """
    歌词时间轴校准主界面
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        
        self.audio_path = ""
        self.lyric_path = ""
        self.loader_thread = None
        
        # 首次加载教程显示标志
        self._tutorial_shown = False
        
        # 撤销/重做状态栈
        self.undo_stack = []
        self.redo_stack = []
        
        # 播放状态
        self.last_play_start_sec = 0.0
        
        # 音频播放器 (使用自定义低延迟引擎)
        self.player = SDAudioPlayer()
        
        # 播放头显示时间（插值）与音频时钟目标
        self.smooth_play_time = 0.0
        self._playhead_target_sec = 0.0
        self._playhead_anim_last = 0.0
        self.last_timer_call_time = 0
        import time
        self.get_sys_time = time.time
        
        # 低频：自动滚动、播放状态
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_playback_ui)
        self.timer.start(66)
        
        self.setup_ui()
        
        # 高频：仅刷新播放头叠加层（~60fps，不触发波形重绘）
        self.playhead_timer = QTimer(self)
        self.playhead_timer.timeout.connect(self._update_playhead_animation)
        self.playhead_timer.start(16)
        self.setup_shortcuts()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        
        title = TitleLabel("歌词时间轴校准", self)
        main_layout.addWidget(title)
        
        # --- 文件选择区 ---
        file_card = CardWidget(self)
        file_layout = QVBoxLayout(file_card)
        
        h1 = QHBoxLayout()
        h1.addWidget(BodyLabel("原始音频文件:", self))
        self.audio_edit = LineEdit(self)
        self.audio_edit.setPlaceholderText("拖拽或选择包含人声的音频文件 (.wav/.mp3)")
        h1.addWidget(self.audio_edit, 1)
        self.btn_audio = PushButton("浏览", self, icon=FIF.FOLDER)
        self.btn_audio.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_audio.clicked.connect(self.choose_audio)
        h1.addWidget(self.btn_audio)
        file_layout.addLayout(h1)
        
        h2 = QHBoxLayout()
        h2.addWidget(BodyLabel("待校准歌词:", self))
        self.lyric_edit = LineEdit(self)
        self.lyric_edit.setPlaceholderText("拖拽或选择包含时间戳的歌词文件 (.lrc/.csv)")
        h2.addWidget(self.lyric_edit, 1)
        self.btn_lyric = PushButton("浏览", self, icon=FIF.FOLDER)
        self.btn_lyric.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_lyric.clicked.connect(self.choose_lyric)
        h2.addWidget(self.btn_lyric)
        file_layout.addLayout(h2)
        
        h3 = QHBoxLayout()
        h3.addStretch(1)
        self.btn_load = PrimaryPushButton("加载波形与歌词", self, icon=FIF.PLAY)
        self.btn_load.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_load.clicked.connect(self.load_data)
        h3.addWidget(self.btn_load)
        file_layout.addLayout(h3)
        
        main_layout.addWidget(file_card)
        
        # --- 工具栏与播放控制 ---
        toolbar = QHBoxLayout()
        
        # 播放控制
        self.btn_play = TransparentToolButton(FIF.PLAY_SOLID, self)
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_play.setToolTip("播放/暂停 (Space)")
        toolbar.addWidget(self.btn_play)
        
        self.btn_stop = TransparentToolButton(FIF.CLOSE, self) 
        self.btn_stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_stop.setToolTip("停止并回到开头")
        self.btn_stop.clicked.connect(self.stop_play)
        toolbar.addWidget(self.btn_stop)

        toolbar.addSpacing(4)

        # 过滤锚点（|← 起始，→| 结束）
        toolbar_icon_size = self.btn_play.iconSize()
        self.btn_filter_start = TransparentToolButton(self)
        self.btn_filter_start.setIconSize(toolbar_icon_size)
        self.btn_filter_start.setIcon(make_filter_anchor_icon(True, toolbar_icon_size.width()))
        self.btn_filter_start.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_filter_start.setToolTip("放置过滤起始锚点 ([)")
        self.btn_filter_start.clicked.connect(self.place_filter_start)
        toolbar.addWidget(self.btn_filter_start)

        self.btn_filter_end = TransparentToolButton(self)
        self.btn_filter_end.setIconSize(toolbar_icon_size)
        self.btn_filter_end.setIcon(make_filter_anchor_icon(False, toolbar_icon_size.width()))
        self.btn_filter_end.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_filter_end.setToolTip("放置过滤结束锚点 (])")
        self.btn_filter_end.clicked.connect(self.place_filter_end)
        toolbar.addWidget(self.btn_filter_end)

        # 插入歌词锚点（空白，双击文本块后填写）
        self.btn_insert_lyric_anchor = TransparentToolButton(FIF.ADD, self)
        self.btn_insert_lyric_anchor.setIconSize(toolbar_icon_size)
        self.btn_insert_lyric_anchor.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_insert_lyric_anchor.setToolTip("插入歌词锚点 (M)")
        self.btn_insert_lyric_anchor.clicked.connect(self.insert_lyric_anchor)
        toolbar.addWidget(self.btn_insert_lyric_anchor)
        
        self.volume_slider = Slider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(lambda v: self.player.set_volume(v / 100.0))
        toolbar.addWidget(self.volume_slider)
        
        toolbar.addSpacing(8)
        
        # 音频设置
        self.btn_audio_settings = TransparentToolButton(FIF.SETTING, self)
        self.btn_audio_settings.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_audio_settings.setToolTip("音频设备设置")
        self.btn_audio_settings.clicked.connect(self.show_audio_settings)
        toolbar.addWidget(self.btn_audio_settings)
        
        toolbar.addSpacing(8)
        
        # 倍速控制
        toolbar.addWidget(BodyLabel("倍速:", self))
        self.speed_combo = ComboBox(self)
        self.speed_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_combo.addItems(["0.25x", "0.5x", "1.0x", "1.5x", "2.0x", "4.0x"])
        self.speed_combo.setCurrentIndex(2) # 默认 1.0x
        self.speed_combo.currentIndexChanged.connect(self.change_speed)
        toolbar.addWidget(self.speed_combo)
        
        toolbar.addSpacing(8)
        
        # 偏好设置
        self.auto_scroll_check = CheckBox("播放时跟随滚动", self)
        self.auto_scroll_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.auto_scroll_check.setChecked(True)
        toolbar.addWidget(self.auto_scroll_check)
        
        self.return_on_stop_check = CheckBox("停止后回到开始处", self)
        self.return_on_stop_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.return_on_stop_check.setChecked(True)
        toolbar.addWidget(self.return_on_stop_check)
        
        toolbar.addStretch(1)

        # 撤回 / 重做（原过滤锚点右侧区域）
        self.btn_undo = TransparentToolButton(self)
        self.btn_undo.setIconSize(toolbar_icon_size)
        self.btn_undo.setIcon(FIF.CANCEL.icon())
        self.btn_undo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_undo.setToolTip("撤回 (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_undo.setEnabled(False)
        toolbar.addWidget(self.btn_undo)

        self.btn_redo = TransparentToolButton(self)
        self.btn_redo.setIconSize(toolbar_icon_size)
        self.btn_redo.setIcon(make_redo_icon(toolbar_icon_size.width()))
        self.btn_redo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_redo.setToolTip("重做 (Ctrl+Shift+Z / Ctrl+Y)")
        self.btn_redo.clicked.connect(self.redo)
        self.btn_redo.setEnabled(False)
        toolbar.addWidget(self.btn_redo)
        main_layout.addLayout(toolbar)
        
        # --- 可视化视图区 ---
        self.view_card = CardWidget(self)
        view_layout = QVBoxLayout(self.view_card)
        view_layout.setContentsMargins(0, 0, 0, 0)
        
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        
        self.timeline_widget = AudioTimelineWidget()
        self.timeline_widget.time_changed.connect(self.seek_audio)
        self.timeline_widget.zoom_changed.connect(self.on_zoom_changed)
        self.timeline_widget.lyric_changed.connect(self.on_lyric_changed)
        self.timeline_widget.state_committed.connect(self.on_state_committed)
        
        # 包装一层以便支持固定宽度滚动
        self.scroll_content = QWidget()
        scroll_layout = QVBoxLayout(self.scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.addWidget(self.timeline_widget, 0, Qt.AlignmentFlag.AlignLeft)
        
        self.playhead_overlay = PlayheadOverlay(self.timeline_widget)
        self.playhead_overlay.setGeometry(self.timeline_widget.rect())
        self.playhead_overlay.show()
        
        self.scroll_area.setWidget(self.scroll_content)
        view_layout.addWidget(self.scroll_area)
        
        main_layout.addWidget(self.view_card, 1)
        
        # --- 底部保存区 ---
        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(BodyLabel("并句阈值(ms):", self))
        self.merge_threshold_spin = SpinBox(self)
        self.merge_threshold_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.merge_threshold_spin.setRange(50, 1500)
        self.merge_threshold_spin.setValue(350)
        self.merge_threshold_spin.setSingleStep(50)
        self.merge_threshold_spin.setToolTip("新插入歌词并入最近句的最大时间距离")
        bottom_layout.addWidget(self.merge_threshold_spin)
        bottom_layout.addStretch(1)
        
        bottom_layout.addWidget(BodyLabel("导出格式:", self))
        self.export_format_combo = ComboBox(self)
        self.export_format_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.export_format_combo.addItems([
            "源歌词格式",
            "标准 LRC 格式 (*.lrc)",
            "纯文本时间戳格式 (*.txt)",
            "小灰熊/KBuilder 脚本 (*.ksc)",
            "时间戳 CSV 格式 (*.csv)"
        ])
        bottom_layout.addWidget(self.export_format_combo)
        
        self.btn_save = PrimaryPushButton("另存为校准后的歌词...", self, icon=FIF.SAVE)
        self.btn_save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_save.clicked.connect(self.save_lyrics)
        self.btn_save.setEnabled(False)
        bottom_layout.addWidget(self.btn_save)
        main_layout.addLayout(bottom_layout)
        
    def setup_shortcuts(self):
        # 撤销快捷键 Ctrl+Z
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo)
        # 重做快捷键 Ctrl+Shift+Z / Ctrl+Y
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self).activated.connect(self.redo)
        
        # 空格键播放/暂停 (设置 context 确保在当前窗口内任意组件都有焦点时生效)
        play_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        play_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        # 禁用自动重复，防止长按空格时疯狂触发
        play_shortcut.setAutoRepeat(False)
        play_shortcut.activated.connect(self.toggle_play)
        
        # 过滤锚点快捷键 [ 和 ]
        filter_start_shortcut = QShortcut(QKeySequence(Qt.Key.Key_BracketLeft), self)
        filter_start_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        filter_start_shortcut.activated.connect(self.place_filter_start)
        
        filter_end_shortcut = QShortcut(QKeySequence(Qt.Key.Key_BracketRight), self)
        filter_end_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        filter_end_shortcut.activated.connect(self.place_filter_end)

        # 插入歌词锚点快捷键 M
        insert_anchor_shortcut = QShortcut(QKeySequence("M"), self)
        insert_anchor_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        insert_anchor_shortcut.activated.connect(self.insert_lyric_anchor)
        
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            ext = os.path.splitext(path)[1].lower()
            if ext in ['.wav', '.mp3', '.flac', '.ogg', '.m4a']:
                self.audio_edit.setText(path)
            elif ext in ['.lrc', '.csv', '.txt']:
                self.lyric_edit.setText(path)
                
        if self.audio_edit.text() and self.lyric_edit.text():
            self.load_data()
            
    def choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择音频", "", "Audio Files (*.wav *.mp3 *.flac *.ogg)")
        if path: self.audio_edit.setText(path)
            
    def choose_lyric(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择歌词", "", "Lyric Files (*.lrc *.csv *.txt)")
        if path: self.lyric_edit.setText(path)
            
    def load_data(self):
        self.audio_path = self.audio_edit.text().strip()
        self.lyric_path = self.lyric_edit.text().strip()
        
        if not os.path.exists(self.audio_path):
            InfoBar.error("错误", "音频文件不存在", parent=self)
            return
        if not os.path.exists(self.lyric_path):
            InfoBar.error("错误", "歌词文件不存在", parent=self)
            return
            
        self.btn_load.setEnabled(False)
        self.btn_load.setText("加载中...")
        
        self.loader_thread = AudioLoaderThread(self.audio_path)
        self.loader_thread.finished.connect(self.on_audio_loaded)
        self.loader_thread.error.connect(self.on_audio_error)
        self.loader_thread.start()
        
    def on_audio_loaded(self, env_min, env_max, duration, raw_data, sr):
        self.timeline_widget.set_audio_data(env_min, env_max, duration)
        self.player.load(raw_data, sr)
        self.player.set_stream_params(0.03, 512)
        
        try:
            events = parse_lyric_file(self.lyric_path, split_units=False)
            # 加载新文件时清空过滤锚点（锚点不随歌词文件保存，避免上次残留）
            self.timeline_widget.set_editor_state(events, [])
            self._sync_playhead_display(0.0, force_full=True)
            self.btn_save.setEnabled(True)
            
            # 清空历史栈
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.update_undo_redo_buttons()
            
            InfoBar.success("加载成功", f"音频 {duration:.1f}s，歌词 {len(events)} 行", parent=self)
            
            # 首次成功加载后显示功能引导
            if not self._tutorial_shown:
                self._tutorial_shown = True
                QTimer.singleShot(500, self._show_tutorial)
                
        except Exception as e:
            InfoBar.error("歌词解析失败", str(e), parent=self)
            
        self.btn_load.setEnabled(True)
        self.btn_load.setText("加载波形与歌词")
    
    def _show_tutorial(self):
        """显示功能引导对话框"""
        dlg = TutorialDialog(self)
        dlg.exec()
        
    def on_audio_error(self, err_msg):
        InfoBar.error("音频加载失败", err_msg, parent=self)
        self.btn_load.setEnabled(True)
        self.btn_load.setText("加载波形与歌词")
        
    def show_audio_settings(self):
        dlg = AudioSettingsDialog(self.player.device_id, self)
        if dlg.exec():
            idx = dlg.device_combo.currentIndex()
            if idx >= 0:
                dev_id = dlg.device_combo.itemData(idx)
                self.player.set_device(dev_id)
        
    def toggle_play(self):
        if self.player.playing:
            self.player.pause()
            self.btn_play.setIcon(FIF.PLAY_SOLID)
            if self.return_on_stop_check.isChecked():
                self.seek_audio(self.last_play_start_sec)
        else:
            self.last_play_start_sec = self.player.position_sec()
            self.last_timer_call_time = self.get_sys_time()
            self._playhead_anim_last = self.last_timer_call_time
            self._sync_playhead_display(self.last_play_start_sec, force_full=True)
            self.player.play()
            self.btn_play.setIcon(FIF.PAUSE_BOLD)
            
    def stop_play(self):
        self.player.stop()
        self.btn_play.setIcon(FIF.PLAY_SOLID)
        if self.return_on_stop_check.isChecked():
            self.seek_audio(self.last_play_start_sec)
        else:
            self.seek_audio(0)
        self._sync_playhead_display(self.smooth_play_time, force_full=True)
            
    def change_speed(self, index):
        speeds = [0.25, 0.5, 1.0, 1.5, 2.0, 4.0]
        if 0 <= index < len(speeds):
            self.player.speed = speeds[index]
            
    def _sync_playhead_display(self, sec: float, force_full: bool = False):
        self.smooth_play_time = sec
        self._playhead_target_sec = sec
        self.timeline_widget.playhead_sec = sec
        self.playhead_overlay.set_playhead(sec, force_full=force_full)

    def seek_audio(self, sec):
        self.player.seek(sec)
        self.last_timer_call_time = self.get_sys_time()
        self._playhead_anim_last = self.last_timer_call_time
        self._sync_playhead_display(sec, force_full=True)

    def _update_playhead_animation(self):
        """高帧率插值，仅更新播放头叠加层"""
        if not self.player.playing:
            return

        now = self.get_sys_time()
        dt = now - self._playhead_anim_last
        self._playhead_anim_last = now
        if dt <= 0 or dt > 0.25:
            dt = 0.016

        speed = float(self.player.speed)
        target = self._playhead_target_sec

        # 按墙钟推进 + 指数平滑贴近音频时钟
        self.smooth_play_time += dt * speed
        diff = target - self.smooth_play_time
        if abs(diff) > 0.15:
            self.smooth_play_time = target
        elif abs(diff) > 0.0001:
            self.smooth_play_time += diff * min(1.0, dt * 20.0)

        duration = self.timeline_widget.duration
        if duration > 0:
            self.smooth_play_time = min(self.smooth_play_time, duration)

        self.timeline_widget.playhead_sec = self.smooth_play_time
        self.playhead_overlay.set_playhead(self.smooth_play_time)

    def update_playback_ui(self):
        if self.player.playing:
            current_sys_time = self.get_sys_time()
            self.last_timer_call_time = current_sys_time

            self._playhead_target_sec = self.player.position_sec()

            if not self.player.playing:
                self.btn_play.setIcon(FIF.PLAY_SOLID)

            if self.auto_scroll_check.isChecked():
                x = self.smooth_play_time * self.timeline_widget.zoom
                sb = self.scroll_area.horizontalScrollBar()
                viewport_width = self.scroll_area.viewport().width()
                
                # 如果播放头超过视野的 80%，向后滚动
                if x > sb.value() + viewport_width * 0.8:
                    sb.setValue(int(x - viewport_width * 0.2))
                # 如果播放头在视野之前（用户手动拖回了播放头），拉回视野
                elif x < sb.value():
                    sb.setValue(int(x - viewport_width * 0.2))
                    
    def on_zoom_changed(self, old_mouse_x, new_mouse_x):
        # old_mouse_x: 缩放前鼠标在 widget 上的位置
        # new_mouse_x: 缩放后同一时间点在 widget 上的新位置
        # 调整滚动位置，使鼠标下方的内容保持不动
        sb = self.scroll_area.horizontalScrollBar()
        
        # 计算鼠标在视口中的位置
        viewport_mouse_x = old_mouse_x - sb.value()
        # 新的滚动位置 = 新的 widget 位置 - 鼠标在视口中的位置
        new_scroll = int(new_mouse_x - viewport_mouse_x)
        # 限制在有效范围内
        new_scroll = max(0, min(sb.maximum(), new_scroll))
        sb.setValue(new_scroll)
        
        self.playhead_overlay.set_playhead(self.smooth_play_time, force_full=True)
        
    def on_lyric_changed(self):
        pass
        
    # --- 撤销/重做逻辑 ---
    def on_state_committed(self, old_state, new_state):
        self.undo_stack.append(old_state)
        self.redo_stack.clear()
        self.update_undo_redo_buttons()
        
    def _current_editor_state(self):
        tw = self.timeline_widget
        return clone_lyrics(tw.lyrics), clone_anchors(tw.filter_anchors)

    def _apply_editor_state(self, state):
        lyrics, anchors = clone_editor_state(state)
        self.timeline_widget.set_editor_state(lyrics, anchors)

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._current_editor_state())
        prev_state = self.undo_stack.pop()
        self._apply_editor_state(prev_state)
        self.update_undo_redo_buttons()
        
    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._current_editor_state())
        next_state = self.redo_stack.pop()
        self._apply_editor_state(next_state)
        self.update_undo_redo_buttons()
        
    def update_undo_redo_buttons(self):
        self.btn_undo.setEnabled(len(self.undo_stack) > 0)
        self.btn_redo.setEnabled(len(self.redo_stack) > 0)
        
    # --- 过滤锚点控制 ---
    def place_filter_start(self):
        self.timeline_widget.set_filter_start(self.smooth_play_time)
        
    def place_filter_end(self):
        self.timeline_widget.set_filter_end(self.smooth_play_time)

    def _merge_inserted_events_generic(self, events, threshold_sec: float = 0.35):
        """
        通用归并（非 KSC 细粒度时长场景）：
        - 新插入锚点按时间归并到所在句/最近句（阈值内）
        - 距离过远则保留为独立新句
        """
        import bisect
        import re

        if not events:
            return []

        base_rows = {}
        extras = []
        for ev in events:
            if isinstance(ev.source_line, int) and ev.source_line > 0:
                if ev.source_line not in base_rows:
                    base_rows[ev.source_line] = ev
            else:
                extras.append(ev)

        # 无原始句子，直接按时间返回
        if not base_rows:
            out = clone_lyrics(events)
            out.sort(key=lambda x: x.time_seconds)
            return out

        rows = sorted(base_rows.values(), key=lambda x: x.time_seconds)
        row_meta = []
        for i, row_ev in enumerate(rows):
            start = row_ev.time_seconds
            if i + 1 < len(rows):
                end = rows[i + 1].time_seconds
            else:
                end = start + 2.0
            row_meta.append({
                "ev": row_ev,
                "start": start,
                "end": end,
                "inserts": [],
            })

        # 分配插入点
        for ex in sorted(extras, key=lambda x: x.time_seconds):
            if not ex.text:
                continue
            best = None
            best_dist = float("inf")
            for row in row_meta:
                if row["start"] <= ex.time_seconds <= row["end"]:
                    best = row
                    best_dist = 0.0
                    break
                if ex.time_seconds < row["start"]:
                    dist = row["start"] - ex.time_seconds
                else:
                    dist = ex.time_seconds - row["end"]
                if dist < best_dist:
                    best = row
                    best_dist = dist
            if best is not None and best_dist <= threshold_sec:
                best["inserts"].append(ex)

        def split_units(text: str):
            if " " in text.strip():
                return [u for u in text.split(" ") if u != ""], "space"
            return list(text), "char"

        def is_ascii_word(token: str) -> bool:
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-']*", token.strip()))

        def render_units(units, style: str):
            if style == "space":
                return " ".join(units)
            # 非空格模式下，保证英文单词之间有空格，其他字符保持原有连写习惯
            out = []
            prev_word = False
            for u in units:
                token = u.strip()
                if not token:
                    continue
                curr_word = is_ascii_word(token)
                if out and prev_word and curr_word:
                    out.append(" ")
                out.append(token)
                prev_word = curr_word
            return "".join(out)

        # 复制基础事件并执行归并文本
        merged_base = []
        for row in row_meta:
            ev = row["ev"]
            inserts = sorted(row["inserts"], key=lambda x: x.time_seconds)
            merged_ev = LyricEvent(time_seconds=ev.time_seconds, text=ev.text, source_line=ev.source_line)
            if inserts:
                units, style = split_units(ev.text)
                n = len(units)
                start = row["start"]
                end = row["end"]
                span = max(0.001, end - start)
                token_starts = [start + i * (span / max(1, n)) for i in range(n)]

                slot_map = {}
                for ins in inserts:
                    slot = bisect.bisect_left(token_starts, ins.time_seconds) if token_starts else 0
                    slot_map.setdefault(slot, []).append(ins.text)

                new_units = []
                for slot in range(n + 1):
                    for t in slot_map.get(slot, []):
                        new_units.append(t)
                    if slot < n:
                        new_units.append(units[slot])
                merged_ev.text = render_units(new_units, style)
            merged_base.append(merged_ev)

        # 保留远距插入点为独立新句
        merged_ids = set()
        for row in row_meta:
            for ins in row["inserts"]:
                merged_ids.add(id(ins))
        standalone = []
        for ex in extras:
            if id(ex) not in merged_ids and ex.text:
                standalone.append(LyricEvent(time_seconds=ex.time_seconds, text=ex.text, source_line=-1))

        out = merged_base + standalone
        out.sort(key=lambda x: x.time_seconds)
        return out

    def insert_lyric_anchor(self):
        """在播放头位置插入一个空白歌词锚点"""
        tw = self.timeline_widget
        if tw.duration <= 0:
            return
        old_state = (clone_lyrics(tw.lyrics), clone_anchors(tw.filter_anchors))
        sec = max(0.0, min(tw.duration, self.smooth_play_time))
        tw.lyrics.append(LyricEvent(time_seconds=sec, text="", source_line=-1))
        tw.lyrics.sort(key=lambda x: x.time_seconds)
        tw.lyric_changed.emit()
        tw.state_committed.emit(old_state, (clone_lyrics(tw.lyrics), clone_anchors(tw.filter_anchors)))
        tw.update()
        
    def save_lyrics(self):
        original_events = self.timeline_widget.lyrics
        if not original_events: return
        
        # 构建过滤锚点过滤逻辑
        sorted_anchors = sorted(self.timeline_widget.filter_anchors, key=lambda x: x.time_seconds)
        intervals = []
        active_start = None
        for a in sorted_anchors:
            if a.is_start:
                if active_start is None:
                    active_start = a.time_seconds
            else:
                if active_start is not None:
                    intervals.append((active_start, a.time_seconds))
                    active_start = None
        if active_start is not None:
            intervals.append((active_start, float('inf')))
        
        # 复制事件并可能包含过滤标记
        updated_events = clone_lyrics(original_events)
        
        # 检查是否需要添加(过滤)标记
        for ev in updated_events:
            text = ev.text
            in_interval = False
            for s_sec, e_sec in intervals:
                if s_sec <= ev.time_seconds <= e_sec:
                    in_interval = True
                    break
                    
            if in_interval:
                if "（过滤）" not in text and "(过滤)" not in text:
                    ev.text = f"（过滤）" + text
            else:
                ev.text = text.replace("（过滤）", "").replace("(过滤)", "")
            
        base, ext = os.path.splitext(self.lyric_path)
        format_idx = self.export_format_combo.currentIndex()
        
        # 决定默认扩展名和文件类型过滤
        default_ext = ext
        filter_str = "All Files (*.*)"
        if format_idx == 1:
            default_ext = ".lrc"
            filter_str = "LRC Files (*.lrc)"
        elif format_idx == 2:
            default_ext = ".txt"
            filter_str = "Text Files (*.txt)"
        elif format_idx == 3:
            default_ext = ".ksc"
            filter_str = "KBuilder Scripts (*.ksc)"
        elif format_idx == 4:
            default_ext = ".csv"
            filter_str = "CSV Files (*.csv)"
        elif format_idx == 0:
            if ext.lower() == ".lrc": filter_str = "LRC Files (*.lrc)"
            elif ext.lower() == ".csv": filter_str = "CSV Files (*.csv)"
            elif ext.lower() == ".txt": filter_str = "Text Files (*.txt)"
            elif ext.lower() == ".ksc": filter_str = "KBuilder Scripts (*.ksc)"
            
        default_save_path = f"{base}_calibrated{default_ext}"
        
        out_path, _ = QFileDialog.getSaveFileName(
            self, 
            "另存为校准后的歌词", 
            default_save_path, 
            filter_str
        )
        
        if not out_path:
            return # 用户取消保存
        
        try:
            # 按时间排序以防拖拽导致乱序
            updated_events.sort(key=lambda x: x.time_seconds)
            # 通用归并（全格式）：将新插入锚点并入分句；远距保留独立句
            merge_threshold_sec = self.merge_threshold_spin.value() / 1000.0
            merged_events = self._merge_inserted_events_generic(updated_events, threshold_sec=merge_threshold_sec)
            
            # 源格式 (智能判断当前 ext) 或明确选择了格式
            target_ext = os.path.splitext(out_path)[1].lower()
            
            # 当选择源格式，或者目标后缀匹配原后缀时，采用智能替换模式
            # 注意：传入源文件扩展名 ext.lower()，以正确解析原始格式并替换时间戳
            if format_idx == 0 or target_ext == ext.lower():
                # 源格式保存交由 _save_as_source_format 内部分流：
                # - KSC: 使用原始插入事件做句内归并+毫秒数组重写
                # - 非 KSC: 走通用归并
                self._save_as_source_format(self.lyric_path, out_path, updated_events, ext.lower(), merge_threshold_sec)
                InfoBar.success("保存成功", f"已成功导出到:\n{out_path}", parent=self)
                return
                
            with open(out_path, "w", encoding="utf-8-sig") as f:
                if target_ext == ".lrc":
                    for ev in merged_events:
                        minutes = int(ev.time_seconds // 60)
                        seconds = ev.time_seconds % 60
                        f.write(f"[{minutes:02d}:{seconds:05.2f}]{ev.text}\n")
                        
                elif target_ext == ".ksc":
                    f.write("karaoke.add('00:00.000', '00:00.000', '*', '');\n")
                    for i, ev in enumerate(merged_events):
                        minutes = int(ev.time_seconds // 60)
                        seconds = ev.time_seconds % 60
                        time_str = f"{minutes:02d}:{seconds:06.3f}"
                        # 简单估算结束时间为下一个词的开始，或当前+2秒
                        end_time_sec = merged_events[i+1].time_seconds if i+1 < len(merged_events) else ev.time_seconds + 2.0
                        e_min = int(end_time_sec // 60)
                        e_sec = end_time_sec % 60
                        end_str = f"{e_min:02d}:{e_sec:06.3f}"
                        f.write(f"karaoke.add('{time_str}', '{end_str}', '{ev.text}', '{ev.text}');\n")
                        
                elif target_ext == ".csv":
                    f.write("Start Time,End Time,Text\n")
                    for i, ev in enumerate(merged_events):
                        end_time_sec = merged_events[i+1].time_seconds if i+1 < len(merged_events) else ev.time_seconds + 2.0
                        f.write(f"{ev.time_seconds:.3f},{end_time_sec:.3f},{ev.text}\n")
                        
                else: # 默认 txt 或其他格式
                    for ev in merged_events:
                        f.write(f"{ev.time_seconds:.3f}\t{ev.text}\n")
                    
            InfoBar.success("保存成功", f"已成功导出到:\n{out_path}", parent=self)
        except Exception as e:
            InfoBar.error("保存失败", str(e), parent=self)

    def _save_as_source_format(self, in_path, out_path, updated_events, ext, merge_threshold_sec: float = 0.35):
        import re
        import bisect
        with open(in_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        
        # 智能检测实际格式（扩展名可能不准确，如 .txt 实际是 KSC 格式）
        content_sample = "".join(lines[:30]).lower()
        detected_format = ext
        if "karaoke.add" in content_sample or "karaoke.add" in content_sample:
            detected_format = ".ksc"
        elif re.search(r"\[\d+:\d+[.,]\d+\]", content_sample):
            detected_format = ".lrc"

        # 非 KSC 源格式：先做通用并句归并
        if detected_format != ".ksc":
            updated_events = self._merge_inserted_events_generic(
                updated_events,
                threshold_sec=max(0.01, float(merge_threshold_sec))
            )
            
        def parse_ksc_line(raw_line: str):
            m = re.search(
                r"^\s*karaoke\.add\(\s*['\"](?P<start>.*?)['\"]\s*,\s*['\"](?P<end>.*?)['\"]\s*,\s*['\"](?P<text>.*?)['\"]\s*,\s*['\"](?P<dur>.*?)['\"]\s*\)\s*;?",
                raw_line.strip(),
                re.IGNORECASE
            )
            if not m:
                return None
            return {
                "start": m.group("start"),
                "end": m.group("end"),
                "text": m.group("text"),
                "dur": m.group("dur"),
            }

        def detect_ksc_non_english_bracket_style(all_lines):
            """
            检测源 KSC 是否对非英文单元使用中括号。
            规则：
            - 若发现 [中/日/韩等] 这样的 bracket 单元，则认为启用非英文 bracket 风格；
            - 否则仅英文单词使用 bracket。
            """
            for raw in all_lines:
                parsed = parse_ksc_line(raw)
                if not parsed:
                    continue
                for token in re.findall(r"\[([^\]]*)\]", parsed["text"]):
                    base = token.strip()
                    if base and not is_ascii_word(base):
                        return True
            return False

        def parse_units(text_param: str):
            bracket_units = re.findall(r"\[([^\]]*)\]", text_param)
            if bracket_units:
                return bracket_units, "bracket"
            if " " in text_param.strip():
                return [u for u in text_param.split(" ") if u != ""], "space"
            return list(text_param), "plain"

        def is_ascii_word(token: str) -> bool:
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-']*", token.strip()))

        ksc_non_english_use_brackets = detect_ksc_non_english_bracket_style(lines) if detected_format == ".ksc" else False

        def normalize_ksc_unit(unit_text: str, is_last: bool):
            """
            KSC 单元格式：
            - 英文单词在句首/句中：末尾带空格（如 TEST -> 'TEST '）
            - 英文单词在句尾或单独成句：不带空格（如 TEST）
            """
            u = unit_text if unit_text is not None else ""
            base = u.strip()
            if is_ascii_word(base):
                return base if is_last else (base + " ")
            return u

        def render_units(units, style: str):
            out = []
            total = len(units)
            for idx, u in enumerate(units):
                raw = u if u is not None else ""
                token = raw.strip()
                if not token:
                    continue
                if is_ascii_word(token):
                    out.append(f"[{normalize_ksc_unit(token, is_last=(idx == total - 1))}]")
                else:
                    if ksc_non_english_use_brackets:
                        out.append(f"[{token}]")
                    else:
                        out.append(token)
            return "".join(out)

        def parse_duration_list(dur_param: str):
            vals = []
            for s in dur_param.split(","):
                s = s.strip()
                if s.isdigit():
                    vals.append(int(s))
            return vals

        def fmt_mmssmmm(sec: float):
            m = int(sec // 60)
            s = sec % 60
            return f"{m:02d}:{s:06.3f}"

        def rebuild_ksc_line(raw_line: str, start_sec: float, end_sec: float, text_param: str, durations):
            line_prefix = re.match(r"^(\s*)", raw_line).group(1) if raw_line else ""
            dur_text = ",".join(str(int(max(1, d))) for d in durations)
            return (
                f"{line_prefix}karaoke.add('{fmt_mmssmmm(start_sec)}', '{fmt_mmssmmm(end_sec)}', "
                f"'{text_param}', '{dur_text}');\n"
            )

        # 根据 source_line 进行精准替换
        event_dict = {}
        extra_events = []
        for ev in updated_events:
            # 记录每一行对应的最新事件列表
            if isinstance(ev.source_line, int) and ev.source_line > 0:
                if ev.source_line not in event_dict:
                    event_dict[ev.source_line] = []
                event_dict[ev.source_line].append(ev)
            else:
                # 新插入锚点等无原始行号的事件，追加写入
                extra_events.append(ev)

        # --- KSC：将新增锚点按时间归并到最近句/所在句 ---
        ksc_line_overrides = {}
        standalone_ksc_events = []
        if detected_format == ".ksc" and extra_events:
            MERGE_NEAR_THRESHOLD_SEC = max(0.01, float(merge_threshold_sec))
            DEFAULT_INSERT_DUR_MS = 150
            MIN_DUR_MS = 40

            row_meta = []
            for line_no, ev_list in event_dict.items():
                if not ev_list:
                    continue
                if line_no <= 0 or line_no > len(lines):
                    continue
                parsed = parse_ksc_line(lines[line_no - 1])
                if not parsed:
                    continue

                ev = ev_list[0]
                units, unit_style = parse_units(parsed["text"])
                orig_durs = parse_duration_list(parsed["dur"])
                if not orig_durs:
                    orig_durs = [DEFAULT_INSERT_DUR_MS] * max(1, len(units))
                if len(units) == 0:
                    units = ["" for _ in orig_durs]
                elif len(units) < len(orig_durs):
                    units.extend([""] * (len(orig_durs) - len(units)))
                elif len(units) > len(orig_durs):
                    orig_durs.extend([DEFAULT_INSERT_DUR_MS] * (len(units) - len(orig_durs)))

                start_sec = ev.time_seconds
                end_sec = start_sec + sum(orig_durs) / 1000.0
                token_starts = []
                cur = start_sec
                for d in orig_durs:
                    token_starts.append(cur)
                    cur += d / 1000.0

                row_meta.append({
                    "line_no": line_no,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "units": units,
                    "style": unit_style,
                    "orig_durs": orig_durs,
                    "token_starts": token_starts,
                    "inserts": [],
                })

            # 分配新增锚点到句子：优先落在句区间内，否则按最近距离+阈值
            for ex in sorted(extra_events, key=lambda x: x.time_seconds):
                if not ex.text:
                    continue
                best_row = None
                best_dist = float("inf")
                for row in row_meta:
                    if row["start_sec"] <= ex.time_seconds <= row["end_sec"]:
                        best_row = row
                        best_dist = 0.0
                        break
                    if ex.time_seconds < row["start_sec"]:
                        dist = row["start_sec"] - ex.time_seconds
                    else:
                        dist = ex.time_seconds - row["end_sec"]
                    if dist < best_dist:
                        best_dist = dist
                        best_row = row

                if best_row is not None and best_dist <= MERGE_NEAR_THRESHOLD_SEC:
                    best_row["inserts"].append(ex)
                else:
                    standalone_ksc_events.append(ex)

            # 对每个句子执行归并并重写 text / durations / end
            for row in row_meta:
                inserts = sorted(row["inserts"], key=lambda x: x.time_seconds)
                if not inserts:
                    continue

                orig_units = list(row["units"])
                orig_durs = list(row["orig_durs"])
                token_starts = list(row["token_starts"])

                # 先为每个插入点分配时长（优先用相邻插入点时间差）
                insert_infos = []
                for i, ins in enumerate(inserts):
                    if i + 1 < len(inserts):
                        gap_ms = int(round((inserts[i + 1].time_seconds - ins.time_seconds) * 1000))
                        dur_ms = max(MIN_DUR_MS, gap_ms)
                    else:
                        dur_ms = DEFAULT_INSERT_DUR_MS
                    insert_infos.append((ins, dur_ms))

                # 将插入按“原 token 起始时间”的插槽归类（保持原有 token 时长不变）
                slot_map = {}
                for ins, dur_ms in insert_infos:
                    slot = bisect.bisect_left(token_starts, ins.time_seconds)
                    slot_map.setdefault(slot, []).append((ins.time_seconds, ins.text, dur_ms))
                for slot in slot_map:
                    slot_map[slot].sort(key=lambda x: x[0])

                new_units = []
                new_durs = []
                for slot in range(len(orig_units) + 1):
                    for _t, txt, dur_ms in slot_map.get(slot, []):
                        new_units.append(txt)
                        new_durs.append(max(MIN_DUR_MS, int(dur_ms)))
                    if slot < len(orig_units):
                        new_units.append(orig_units[slot])
                        new_durs.append(max(MIN_DUR_MS, int(orig_durs[slot])))

                new_end_sec = row["start_sec"] + sum(new_durs) / 1000.0
                new_text_param = render_units(new_units, row["style"])

                ksc_line_overrides[row["line_no"]] = {
                    "start_sec": row["start_sec"],
                    "end_sec": new_end_sec,
                    "text_param": new_text_param,
                    "durations": new_durs,
                }
            
        if detected_format == ".ksc":
            append_events = sorted([e for e in standalone_ksc_events if e.text], key=lambda x: x.time_seconds)
        else:
            append_events = sorted([e for e in extra_events if e.text], key=lambda x: x.time_seconds)

        def parse_line_time(raw_line: str):
            if detected_format == ".ksc":
                parsed = parse_ksc_line(raw_line)
                if parsed:
                    try:
                        return parse_time_to_seconds(parsed["start"])
                    except Exception:
                        return None
                return None
            if detected_format == ".lrc":
                m = re.search(r"\[(\d+:\d+(?:[.,]\d+)?)\]", raw_line)
                if m:
                    try:
                        return parse_time_to_seconds(m.group(1).replace(",", "."))
                    except Exception:
                        return None
                return None
            if detected_format == ".csv":
                head = raw_line.strip().split(",")[0] if raw_line.strip() else ""
                try:
                    return parse_time_to_seconds(head)
                except Exception:
                    return None
            m = re.match(r"^\s*((?:\d+:)?\d+[:.]\d+)", raw_line)
            if m:
                try:
                    return parse_time_to_seconds(m.group(1).replace(",", "."))
                except Exception:
                    return None
            return None

        def write_insert_event(file_obj, ev, upper_bound_sec=None, next_insert_sec=None):
            minutes = int(ev.time_seconds // 60)
            seconds = ev.time_seconds % 60
            if detected_format == ".lrc":
                file_obj.write(f"[{minutes:02d}:{seconds:05.2f}]{ev.text}\n")
            elif detected_format == ".ksc":
                end_time_sec = ev.time_seconds + 0.5
                if next_insert_sec is not None:
                    end_time_sec = min(end_time_sec, next_insert_sec)
                if upper_bound_sec is not None:
                    end_time_sec = min(end_time_sec, upper_bound_sec)
                e_min = int(end_time_sec // 60)
                e_sec = end_time_sec % 60
                token_raw = (ev.text or "").strip()
                if is_ascii_word(token_raw):
                    text_param = f"[{normalize_ksc_unit(token_raw, is_last=True)}]"
                else:
                    text_param = f"[{token_raw}]" if ksc_non_english_use_brackets else token_raw
                file_obj.write(
                    f"karaoke.add('{minutes:02d}:{seconds:06.3f}', "
                    f"'{e_min:02d}:{e_sec:06.3f}', '{text_param}', '500');\n"
                )
            else:
                file_obj.write(f"{ev.time_seconds:.3f}\t{ev.text}\n")

        insert_ptr = 0

        with open(out_path, "w", encoding="utf-8-sig") as f:
            for i, line in enumerate(lines, start=1):
                # 当前行时间（用于把独立新句插入到正确行位置）
                if i in event_dict and event_dict[i]:
                    current_line_time = event_dict[i][0].time_seconds
                else:
                    current_line_time = parse_line_time(line)

                # 在写当前行前，先写入所有时间更早的独立新句
                if current_line_time is not None:
                    while insert_ptr < len(append_events) and append_events[insert_ptr].time_seconds < current_line_time:
                        next_insert_sec = append_events[insert_ptr + 1].time_seconds if insert_ptr + 1 < len(append_events) else None
                        write_insert_event(
                            f,
                            append_events[insert_ptr],
                            upper_bound_sec=current_line_time,
                            next_insert_sec=next_insert_sec
                        )
                        insert_ptr += 1

                if i not in event_dict:
                    f.write(line)
                    continue
                    
                ev_list = event_dict[i]
                ev = ev_list[0] # 取这行第一个事件的时间作为主要时间
                minutes = int(ev.time_seconds // 60)
                seconds = ev.time_seconds % 60
                
                # 首先，处理歌词文本的（过滤）标记
                current_line = line
                expected_text = ev.text
                has_filter_marker = expected_text.startswith("（过滤）") or expected_text.startswith("(过滤)")
                
                # LRC 格式行内替换（时间+文本）
                if detected_format == ".lrc":
                    f.write(f"[{minutes:02d}:{seconds:05.2f}]{ev.text}\n")
                    
                # CSV 格式（时间+文本）
                elif detected_format == ".csv":
                    end_time_sec = ev.time_seconds + 2.0
                    if i + 1 in event_dict:
                        end_time_sec = event_dict[i+1][0].time_seconds
                    f.write(f"{ev.time_seconds:.3f},{end_time_sec:.3f},{ev.text}\n")
                    
                # KSC/小灰熊格式替换
                elif detected_format == ".ksc":
                    # 若该行有归并结果，直接重写整句（起始、结束、文本、毫秒数组）
                    if i in ksc_line_overrides:
                        ov = ksc_line_overrides[i]
                        new_line = rebuild_ksc_line(
                            current_line,
                            ov["start_sec"],
                            ov["end_sec"],
                            ov["text_param"],
                            ov["durations"]
                        )
                        f.write(new_line)
                        continue

                    # 从原始行提取第四个参数（每个字的毫秒数），用于计算正确的结束时间
                    # 格式: karaoke.add('起始时间', '结束时间', '[字1][字2]...', '毫秒1,毫秒2,...');
                    duration_match = re.search(r"karaoke\.add\s*\([^,]+,[^,]+,[^,]+,\s*['\"]([^'\"]+)['\"]", current_line, re.IGNORECASE)
                    total_duration_ms = 0
                    if duration_match:
                        durations_str = duration_match.group(1)
                        try:
                            durations = [int(d.strip()) for d in durations_str.split(',') if d.strip().isdigit()]
                            total_duration_ms = sum(durations)
                        except:
                            pass
                    
                    # 新起始时间
                    time_str = f"{minutes:02d}:{seconds:06.3f}"
                    
                    # 结束时间 = 起始时间 + 总毫秒数
                    if total_duration_ms > 0:
                        end_time_sec = ev.time_seconds + total_duration_ms / 1000.0
                    else:
                        # 如果无法提取毫秒数，回退到下一句起始时间或+2秒
                        end_time_sec = ev.time_seconds + 2.0
                        if i + 1 in event_dict:
                            end_time_sec = event_dict[i+1][0].time_seconds
                    
                    e_min = int(end_time_sec // 60)
                    e_sec = end_time_sec % 60
                    end_str = f"{e_min:02d}:{e_sec:06.3f}"
                    
                    # 使用正则替换 karaoke.add(...) 里面的前两个时间参数
                    pattern = r"(karaoke\.add\(\s*['\"])(.*?)(['\"]\s*,\s*['\"])(.*?)(['\"]\s*,)"
                    new_line = re.sub(pattern, rf"\g<1>{time_str}\g<3>{end_str}\g<5>", current_line, count=1, flags=re.IGNORECASE)
                    
                    f.write(new_line)
                    
                else:
                    # 普通文本：统一写为 时间 + 文本
                    f.write(f"{ev.time_seconds:.3f}\t{ev.text}\n")

            # 写入剩余独立新句（时间在最后一行之后）
            while insert_ptr < len(append_events):
                next_insert_sec = append_events[insert_ptr + 1].time_seconds if insert_ptr + 1 < len(append_events) else None
                write_insert_event(f, append_events[insert_ptr], upper_bound_sec=None, next_insert_sec=next_insert_sec)
                insert_ptr += 1
