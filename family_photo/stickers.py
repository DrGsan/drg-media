"""Bounded TGS decoding for the expression-free canvas player."""
import gzip
import io
import json
import math


def read_animation(objects,key):
    if objects.size(key)>1024*1024:raise ValueError('Sticker too large')
    raw=b''.join(objects.read(key))
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:raw=stream.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('Expanded sticker too large')
    try:data=json.loads(raw)
    except (ValueError,RecursionError):raise ValueError('Invalid sticker') from None
    if not isinstance(data,dict):raise ValueError('Invalid sticker')
    for field in ('w','h','fr','ip','op'):
        value=data.get(field)
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Invalid animation bounds')
    if not (0<data['w']<=512 and 0<data['h']<=512 and 0<data['fr']<=120 and 0<data['op']-data['ip']<=data['fr']*5):
        raise ValueError('Invalid animation bounds')
    if not isinstance(data.get('layers'),list):raise ValueError('Missing layers')
    pending=[(data,0)];nodes=0
    while pending:
        item,depth=pending.pop();nodes+=1
        if nodes>500000 or depth>64:raise ValueError('Complex animation')
        if isinstance(item,dict):
            if any(k in item for k in ('__proto__','constructor','prototype','fonts','chars')):raise ValueError('Unsupported animation')
            if isinstance(item.get('x'),str):raise ValueError('Expressions are forbidden')
            if isinstance(item.get('ty'),int) and item['ty'] in (2,5):raise ValueError('Images and text are forbidden')
            if 'p' in item and isinstance(item['p'],str) or 'u' in item and isinstance(item['u'],str):raise ValueError('External assets are forbidden')
            pending.extend((value,depth+1) for value in item.values())
        elif isinstance(item,list):pending.extend((value,depth+1) for value in item)
    return data
