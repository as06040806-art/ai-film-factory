import asyncio, json, os, subprocess, urllib.request
from pathlib import Path
import edge_tts

ROOT=Path(__file__).parent
OUT=ROOT/'output'; OUT.mkdir(exist_ok=True)
FONT='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'

def run(*args):
    subprocess.run(list(args), check=True)

def download(url, dest):
    if not url.startswith('https://'): raise ValueError('A valid https Runway clip URL is required')
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(request,timeout=120) as response, open(dest,'wb') as f:
        while True:
            chunk=response.read(1024*1024)
            if not chunk: break
            f.write(chunk)
    if dest.stat().st_size<10000: raise RuntimeError('Downloaded video is unexpectedly small')

def escape_ass(s):
    return s.replace('\\','\\\\').replace('{','').replace('}','').replace('\n','\\N')

def make_ass(lines,path):
    header='''[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK TC,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,3,1,2,42,42,145,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    def stamp(t): return f'{int(t//3600)}:{int(t%3600//60):02}:{t%60:05.2f}'
    with open(path,'w',encoding='utf-8') as f:
        f.write(header)
        for i,line in enumerate(lines):
            f.write(f'Dialogue: 0,{stamp(i*5)},{stamp((i+1)*5)},Default,,0,0,0,,{escape_ass(line["text"])}\n')

async def speech(lines, folder):
    folder.mkdir(exist_ok=True)
    for i,line in enumerate(lines):
        voice='zh-TW-HsiaoChenNeural' if line['speaker']=='female' else 'zh-TW-YunJheNeural'
        # Speech synthesized for every segment; speech >5s is shortened to fit rather than cut off.
        raw=folder/f'{i:02}_raw.mp3'
        await edge_tts.Communicate(line['text'],voice,rate='+15%').save(str(raw))
        duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(raw)],text=True))
        speed=max(1.0,duration/4.7)
        run('ffmpeg','-y','-i',str(raw),'-af',f'atempo={min(speed,100):.4f},apad=pad_dur=5,atrim=duration=5','-ar','44100','-ac','2',str(folder/f'{i:02}.wav'))

def render(name, lines, url):
    folder=OUT/name; folder.mkdir(exist_ok=True)
    source=folder/'runway_opening.mp4'; download(url,source)
    # 60 seconds, 12 five-second scenes. First 5 seconds is actual Runway video;
    # remaining scenes are animated crops of a still from that video (not lip-synced).
    still=folder/'last_frame.png'
    run('ffmpeg','-y','-sseof','-0.1','-i',str(source),'-frames:v','1',str(still))
    make_ass(lines,folder/'subtitles.ass')
    asyncio.run(speech(lines,folder/'speech'))
    for i in range(12):
        segment=folder/f'visual_{i:02}.mp4'
        if i==0:
            run('ffmpeg','-y','-i',str(source),'-t','5','-vf','scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=24,setsar=1','-an','-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p',str(segment))
        else:
            # alternate gentle zoom and pan, using a real frame, without black filler
            direction='on/120' if i%2 else '1-on/120'
            vf=f'scale=960:1708,crop=720:1280:x=120+40*sin(n/50+{i}):y=214+50*cos(n/60+{i}),fps=24,setsar=1'
            run('ffmpeg','-y','-loop','1','-framerate','24','-i',str(still),'-t','5','-vf',vf,'-an','-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p',str(segment))
    manifest=folder/'segments.txt'
    manifest.write_text(''.join(f"file '{(folder/f'visual_{i:02}.mp4').resolve()}'\n" for i in range(12)))
    video=folder/'visual.mp4'
    run('ffmpeg','-y','-f','concat','-safe','0','-i',str(manifest),'-c','copy',str(video))
    audlist=folder/'audio.txt'
    audlist.write_text(''.join(f"file '{(folder/'speech'/f'{i:02}.wav').resolve()}'\n" for i in range(12)))
    audio=folder/'dialogue.wav'
    run('ffmpeg','-y','-f','concat','-safe','0','-i',str(audlist),'-c:a','pcm_s16le',str(audio))
    target=OUT/f'{name}_60s.mp4'
    ass=(folder/'subtitles.ass').resolve().as_posix()
    run('ffmpeg','-y','-i',str(video),'-i',str(audio),'-vf',f'ass={ass}', '-t','60','-c:v','libx264','-preset','veryfast','-crf','24','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart',str(target))
    print('Created',target)

if __name__=='__main__':
    stories=json.loads((ROOT/'stories.json').read_text(encoding='utf-8'))
    for story in stories:
        key='RUNWAY_'+story['name'].upper()+'_URL'
        url=os.environ.get(key,'')
        if not url: raise RuntimeError(f'Missing GitHub Actions input {key}')
        render(story['name'],story['lines'],url)
