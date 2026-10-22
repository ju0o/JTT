"""Verify documentation media without launching JTT or touching user state."""
import argparse
import gzip
import hashlib
import json
import re
import subprocess
from pathlib import Path
from PIL import Image
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT.parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-manifest', action='store_true')
    args = parser.parse_args()
    plan = json.loads((ROOT/'timeline.json').read_text())
    with gzip.open(ROOT/'captures.json.gz','rt') as stream:
        frames = json.load(stream)
    markers = ['/home/', '/mnt/', 'DESKTOP', 'OPENAI_API_KEY', 'password', 'token', '@', 'BEGIN PRIVATE', 'sk-']
    for frame in frames:
        text = '\n'.join(''.join(cell['data'] for cell in row) for row in frame['cells'])
        assert not any(marker in text for marker in markers)
        assert not re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)
    # Assert observed panel content, not a scene-name assumption.
    by_id = {(f['source'], f['frame_index']): f for f in frames}
    for i in [17,18,19]:
        assert '터미널 배치' in ''.join(c['data'] for c in by_id['narrow',i]['cells'][0])
    for i in [20,21,22,23,24,25,26,27,28,35,36]:
        assert '터미널 · Terminal' in ''.join(c['data'] for c in by_id['narrow',i]['cells'][0])
    gif = Image.open(MEDIA/'jtt-rc3-showcase.gif')
    assert gif.size == (1280,800)
    millis = 0
    for i in range(gif.n_frames):
        gif.seek(i)
        millis += gif.info['duration']
    assert millis == 18000 and gif.n_frames > 7
    assert (MEDIA/'jtt-rc3-showcase.gif').stat().st_size < 3_000_000
    assert Image.open(MEDIA/'jtt-rc3-thumbnail.png').size == (1280,640)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    probe = subprocess.run([ffmpeg,'-i',str(MEDIA/'jtt-rc3-intro.mp4')],capture_output=True,text=True)
    assert 'Duration: 00:01:15.00' in probe.stderr and '1920x1200' in probe.stderr
    decode = subprocess.run([ffmpeg,'-v','error','-i',str(MEDIA/'jtt-rc3-intro.mp4'),'-f','null','-','-progress','pipe:1'],capture_output=True,text=True)
    assert decode.returncode == 0 and not decode.stderr
    assert re.findall(r'^frame=(\d+)$', decode.stdout, re.M)[-1] == '375'
    assert sum(c['duration'] for c in plan['video']) == 75
    assert sum(c['duration'] for c in plan['gif']) == 18
    for lang in ('en','ko'):
        srt=(MEDIA/f'jtt-rc3-intro.{lang}.srt').read_text()
        assert srt.count(' --> ') == len(plan['video'])
        assert '00:01:15,000' in srt
        for clip in plan['video']:
            assert clip[lang] in srt
    names=['jtt-rc3-showcase.gif','jtt-rc3-intro.mp4','jtt-rc3-thumbnail.png',
           'jtt-rc3-intro.en.srt','jtt-rc3-intro.ko.srt',
           'showcase-source/captures.json.gz','showcase-source/timeline.json',
           'showcase-source/capture-evidence.json','showcase-source/render.py']
    files=[]
    for name in names:
        p=MEDIA/name
        info={'file':name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        if name.endswith('.gif'):
            info.update(seconds=18,width=1280,height=800,stored_frames=gif.n_frames)
        elif name.endswith('.mp4'):
            info.update(seconds=75,width=1920,height=1200,fps=5,frames=375)
        elif name.endswith('.png'):
            info.update(width=1280,height=640)
        files.append(info)
    result={'product':'JTT RC3','source_tag':'jtt-v0.3.0-rc3',
            'source_commit':'2a4480178b500fbd3044d9e0bb152a98dbc12a63',
            'truth_level':'REAL_UI_SYNTHETIC_DATA',
            'presentation':'Edited independent takes of real PTY cells; labeled detail crops and editorial detach title',
            'native_windows_terminal':'PENDING',
            'isolated_pty_160x44':'PASS','isolated_pty_80x24':'PASS',
            'source_frames_scanned':len(frames),'privacy_check':'Bounded keyword/IP scans plus representative visual review',
            'runtime_unchanged':True,'files':files}
    path=MEDIA/'jtt-rc3-manifest.json'
    if args.write_manifest:
        path.write_text(json.dumps(result,indent=2)+'\n')
    else:
        assert json.loads(path.read_text())==result, 'Manifest differs; investigate before accepting new hashes'
    print('PASS: media decode, dimensions, duration, captions, source-panel identity, bounded privacy scan and hashes')

if __name__=='__main__':
    main()
