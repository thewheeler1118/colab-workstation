"""Assemble a final delivery MP4 from generated video frames.

Default shape (the frozen A/B production shape): 226 generated frames
plus a 48-frame static tail hold of the reference image, scaled to
1080x1920 at 24 fps. All of those numbers are parameters now (TASK-007):
a generic TaskSpec drives them, and tail_card_frames=0 means "no tail
card, deliver the animation alone". Requires ffmpeg + ffprobe on the
execution machine. No external models used.
"""
import argparse, json, subprocess, hashlib
from pathlib import Path
from h3_contract import FPS,ANIMATION_FRAMES,POSTER_FRAMES,TOTAL_FRAMES,WIDTH,HEIGHT,validate_poster,validate_ref_image,sha256_file

def ffprobe(path):
    p=subprocess.run(['ffprobe','-v','error','-count_frames','-show_streams','-of','json',str(path)],capture_output=True,text=True,check=True)
    return json.loads(p.stdout)

def run(raw:Path,poster:Path | None,dest:Path,*,animation_frames:int=ANIMATION_FRAMES,tail_card_frames:int=POSTER_FRAMES,tail_sha256:str | None=None,fps:int=FPS,output_width:int=1080,output_height:int=1920):
    """Append a static tail hold to the generated frames and scale to delivery size.

    `poster` is the tail-card image (the spec's ref_image_1). When
    tail_card_frames is 0 it is ignored and may be None: the output is
    just the animation, scaled. When a tail is requested, the image is
    verified first: against tail_sha256 when given, otherwise against
    the frozen original-poster hash (the standalone CLI's legacy
    behaviour). The raw model output must have exactly
    animation_frames frames; a short clip is never padded.
    """
    total_frames=animation_frames+tail_card_frames
    if tail_card_frames>0:
        if poster is None:
            raise ValueError('Tail card requested but no tail image given')
        if tail_sha256 is not None:
            validate_ref_image(Path(poster),tail_sha256)
        else:
            validate_poster(Path(poster))
    streams=ffprobe(raw)['streams']
    vid=next((x for x in streams if x['codec_type']=='video'),None)
    if not vid: raise ValueError('No video in generation output')
    # Fail closed: the exact animation frame count is required, do not pad a short clip and claim success.
    n=int(vid.get('nb_read_frames') or vid.get('nb_frames') or 0)
    if n!=animation_frames: raise ValueError(f'Raw model output {n} frames, expected {animation_frames}')
    has_audio=any(x['codec_type']=='audio' for x in streams)
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_name(dest.stem+'.part.mp4')
    vf=f'[0:v]fps={fps},scale={output_width}:{output_height}:force_original_aspect_ratio=increase,crop={output_width}:{output_height},setsar=1,trim=end_frame={animation_frames},setpts=PTS-STARTPTS'
    if tail_card_frames>0:
        # The tail still is scaled/cropped to the output size, like the
        # animation stream: a generic ref image need not already be
        # output-sized (the A/B poster happened to be 1080x1920, which
        # hid this mismatch in the old filter).
        vf+=f'[v0];[1:v]fps={fps},scale={output_width}:{output_height}:force_original_aspect_ratio=increase,crop={output_width}:{output_height},setsar=1,trim=end_frame={tail_card_frames},setpts=PTS-STARTPTS[v1];[v0][v1]concat=n=2:v=1:a=0[v]'
        inputs=['-i',str(raw),'-loop','1','-framerate',str(fps),'-i',str(poster)]
    else:
        vf+='[v]'
        inputs=['-i',str(raw)]
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-y']+inputs+['-filter_complex',vf,'-map','[v]']
    if has_audio:
        cmd+=['-map','0:a:0']
        if tail_card_frames>0:
            cmd+=['-af',f'apad=pad_dur={tail_card_frames/fps}']
        cmd+=['-c:a','aac','-b:a','192k']
    else: cmd+=['-an']
    cmd+=['-c:v','libx264','-pix_fmt','yuv420p','-r',str(fps),'-frames:v',str(total_frames),'-movflags','+faststart','-t',str(total_frames/fps),str(temp)]
    try:
        subprocess.run(cmd,check=True,timeout=2400)
        probe=ffprobe(temp)
        vs=next(x for x in probe['streams'] if x['codec_type']=='video')
        assert int(vs['width'])==output_width and int(vs['height'])==output_height
        assert int(vs.get('nb_read_frames') or vs.get('nb_frames') or 0)==total_frames
        # Verify last decoded frame is exact source poster (lossy encode may alter pixel values).
        # Equality is defined as source image content/duration rather than byte-identical encoded frame.
        temp.replace(dest)
        return {'file':str(dest),'frames':total_frames,'seconds':total_frames/fps,'sha256':sha256_file(dest),'with_audio':has_audio}
    finally:
        if temp.exists():temp.unlink()

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--raw',type=Path,required=True);p.add_argument('--poster',type=Path,default=None)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--animation-frames',type=int,default=ANIMATION_FRAMES)
    p.add_argument('--tail-card-frames',type=int,default=POSTER_FRAMES)
    p.add_argument('--tail-sha256',type=str,default=None)
    p.add_argument('--fps',type=int,default=FPS)
    p.add_argument('--output-width',type=int,default=1080)
    p.add_argument('--output-height',type=int,default=1920)
    a=p.parse_args();print(json.dumps(run(a.raw,a.poster,a.output,animation_frames=a.animation_frames,tail_card_frames=a.tail_card_frames,tail_sha256=a.tail_sha256,fps=a.fps,output_width=a.output_width,output_height=a.output_height),ensure_ascii=False,indent=2))
