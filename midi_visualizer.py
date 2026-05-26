import matplotlib.pyplot as plt
import numpy as np
import argparse
import os
from midi_analyzer import analyze_midi

def visualize_midi(midi_file_path, output_dir='.'):
    """可视化MIDI文件中的音符和歌词分布"""
    try:
        # 分析MIDI文件
        notes, lyrics, text_events = analyze_midi(midi_file_path)
        
        # 创建图形
        plt.figure(figsize=(12, 8))
        
        # 绘制音符分布
        if notes:
            times = [note['time_seconds'] for note in notes]
            pitches = [note['note'] for note in notes]
            velocities = [note['velocity'] for note in notes]
            
            # 使用散点图绘制音符
            plt.scatter(times, pitches, c=velocities, cmap='viridis', 
                       alpha=0.7, s=30, label='音符')
            
            # 添加颜色条以表示力度
            cbar = plt.colorbar()
            cbar.set_label('力度')
        
        # 绘制歌词位置
        if lyrics:
            lyric_times = [lyric['time_seconds'] for lyric in lyrics]
            # 在底部绘制歌词位置标记
            plt.scatter(lyric_times, [0] * len(lyric_times), marker='^', 
                       color='red', s=100, label='歌词事件')
        
        # 绘制文本事件位置
        if text_events:
            text_times = [text['time_seconds'] for text in text_events]
            # 在底部绘制文本事件位置标记
            plt.scatter(text_times, [5] * len(text_times), marker='s', 
                       color='blue', s=100, label='文本事件')
        
        # 设置图表属性
        plt.title(f'MIDI文件分析: {os.path.basename(midi_file_path)}')
        plt.xlabel('时间 (秒)')
        plt.ylabel('MIDI音符号')
        plt.grid(True, alpha=0.3)
        plt.legend()
        
        # 保存图表
        output_file = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(midi_file_path))[0]}_visualization.png")
        plt.savefig(output_file, dpi=300, bbox_inches='tight')
        plt.close()
        
        print(f"可视化结果已保存到: {output_file}")
        
        # 创建带有歌词文本的时间线图
        create_lyrics_timeline(lyrics, text_events, midi_file_path, output_dir)
        
        return output_file
    except Exception as e:
        print(f"可视化过程中出错: {e}")
        return None

def create_lyrics_timeline(lyrics, text_events, midi_file_path, output_dir='.'):
    """创建带有歌词文本的时间线图"""
    # 合并歌词和文本事件
    combined_events = lyrics + text_events
    combined_events.sort(key=lambda x: x['time_seconds'])
    
    if not combined_events:
        print("没有找到歌词或文本事件，无法创建时间线图")
        return
    
    # 创建图形
    plt.figure(figsize=(15, 8))
    
    # 获取时间点和文本
    times = [event['time_seconds'] for event in combined_events]
    texts = [event['text'] for event in combined_events]
    event_types = [event['type'] for event in combined_events]
    
    # 设置颜色映射
    colors = ['red' if t == 'lyrics' else 'blue' for t in event_types]
    
    # 绘制时间线
    plt.scatter(times, [1] * len(times), c=colors, s=100)
    
    # 添加文本标签
    for i, (time, text) in enumerate(zip(times, texts)):
        # 限制文本长度，避免重叠
        short_text = text[:20] + '...' if len(text) > 20 else text
        plt.annotate(short_text, (time, 1), 
                    textcoords="offset points", 
                    xytext=(0, 10 if i % 2 == 0 else -30), 
                    ha='center', 
                    fontsize=8,
                    color=colors[i],
                    rotation=45 if i % 2 == 0 else -45)
    
    # 设置图表属性
    plt.title(f'歌词/文本时间线: {os.path.basename(midi_file_path)}')
    plt.xlabel('时间 (秒)')
    plt.yticks([])  # 隐藏Y轴刻度
    plt.grid(True, alpha=0.3, axis='x')
    
    # 添加图例
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor='red', markersize=10, label='歌词事件'),
        Line2D([0], [0], marker='o', color='w', markerfacecolor='blue', markersize=10, label='文本事件')
    ]
    plt.legend(handles=legend_elements)
    
    # 保存图表
    output_file = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(midi_file_path))[0]}_lyrics_timeline.png")
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    plt.close()
    
    print(f"歌词时间线已保存到: {output_file}")

def main():
    parser = argparse.ArgumentParser(description='可视化MIDI文件中的音符和歌词分布')
    parser.add_argument('midi_file', help='MIDI文件路径')
    parser.add_argument('--output-dir', '-o', default='.', help='输出目录路径')
    args = parser.parse_args()
    
    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)
    
    # 可视化MIDI文件
    visualize_midi(args.midi_file, args.output_dir)

if __name__ == "__main__":
    main() 