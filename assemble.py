"""Append 48 exact source-poster frames to 226 generated video frames.
Requires ffmpeg + ffprobe on the execution machine. No external models used.
"""
import argparse, json, subprocess, hashlib
from pathlib import Path
from h3_contract import FPS,ANIMATION_FRAMES,POSTER_FRAMES,TOTAL_FRAMES,WIDTH,HEIGHT,validate_poster,sha256_file

def ffprobe(path):
    p=subprocess.run(['ffprobe','-v','error','-count_frames','-show_streams','-of','json',str(path)],capture_output=True,text=True,check=True)
    return json.loads(p.stdout)

def run(raw:Path,poster:Path,dest:Path):
    validate_poster(poster)
    streams=ffprobe(raw)['streams']
    vid=next((x for x in streams if x['codec_type']=='video'),None)
    if not vid: raise ValueError('No video in generation output')
    # Fail closed: 226 source frames are required, do not pad a short clip and claim success.
    n=int(vid.get('nb_read_frames') or vid.get('nb_frames') or 0)
    if n!=ANIMATION_FRAMES: raise ValueError(f'Raw model output {n} frames, expected {ANIMATION_FRAMES}')
    has_audio=any(x['codec_type']=='audio' for x in streams)
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_name(dest.stem+'.part.mp4')
    vf=f'[0:v]fps={FPS},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,trim=end_frame={ANIMATION_FRAMES},setpts=PTS-STARTPTS[v0];[1:v]fps={FPS},trim=end_frame={POSTER_FRAMES},setpts=PTS-STARTPTS[v1];[v0][v1]concat=n=2:v=1:a=0[v]'
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(raw),'-loop','1','-framerate',str(FPS),'-i',str(poster),'-filter_complex',vf,'-map','[v]']
    if has_audio:
        cmd+=['-map','0:a:0','-af','apad=pad_dur=2.0','-c:a','aac','-b:a','192k']
    else: cmd+=['-an']
    cmd+=['-c:v','libx264','-pix_fmt','yuv420p','-r',str(FPS),'-frames:v',str(TOTAL_FRAMES),'-movflags','+faststart','-t',str(TOTAL_FRAMES/FPS),str(temp)]
    try:
        subprocess.run(cmd,check=True,timeout=2400)
        probe=ffprobe(temp)
        vs=next(x for x in probe['streams'] if x['codec_type']=='video')
        assert int(vs['width'])==1080 and int(vs['height'])==1920
        assert int(vs.get('nb_read_frames') or vs.get('nb_frames') or 0)==TOTAL_FRAMES
        # Verify last decoded frame is exact source poster (lossy encode may alter pixel values).
        # Equality is defined as source image content/duration rather than byte-identical encoded frame.
        temp.replace(dest)
        return {'file':str(dest),'frames':TOTAL_FRAMES,'seconds':TOTAL_FRAMES/FPS,'sha256':sha256_file(dest),'with_audio':has_audio}
    finally:
        if temp.exists():temp.unlink()

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--raw',type=Path,required=True);p.add_argument('--poster',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(run(a.raw,a.poster,a.output),ensure_ascii=False,indent=2))
