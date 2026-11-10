"""Bounded recording hints, verified against the uploaded media container."""
import json
import math


def parse(raw):
    if raw is None:return None
    try:
        if len(raw)>2048:raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict) or set(value)-{'kind','duration_ms','waveform'}:raise ValueError()
        kind=value.get('kind');duration=value.get('duration_ms');wave=value.get('waveform',[])
        if kind not in {'voice','video_note'} or type(duration) is not int or not 300<=duration<=(600000 if kind=='voice' else 60000):raise ValueError()
        if not isinstance(wave,list) or len(wave)>64 or any(type(n) is not int or not 0<=n<=31 for n in wave) or kind=='video_note' and wave:raise ValueError()
        return {'kind':kind,'duration_ms':duration,'waveform':wave}
    except (ValueError,TypeError):
        from .site_messaging import MessagingError
        raise MessagingError('invalid_recording',422)


def verified(value,probe):
    from .site_messaging import MessagingError
    try:
        streams=probe['streams'];duration=float(probe['format']['duration'])
        if not math.isfinite(duration) or duration<.25 or duration>(600.25 if value['kind']=='voice' else 60.25):raise ValueError()
        video=[s for s in streams if s.get('codec_type')=='video'];audio=[s for s in streams if s.get('codec_type')=='audio']
        if value['kind']=='voice':
            if video or not audio:raise ValueError()
        elif len(video)!=1 or not audio or not 0<video[0]['width']==video[0]['height']<=1920:raise ValueError()
        return {'kind':value['kind'],'duration':round(min(duration,600 if value['kind']=='voice' else 60),3),'waveform':value['waveform'],**({'length':video[0]['width']} if value['kind']=='video_note' else {})}
    except (ValueError,KeyError,TypeError):raise MessagingError('invalid_recording',422)


def telegram_waveform(raw):
    # Telegram documentAttributeAudio: little-endian, bitpacked 5-bit samples.
    if not isinstance(raw,bytes) or not 0<len(raw)<=256:return []
    packed=int.from_bytes(raw,'little');samples=[(packed>>(5*i))&31 for i in range(len(raw)*8//5)]
    count=min(64,len(samples))
    return [max(samples[i*len(samples)//count:max(i*len(samples)//count+1,(i+1)*len(samples)//count)]) for i in range(count)]
