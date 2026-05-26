import mido
import argparse
from collections import defaultdict
import os

def analyze_midi(midi_file_path):
    """分析MIDI文件，提取音符开始时间点和歌词信息"""
    midi_file = mido.MidiFile(midi_file_path)
    
    # 存储结果
    notes = []
    lyrics = []
    text_events = []
    
    # 跟踪累计时间
    current_time = 0
    
    # 遍历所有轨道
    for i, track in enumerate(midi_file.tracks):
        track_time = 0
        track_notes = []
        track_lyrics = []
        track_text_events = []
        
        print(f"轨道 {i}: {track.name if hasattr(track, 'name') else '未命名'}")
        
        # 遍历轨道中的所有消息
        for msg in track:
            track_time += msg.time
            
            # 检查音符开始事件
            if msg.type == 'note_on' and msg.velocity > 0:
                note_info = {
                    'track': i,
                    'time': track_time,
                    'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, get_tempo(midi_file)),
                    'note': msg.note,
                    'velocity': msg.velocity,
                    'channel': msg.channel
                }
                track_notes.append(note_info)
            
            # 检查歌词事件
            elif msg.type == 'lyrics':
                lyric_info = {
                    'track': i,
                    'time': track_time,
                    'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, get_tempo(midi_file)),
                    'text': msg.text,
                    'type': 'lyrics'
                }
                track_lyrics.append(lyric_info)
            
            # 检查文本事件（有些MIDI文件使用文本事件存储歌词）
            elif msg.type == 'text':
                text_info = {
                    'track': i,
                    'time': track_time,
                    'time_seconds': mido.tick2second(track_time, midi_file.ticks_per_beat, get_tempo(midi_file)),
                    'text': msg.text,
                    'type': 'text'
                }
                track_text_events.append(text_info)
        
        notes.extend(track_notes)
        lyrics.extend(track_lyrics)
        text_events.extend(track_text_events)
    
    # 按时间排序
    notes.sort(key=lambda x: x['time'])
    lyrics.sort(key=lambda x: x['time'])
    text_events.sort(key=lambda x: x['time'])
    
    return notes, lyrics, text_events

def get_tempo(midi_file):
    """获取MIDI文件的默认速度"""
    # 默认速度 (BPM = 120)
    default_tempo = 500000
    
    # 尝试从第一个轨道找到速度信息
    for track in midi_file.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                return msg.tempo
    
    return default_tempo

def display_results(notes, lyrics, text_events):
    """显示分析结果"""
    print("\n音符信息:")
    print("-" * 80)
    print(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'音符':<6} {'力度':<6} {'通道':<6}")
    print("-" * 80)
    
    for note in notes[:50]:  # 限制输出数量
        print(f"{note['track']:<6} {note['time']:<12} {note['time_seconds']:<12.2f} {note['note']:<6} {note['velocity']:<6} {note['channel']:<6}")
    
    if len(notes) > 50:
        print(f"... 还有 {len(notes) - 50} 个音符未显示")
    
    print("\n歌词信息 (lyrics事件):")
    print("-" * 80)
    print(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'歌词'}")
    print("-" * 80)
    
    for lyric in lyrics:
        print(f"{lyric['track']:<6} {lyric['time']:<12} {lyric['time_seconds']:<12.2f} {lyric['text']}")
    
    print("\n文本信息 (text事件):")
    print("-" * 80)
    print(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'文本'}")
    print("-" * 80)
    
    for text in text_events:
        print(f"{text['track']:<6} {text['time']:<12} {text['time_seconds']:<12.2f} {text['text']}")

def save_combined_results(notes, lyrics, text_events, midi_file_path, output_dir='.'):
    """将所有结果保存到单一的txt文件中"""
    # 获取MIDI文件名（不含路径和扩展名）
    midi_filename = os.path.splitext(os.path.basename(midi_file_path))[0]
    
    # 创建输出文件路径
    output_file = os.path.join(output_dir, f"{midi_filename}_analysis_results.txt")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        # 写入文件头
        f.write(f"MIDI文件分析结果: {midi_file_path}\n")
        f.write("=" * 80 + "\n\n")
        
        # 写入音符信息
        f.write("音符信息:\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'音符':<6} {'力度':<6} {'通道':<6}\n")
        f.write("-" * 80 + "\n")
        
        for note in notes:
            f.write(f"{note['track']:<6} {note['time']:<12} {note['time_seconds']:<12.2f} {note['note']:<6} {note['velocity']:<6} {note['channel']:<6}\n")
        
        f.write("\n\n")
        
        # 写入歌词信息
        f.write("歌词信息 (lyrics事件):\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'歌词'}\n")
        f.write("-" * 80 + "\n")
        
        for lyric in lyrics:
            f.write(f"{lyric['track']:<6} {lyric['time']:<12} {lyric['time_seconds']:<12.2f} {lyric['text']}\n")
        
        f.write("\n\n")
        
        # 写入文本事件信息
        f.write("文本信息 (text事件):\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'文本'}\n")
        f.write("-" * 80 + "\n")
        
        for text in text_events:
            f.write(f"{text['track']:<6} {text['time']:<12} {text['time_seconds']:<12.2f} {text['text']}\n")
        
        f.write("\n\n")
        
        # 写入合并的歌词和文本事件（按时间排序）
        combined_events = lyrics + text_events
        combined_events.sort(key=lambda x: x['time_seconds'])
        
        f.write("所有文本事件 (lyrics和text，按时间排序):\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'类型':<8} {'文本'}\n")
        f.write("-" * 80 + "\n")
        
        for event in combined_events:
            f.write(f"{event['track']:<6} {event['time']:<12} {event['time_seconds']:<12.2f} {event['type']:<8} {event['text']}\n")
    
    return output_file

def main():
    parser = argparse.ArgumentParser(description='分析MIDI文件中的音符和歌词信息')
    parser.add_argument('midi_file', help='MIDI文件路径')
    parser.add_argument('--output-dir', '-o', default='.', help='输出目录路径')
    parser.add_argument('--single-file', '-s', action='store_true', help='将所有结果保存到单一的txt文件中')
    args = parser.parse_args()
    
    try:
        notes, lyrics, text_events = analyze_midi(args.midi_file)
        display_results(notes, lyrics, text_events)
        
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        # 保存结果到单一文件
        if args.single_file:
            output_file = save_combined_results(notes, lyrics, text_events, args.midi_file, output_dir)
            print(f"\n所有结果已保存到单一文件: {output_file}")
        else:
            # 保存结果到多个文件
            with open(os.path.join(output_dir, 'midi_notes.txt'), 'w', encoding='utf-8') as f:
                f.write("音符信息:\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'音符':<6} {'力度':<6} {'通道':<6}\n")
                for note in notes:
                    f.write(f"{note['track']:<6} {note['time']:<12} {note['time_seconds']:<12.2f} {note['note']:<6} {note['velocity']:<6} {note['channel']:<6}\n")
            
            with open(os.path.join(output_dir, 'midi_lyrics.txt'), 'w', encoding='utf-8') as f:
                f.write("歌词信息 (lyrics事件):\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'歌词'}\n")
                for lyric in lyrics:
                    f.write(f"{lyric['track']:<6} {lyric['time']:<12} {lyric['time_seconds']:<12.2f} {lyric['text']}\n")
                
                f.write("\n\n文本信息 (text事件):\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'文本'}\n")
                for text in text_events:
                    f.write(f"{text['track']:<6} {text['time']:<12} {text['time_seconds']:<12.2f} {text['text']}\n")
            
            # 保存合并的歌词和文本事件（按时间排序）
            combined_events = lyrics + text_events
            combined_events.sort(key=lambda x: x['time'])
            
            with open(os.path.join(output_dir, 'midi_all_text.txt'), 'w', encoding='utf-8') as f:
                f.write("所有文本事件 (lyrics和text):\n")
                f.write(f"{'轨道':<6} {'时间(ticks)':<12} {'时间(秒)':<12} {'类型':<8} {'文本'}\n")
                for event in combined_events:
                    f.write(f"{event['track']:<6} {event['time']:<12} {event['time_seconds']:<12.2f} {event['type']:<8} {event['text']}\n")
            
            print(f"\n结果已保存到 {output_dir} 目录下的 midi_notes.txt、midi_lyrics.txt 和 midi_all_text.txt")
        
        # 默认也生成单一文件
        if not args.single_file:
            output_file = save_combined_results(notes, lyrics, text_events, args.midi_file, output_dir)
            print(f"同时，所有结果也已合并保存到: {output_file}")
        
    except Exception as e:
        import traceback
        print(f"错误: {e}")
        print("详细错误信息:")
        traceback.print_exc()

if __name__ == "__main__":
    main() 