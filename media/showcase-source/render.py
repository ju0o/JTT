"""Render verified RC3 PTY cells for documentation, not a native desktop recording.

Usage: python render.py --mono-font FONT --cjk-font FONT
Requires Pillow 12.3.0 and imageio-ffmpeg 0.6.0 in an isolated media venv.
Font files are supplied locally and are not distributed with the media.
"""
import argparse
import gzip
import hashlib
import json
import subprocess
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent
BG = '#101823'
FG = '#d0d8e4'
PALETTE = {'black': BG, 'red': '#d47777', 'green': '#74b6a0',
           'brown': '#c4ab78', 'yellow': '#c4ab78', 'blue': '#7399bd',
           'magenta': '#ad8eaf', 'cyan': '#7abfc5', 'white': '#d5dce5',
           'brightblack': '#647184', 'brightwhite': '#f0f4f8'}
BOX = {'─':'lr', '│':'ud', '┌':'rd', '┐':'ld', '└':'ru', '┘':'lu',
       '├':'urd', '┤':'uld', '┬':'lrd', '┴':'lru', '┼':'lrud'}

def color(value, default):
    if value in PALETTE:
        return PALETTE[value]
    if value and len(value) == 6 and all(c in '0123456789abcdefABCDEF' for c in value):
        return '#' + value
    return default

def terminal(frame, scale=1):
    """Rasterize actual cells; cell-aligned box glyphs preserve continuous borders."""
    cw, ch = 12 * scale, 22 * scale
    im = Image.new('RGB', (frame['cols'] * cw, frame['rows'] * ch), BG)
    draw = ImageDraw.Draw(im)
    mono = ImageFont.truetype(ARGS.mono_font, 19 * scale)
    cjk = ImageFont.truetype(ARGS.cjk_font, 19 * scale)
    # Background pass first, so wide glyphs cannot be erased by continuation cells.
    for y, row in enumerate(frame['cells']):
        for x, c in enumerate(row):
            fg, bg = color(c.get('fg'), FG), color(c.get('bg'), BG)
            if c.get('reverse'):
                fg, bg = bg, fg
            draw.rectangle((x*cw, y*ch, (x+1)*cw-1, (y+1)*ch-1), fill=bg)
    for y, row in enumerate(frame['cells']):
        for x, c in enumerate(row):
            data = c.get('data', '')
            if not data.strip():
                continue
            fg, bg = color(c.get('fg'), FG), color(c.get('bg'), BG)
            if c.get('reverse'):
                fg, bg = bg, fg
            px, py = x*cw, y*ch
            if data in BOX:
                cx, cy = px+cw//2, py+ch//2
                ends = {'l':(px,cy), 'r':(px+cw,cy), 'u':(cx,py), 'd':(cx,py+ch)}
                for direction in BOX[data]:
                    draw.line((cx,cy,*ends[direction]), fill=fg, width=scale)
            else:
                wide = any(unicodedata.east_asian_width(t) in ('W','F') for t in data)
                draw.text((px,py), data, font=cjk if wide else mono, fill=fg)
    cursor = frame.get('cursor', {})
    if not cursor.get('hidden', True) and 0 <= cursor.get('x',-1) < frame['cols'] and 0 <= cursor.get('y',-1) < frame['rows']:
        x,y=cursor['x']*cw,cursor['y']*ch
        draw.rectangle((x,y+ch-3*scale,x+cw-1,y+ch-1),fill='#b8e0dd')
    return im

def paint(frame, clip):
    im = Image.new('RGB', (1920,1200), BG)
    d = ImageDraw.Draw(im)
    d.text((24,12),'JTT — Ju0 Terminal Tower | RC3',font=TITLE,fill='#88c6c8')
    d.text((24,52),'Actual PTY output · synthetic data · edited takes/timing · not a desktop recording',font=SMALL,fill='#adb9c8')
    if clip.get('interstitial'):
        # Explicit editorial card, not fabricated application UI.
        d.text((100,330),'UI detached',font=HERO,fill=FG)
        d.text((100,410),'The two test shells kept running.',font=TITLE,fill='#88c6c8')
        d.text((100,470),'Next: reopening the same isolated JTT state',font=TITLE,fill=FG)
        d.text((100,540),'EDITORIAL TRANSITION · identity verified in capture metadata',font=SMALL,fill='#adb9c8')
    else:
        screen = terminal(frame)
        if frame['cols'] == 80:
            # Show the complete narrow screen, centered and enlarged, not tiny in a corner.
            screen = screen.resize((1536,845),Image.Resampling.LANCZOS)
            im.paste(screen,(192,130))
        else:
            screen = screen.resize((1872,944),Image.Resampling.LANCZOS)
            im.paste(screen,(24,108))
            if clip.get('detail'):
                # Detail is explicitly a crop of the same source frame, not a new terminal.
                original = terminal(frame,scale=2)
                crop = original.crop((91*24,1*44,137*24,6*44)).resize((1008,201),Image.Resampling.LANCZOS)
                d.rectangle((870,697,1900,940),fill=BG,outline='#7abfc5',width=2)
                d.text((886,704),'RIGHT detail · same recorded frame',font=SMALL,fill='#88c6c8')
                im.paste(crop,(882,735))
    d.text((24,1080),clip['en'],font=TITLE,fill=FG)
    d.text((24,1132),clip['ko'],font=KOREAN,fill='#88c6c8')
    return im

def stamp(t):
    ms=round(t*1000)
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'

def main():
    global ARGS,TITLE,SMALL,KOREAN,HERO
    parser=argparse.ArgumentParser()
    parser.add_argument('--mono-font',required=True)
    parser.add_argument('--cjk-font',required=True)
    ARGS=parser.parse_args()
    TITLE=ImageFont.truetype(ARGS.cjk_font,30)
    SMALL=ImageFont.truetype(ARGS.cjk_font,21)
    KOREAN=ImageFont.truetype(ARGS.cjk_font,30)
    HERO=ImageFont.truetype(ARGS.mono_font,52)
    plan=json.loads((ROOT/'timeline.json').read_text())
    with gzip.open(ROOT/'captures.json.gz','rt') as f:
        frames=json.load(f)
    records=[]
    for kind in ('gif','video'):
        timeline=[]; subtitles={'en':[],'ko':[]}; elapsed=0
        for clip in plan[kind]:
            selected=[f for f in frames if f['source']==clip['source'] and
                      (f['frame_index'] in clip['frame_indices'] if 'frame_indices' in clip else f['scene']==clip['scene'])]
            if not selected:
                raise RuntimeError('Missing recorded scene: '+clip['scene'])
            if clip.get('stable') and len(selected)>4:
                selected=selected[len(selected)//4:len(selected)*3//4]
            count=clip['duration']*5
            hold=clip.get('hold_last',0)*5
            for i in range(count):
                index = len(selected)-1 if hold and i >= count-hold else min(len(selected)-1,int(i*len(selected)/max(1,count-hold)))
                timeline.append((selected[index],clip))
            for lang in subtitles:
                subtitles[lang].append(f"{len(subtitles[lang])+1}\n{stamp(elapsed)} --> {stamp(elapsed+clip['duration'])}\n{clip[lang]}\n")
            elapsed+=clip['duration']
        path=OUT/('jtt-rc3-showcase.gif' if kind=='gif' else 'jtt-rc3-intro.mp4')
        if kind=='gif':
            images=[paint(*x).resize((1280,800),Image.Resampling.LANCZOS) for x in timeline]
            images[0].save(path,save_all=True,append_images=images[1:],duration=200,loop=0,optimize=True)
        else:
            command=[imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-y','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1200','-r','5','-i','-','-an','-c:v','libx264','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',str(path)]
            proc=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
            for frame,clip in timeline:
                proc.stdin.write(paint(frame,clip).tobytes())
            proc.stdin.close();error=proc.stderr.read()
            if proc.wait()!=0:
                raise RuntimeError(error.decode(errors='replace'))
            for lang,lines in subtitles.items():
                (OUT/f'jtt-rc3-intro.{lang}.srt').write_text('\n'.join(lines))
        records.append({'file':path.name,'seconds':elapsed,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    chosen=[f for f in frames if f['source']==plan['thumbnail']['source'] and f['scene']==plan['thumbnail']['scene']]
    frame=chosen[len(chosen)//2]
    thumbnail=Image.new('RGB',(1280,640),BG);d=ImageDraw.Draw(thumbnail)
    d.text((24,12),'JTT · Your terminals. One workspace.',font=TITLE,fill='#88c6c8')
    d.text((24,58),'RC3 · actual PTY output · demo data · cropped view',font=SMALL,fill='#adb9c8')
    crop=terminal(frame).crop((0,0,1920,440)).resize((1232,282),Image.Resampling.LANCZOS)
    thumbnail.paste(crop,(24,114))
    d.text((24,460),'Create → type → switch → detach → reconnect',font=TITLE,fill=FG)
    d.text((24,515),'생성 · 직접 입력 · 전환 · UI 분리 · 재연결',font=KOREAN,fill='#88c6c8')
    thumbnail.save(OUT/'jtt-rc3-thumbnail.png')
    print(json.dumps(records,indent=2))

if __name__=='__main__':
    main()
