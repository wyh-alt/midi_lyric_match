import os
import sys
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import csv
import time

# 尝试导入必要的库
try:
    import mido
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError as e:
    # 如果导入失败，显示错误消息
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("导入错误", f"缺少必要的库: {e}\n\n请运行以下命令安装依赖:\npip install mido matplotlib numpy")
    sys.exit(1)

class MidiBatchProcessor:
    def __init__(self, root):
        self.root = root
        self.root.title("MIDI文件批量处理工具")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)
        
        # 设置样式
        self.style = ttk.Style()
        self.style.configure("TButton", font=("微软雅黑", 10))
        self.style.configure("TLabel", font=("微软雅黑", 10))
        
        # 创建主框架
        self.main_frame = ttk.Frame(self.root, padding="10")
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建界面组件
        self.create_widgets()
        
        # 状态变量
        self.processing = False
        self.midi_files = []
        self.output_dir = ""

    def create_widgets(self):
        # 输入文件选择部分
        input_frame = ttk.LabelFrame(self.main_frame, text="MIDI文件选择", padding="10")
        input_frame.pack(fill=tk.X, pady=10)
        
        # 添加文件按钮
        self.add_files_button = ttk.Button(input_frame, text="添加MIDI文件", command=self.add_files)
        self.add_files_button.pack(side=tk.LEFT, padx=5)
        
        # 添加文件夹按钮
        self.add_folder_button = ttk.Button(input_frame, text="添加文件夹", command=self.add_folder)
        self.add_folder_button.pack(side=tk.LEFT, padx=5)
        
        # 清空列表按钮
        self.clear_button = ttk.Button(input_frame, text="清空列表", command=self.clear_files)
        self.clear_button.pack(side=tk.LEFT, padx=5)
        
        # 文件列表
        list_frame = ttk.LabelFrame(self.main_frame, text="待处理文件列表", padding="10")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        # 创建文件列表控件
        self.file_listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, font=("Courier New", 10))
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # 输出选项
        output_frame = ttk.LabelFrame(self.main_frame, text="输出选项", padding="10")
        output_frame.pack(fill=tk.X, pady=10)
        
        # 输出目录选择
        ttk.Label(output_frame, text="输出目录:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.output_dir_var = tk.StringVar()
        self.output_dir_entry = ttk.Entry(output_frame, textvariable=self.output_dir_var, width=50)
        self.output_dir_entry.grid(row=0, column=1, sticky=tk.W+tk.E, padx=5, pady=5)
        self.browse_output_button = ttk.Button(output_frame, text="浏览...", command=self.browse_output_dir)
        self.browse_output_button.grid(row=0, column=2, padx=5, pady=5)
        
        # 输出格式选项
        ttk.Label(output_frame, text="输出格式:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.output_format_var = tk.StringVar(value="txt")
        format_frame = ttk.Frame(output_frame)
        format_frame.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)
        ttk.Radiobutton(format_frame, text="TXT文本文件", variable=self.output_format_var, value="txt").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(format_frame, text="CSV表格文件", variable=self.output_format_var, value="csv").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(format_frame, text="两种格式都输出", variable=self.output_format_var, value="both").pack(side=tk.LEFT, padx=5)
        
        # 处理选项
        process_frame = ttk.LabelFrame(self.main_frame, text="处理选项", padding="10")
        process_frame.pack(fill=tk.X, pady=10)
        
        # 处理内容选择
        ttk.Label(process_frame, text="提取内容:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        options_frame = ttk.Frame(process_frame)
        options_frame.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        
        self.extract_notes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="音符信息", variable=self.extract_notes_var).pack(side=tk.LEFT, padx=5)
        
        self.extract_lyrics_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="歌词信息", variable=self.extract_lyrics_var).pack(side=tk.LEFT, padx=5)
        
        self.extract_text_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="文本事件", variable=self.extract_text_var).pack(side=tk.LEFT, padx=5)
        
        # 处理按钮
        button_frame = ttk.Frame(self.main_frame)
        button_frame.pack(fill=tk.X, pady=10)
        
        self.process_button = ttk.Button(button_frame, text="开始处理", command=self.start_processing)
        self.process_button.pack(side=tk.RIGHT, padx=5)
        
        # 进度条和状态
        progress_frame = ttk.Frame(self.main_frame)
        progress_frame.pack(fill=tk.X, pady=5)
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, padx=5, pady=5)
        
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, anchor=tk.W)
        self.status_label.pack(fill=tk.X, padx=5)

    def add_files(self):
        """添加MIDI文件到列表"""
        files = filedialog.askopenfilenames(
            title="选择MIDI文件",
            filetypes=[("MIDI文件", "*.mid *.midi"), ("所有文件", "*.*")]
        )
        
        if files:
            for file in files:
                if file not in self.midi_files:
                    self.midi_files.append(file)
                    self.file_listbox.insert(tk.END, os.path.basename(file))
            
            self.status_var.set(f"已添加 {len(files)} 个文件，共 {len(self.midi_files)} 个文件")

    def add_folder(self):
        """添加文件夹中的所有MIDI文件"""
        folder = filedialog.askdirectory(title="选择包含MIDI文件的文件夹")
        
        if folder:
            count = 0
            for root, _, files in os.walk(folder):
                for file in files:
                    if file.lower().endswith(('.mid', '.midi')):
                        file_path = os.path.join(root, file)
                        if file_path not in self.midi_files:
                            self.midi_files.append(file_path)
                            self.file_listbox.insert(tk.END, file)
                            count += 1
            
            self.status_var.set(f"从文件夹添加了 {count} 个MIDI文件，共 {len(self.midi_files)} 个文件")

    def clear_files(self):
        """清空文件列表"""
        self.midi_files = []
        self.file_listbox.delete(0, tk.END)
        self.status_var.set("文件列表已清空")

    def browse_output_dir(self):
        """选择输出目录"""
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.output_dir_var.set(directory)
            self.output_dir = directory

    def start_processing(self):
        """开始批量处理MIDI文件"""
        if not self.midi_files:
            messagebox.showwarning("警告", "请先添加MIDI文件")
            return
        
        output_dir = self.output_dir_var.get()
        if not output_dir:
            messagebox.showwarning("警告", "请选择输出目录")
            return
        
        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except Exception as e:
                messagebox.showerror("错误", f"创建输出目录失败: {str(e)}")
                return
        
        # 禁用界面控件
        self.set_controls_state(tk.DISABLED)
        
        # 重置进度条
        self.progress_var.set(0)
        
        # 在单独的线程中运行处理
        self.processing = True
        threading.Thread(target=self._process_files, daemon=True).start()

    def _process_files(self):
        """在后台线程中处理文件"""
        total_files = len(self.midi_files)
        processed = 0
        errors = []
        
        start_time = time.time()
        
        try:
            for file_path in self.midi_files:
                if not self.processing:  # 检查是否应该停止处理
                    break
                
                try:
                    # 更新状态
                    file_name = os.path.basename(file_path)
                    self.root.after(0, lambda msg=f"正在处理: {file_name}": self.status_var.set(msg))
                    
                    # 分析MIDI文件
                    notes, lyrics, text_events = self.analyze_midi_file(file_path)
                    
                    # 保存结果
                    self.save_results(file_path, notes, lyrics, text_events)
                    
                except Exception as e:
                    error_msg = f"处理 {os.path.basename(file_path)} 时出错: {str(e)}"
                    errors.append(error_msg)
                    print(error_msg)
                    traceback.print_exc()
                
                # 更新进度
                processed += 1
                progress = (processed / total_files) * 100
                self.root.after(0, lambda p=progress: self.progress_var.set(p))
            
            # 处理完成
            elapsed_time = time.time() - start_time
            if errors:
                final_msg = f"处理完成，有 {len(errors)} 个错误。用时: {elapsed_time:.1f} 秒"
                self.root.after(0, lambda: messagebox.showwarning("处理完成但有错误", 
                                                                 f"成功处理了 {processed - len(errors)}/{total_files} 个文件。\n\n错误详情请查看控制台输出。"))
            else:
                final_msg = f"全部处理完成！共处理 {processed} 个文件。用时: {elapsed_time:.1f} 秒"
                self.root.after(0, lambda: messagebox.showinfo("处理完成", final_msg))
            
            self.root.after(0, lambda msg=final_msg: self.status_var.set(msg))
            
        except Exception as e:
            error_msg = f"批处理过程中出错: {str(e)}"
            self.root.after(0, lambda: messagebox.showerror("错误", error_msg))
            self.root.after(0, lambda msg=error_msg: self.status_var.set(msg))
            traceback.print_exc()
        
        finally:
            # 重新启用界面控件
            self.root.after(0, lambda: self.set_controls_state(tk.NORMAL))
            self.processing = False

    def set_controls_state(self, state):
        """设置界面控件的启用/禁用状态"""
        self.add_files_button["state"] = state
        self.add_folder_button["state"] = state
        self.clear_button["state"] = state
        self.browse_output_button["state"] = state
        self.process_button["state"] = state
        self.file_listbox["state"] = state

    def analyze_midi_file(self, midi_file_path):
        """分析MIDI文件，提取音符开始时间点和歌词信息"""
        midi_file = mido.MidiFile(midi_file_path)
        
        # 存储结果
        notes = []
        lyrics = []
        text_events = []
        
        # 遍历所有轨道
        for i, track in enumerate(midi_file.tracks):
            track_time = 0
            track_notes = []
            track_lyrics = []
            track_text_events = []
            
            # 遍历轨道中的所有消息
            for msg in track:
                track_time += msg.time
                
                # 检查音符开始事件
                if msg.type == 'note_on' and msg.velocity > 0 and self.extract_notes_var.get():
                    note_info = {
                        'track': i,
                        'time': track_time,
                        'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                        'note': msg.note,
                        'velocity': msg.velocity,
                        'channel': msg.channel
                    }
                    note_info['time_ms'] = int(note_info['time_seconds'] * 1000)
                    track_notes.append(note_info)
                
                # 检查歌词事件
                elif msg.type == 'lyrics' and self.extract_lyrics_var.get():
                    lyric_info = {
                        'track': i,
                        'time': track_time,
                        'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                        'text': msg.text,
                        'type': 'lyrics'
                    }
                    lyric_info['time_ms'] = int(lyric_info['time_seconds'] * 1000)
                    track_lyrics.append(lyric_info)
                
                # 检查文本事件（有些MIDI文件使用文本事件存储歌词）
                elif msg.type == 'text' and self.extract_text_var.get():
                    text_info = {
                        'track': i,
                        'time': track_time,
                        'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, self.get_tempo(midi_file)),
                        'text': msg.text,
                        'type': 'text'
                    }
                    text_info['time_ms'] = int(text_info['time_seconds'] * 1000)
                    track_text_events.append(text_info)
            
            notes.extend(track_notes)
            lyrics.extend(track_lyrics)
            text_events.extend(track_text_events)
        
        # 按时间排序
        notes.sort(key=lambda x: x['time'])
        lyrics.sort(key=lambda x: x['time'])
        text_events.sort(key=lambda x: x['time'])
        
        return notes, lyrics, text_events

    def get_tempo(self, midi_file):
        """获取MIDI文件的默认速度"""
        # 默认速度 (BPM = 120)
        default_tempo = 500000
        
        # 尝试从第一个轨道找到速度信息
        for track in midi_file.tracks:
            for msg in track:
                if msg.type == 'set_tempo':
                    return msg.tempo
        
        return default_tempo

    def save_results(self, midi_file_path, notes, lyrics, text_events):
        """保存分析结果到文件"""
        output_dir = self.output_dir_var.get()
        output_format = self.output_format_var.get()
        
        # 获取文件名（不含扩展名）
        file_name = os.path.splitext(os.path.basename(midi_file_path))[0]
        
        # 合并歌词和文本事件
        combined_events = lyrics + text_events
        combined_events.sort(key=lambda x: x['time_seconds'])
        
        # 根据选择的格式保存
        if output_format in ["txt", "both"]:
            self.save_as_txt(output_dir, file_name, notes, lyrics, text_events, combined_events)
        
        if output_format in ["csv", "both"]:
            self.save_as_csv(output_dir, file_name, notes, lyrics, text_events, combined_events)

    def save_as_txt(self, output_dir, file_name, notes, lyrics, text_events, combined_events):
        """保存为TXT文本文件"""
        output_file = os.path.join(output_dir, f"{file_name}_analysis.txt")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            # 写入文件头
            f.write(f"MIDI文件分析结果: {file_name}\n")
            f.write("=" * 80 + "\n\n")
            
            # 写入音符信息
            if self.extract_notes_var.get() and notes:
                f.write("音符信息:\n")
                f.write("-" * 80 + "\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'时间(毫秒)':<12} {'音符':<6} {'力度':<6} {'通道':<6}\n")
                f.write("-" * 80 + "\n")
                
                for note in notes:
                    f.write(f"{note['track']:<6} {note['time']:<12} {note['time_seconds']:<12.2f} {note['time_ms']:<12} {note['note']:<6} {note['velocity']:<6} {note['channel']:<6}\n")
                
                f.write("\n\n")
            
            # 写入歌词信息
            if self.extract_lyrics_var.get() and lyrics:
                f.write("歌词信息 (lyrics事件):\n")
                f.write("-" * 80 + "\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'时间(毫秒)':<12} {'歌词'}\n")
                f.write("-" * 80 + "\n")
                
                for lyric in lyrics:
                    f.write(f"{lyric['track']:<6} {lyric['time']:<12} {lyric['time_seconds']:<12.2f} {lyric['time_ms']:<12} {lyric['text']}\n")
                
                f.write("\n\n")
            
            # 写入文本事件信息
            if self.extract_text_var.get() and text_events:
                f.write("文本信息 (text事件):\n")
                f.write("-" * 80 + "\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'时间(毫秒)':<12} {'文本'}\n")
                f.write("-" * 80 + "\n")
                
                for text in text_events:
                    f.write(f"{text['track']:<6} {text['time']:<12} {text['time_seconds']:<12.2f} {text['time_ms']:<12} {text['text']}\n")
                
                f.write("\n\n")
            
            # 写入合并的歌词和文本事件
            if combined_events:
                f.write("所有文本事件 (lyrics和text，按时间排序):\n")
                f.write("-" * 80 + "\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'时间(毫秒)':<12} {'类型':<8} {'文本'}\n")
                f.write("-" * 80 + "\n")
                
                for event in combined_events:
                    f.write(f"{event['track']:<6} {event['time']:<12} {event['time_seconds']:<12.2f} {event['time_ms']:<12} {event['type']:<8} {event['text']}\n")

    def save_as_csv(self, output_dir, file_name, notes, lyrics, text_events, combined_events):
        """保存为CSV表格文件"""
        # 保存音符信息
        if self.extract_notes_var.get() and notes:
            notes_file = os.path.join(output_dir, f"{file_name}_notes.csv")
            with open(notes_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['轨道', '时间(ticks)', '时间(秒)', '时间(毫秒)', '音符', '力度', '通道'])
                for note in notes:
                    writer.writerow([
                        note['track'], note['time'], f"{note['time_seconds']:.3f}", 
                        note['time_ms'], note['note'], note['velocity'], note['channel']
                    ])
        
        # 保存歌词信息
        if self.extract_lyrics_var.get() and lyrics:
            lyrics_file = os.path.join(output_dir, f"{file_name}_lyrics.csv")
            with open(lyrics_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['轨道', '时间(ticks)', '时间(秒)', '时间(毫秒)', '类型', '文本'])
                for lyric in lyrics:
                    writer.writerow([
                        lyric['track'], lyric['time'], f"{lyric['time_seconds']:.3f}", 
                        lyric['time_ms'], lyric['type'], lyric['text']
                    ])
        
        # 保存文本事件
        if self.extract_text_var.get() and text_events:
            text_file = os.path.join(output_dir, f"{file_name}_text_events.csv")
            with open(text_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['轨道', '时间(ticks)', '时间(秒)', '时间(毫秒)', '类型', '文本'])
                for text in text_events:
                    writer.writerow([
                        text['track'], text['time'], f"{text['time_seconds']:.3f}", 
                        text['time_ms'], text['type'], text['text']
                    ])
        
        # 保存合并的歌词和文本事件
        if combined_events:
            combined_file = os.path.join(output_dir, f"{file_name}_all_text.csv")
            with open(combined_file, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['轨道', '时间(ticks)', '时间(秒)', '时间(毫秒)', '类型', '文本'])
                for event in combined_events:
                    writer.writerow([
                        event['track'], event['time'], f"{event['time_seconds']:.3f}", 
                        event['time_ms'], event['type'], event['text']
                    ])

def main():
    # 创建主窗口
    root = tk.Tk()
    app = MidiBatchProcessor(root)
    
    # 运行应用程序
    root.mainloop()

if __name__ == "__main__":
    main() 