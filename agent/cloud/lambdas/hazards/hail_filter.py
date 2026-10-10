"""SQS MRMS notifications and replay MrmsFileAvailable -> per-farm hail watches."""
from __future__ import annotations
import io
import json
import hashlib
import re
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus
from fieldwatch import contracts as C
from fieldwatch.hazards import summarize_hail, MRMS_BUCKET
from . import common as cm

PRODUCTS = ('MESH_Max_60min_00.50', 'MESH_Max_1440min_00.50')


def notifications(event):
    if 'Message' in event: event = json.loads(event['Message'])
    if 'detail' in event: return [event['detail']]
    if 'Records' in event:
        return [dict(bucket=row['s3']['bucket']['name'], key=unquote_plus(row['s3']['object']['key'])) for row in event['Records'] if 's3' in row]
    return [event]


def save_overlay(key, grid, polygon):
    """Persist a georeferenced clipped swath and its transparent map PNG."""
    import numpy as np
    from rasterio.io import MemoryFile
    from PIL import Image
    # Read the window again for exact GRIB georeferencing; callers pass it via process.
    transform, crs = grid[1], grid[2]
    values = grid[0]
    with MemoryFile() as mem:
        with mem.open(driver='GTiff', height=values.shape[0], width=values.shape[1], count=1,
                      dtype='float32', crs=crs, transform=transform, nodata=-1, compress='deflate') as dst:
            dst.write(np.nan_to_num(values,nan=-1).astype('float32'),1)
        cm.client('s3').put_object(Bucket=cm.bucket(), Key=key+'.tif', Body=mem.read(), ContentType='image/tiff')
    rgba = np.zeros((*values.shape,4),dtype='uint8')
    rgba[...,0] = 230; rgba[...,1] = 100
    rgba[...,3] = np.where(np.isfinite(values) & (values>=25.4),180,0)
    out = io.BytesIO(); Image.fromarray(rgba).save(out,format='PNG')
    cm.client('s3').put_object(Bucket=cm.bucket(),Key=key+'.png',Body=out.getvalue(),ContentType='image/png')


def read_swath(url, polygon):
    import numpy as np
    import rasterio
    from rasterio.features import geometry_window, geometry_mask
    from rasterio.warp import transform_geom
    from rasterio.errors import WindowError
    geometry = {'type':'Polygon','coordinates':[polygon+[polygon[0]] if polygon[-1]!=polygon[0] else polygon]}
    with rasterio.open(url) as src:
        geom = transform_geom('EPSG:4326',src.crs,geometry)
        try: window = geometry_window(src,[geom])
        except WindowError: return None
        values = src.read(1,window=window,masked=True).astype(float).filled(float("nan"))
        transform = src.window_transform(window)
        inside = geometry_mask([geom],out_shape=values.shape,transform=transform,invert=True,all_touched=True)
        values[~inside | (values<0) | ~np.isfinite(values)] = np.nan
        return values, transform, src.crs


def store_watch(watch):
    """Stable row keys make redelivery safe. Event delivery may repeat (EventBridge is at least once)."""
    sort_key = watch.opened_at+'#'+watch.watch_id
    keys = [{'field_id':f.field_id, 'opened_at_watch_id':sort_key} for f in watch.fields]
    existing = [cm.table('watches').get_item(Key=key).get('Item',{}) for key in keys]
    if existing and all(row.get('event_emitted') for row in existing): return False
    for field in watch.fields:
        row = watch.model_dump(mode='json')
        row.update(field_id=field.field_id,farm_id=field.farm_id,status='open',
                   opened_at_watch_id=watch.opened_at+'#'+watch.watch_id,
                   expires_at=int(cm.parse_time(watch.expires_at).timestamp()), expires_at_iso=watch.expires_at)
        cm.table('watches').put_item(Item=cm.to_ddb(row))
    cm.emit(C.WatchOpened(occurred_at=cm.iso(cm.now()),watch=watch),source='fieldwatch.hazards')
    for key in keys:
        cm.table('watches').update_item(Key=key,UpdateExpression='SET event_emitted = :yes',ExpressionAttributeValues={':yes':True})
    return True


def process(event):
    opened = 0
    for file in notifications(event):
        key = file.get('key','')
        if file.get('bucket',MRMS_BUCKET) != MRMS_BUCKET or not any(key.startswith('CONUS/'+p+'/') for p in PRODUCTS): continue
        match = re.search(r'(\d{8})-(\d{6})',key)
        valid = cm.parse_time(file.get('valid_time'))
        if valid is None and match: valid = datetime.strptime(''.join(match.groups()),'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        if valid is None: raise ValueError('MRMS file lacks valid time')
        url = f'/vsigzip//vsicurl/https://{MRMS_BUCKET}.s3.amazonaws.com/{key}'
        farms = {}
        for row in cm.scan_all(cm.table('fields')):
            polygon = cm.field_polygon(row)
            swath = read_swath(url,polygon)
            if swath is None: continue
            import numpy as np
            if not np.isfinite(swath[0]).any(): continue
            result = summarize_hail(row['field_id'],valid.date().isoformat(),valid.date().isoformat(),[(valid.date().isoformat(),swath[0])])
            if result.max_hail_mm < result.threshold_mm: continue
            artifact = f"hazards/hail/{valid:%Y%m%dT%H%M%SZ}/{row['farm_id']}/{row['field_id']}/swath"
            save_overlay(artifact,swath,polygon)
            farms.setdefault(row['farm_id'],[]).append((row,result,artifact))
        for farm, hits in farms.items():
            watch_id = 'hail-'+hashlib.sha256((farm+key).encode()).hexdigest()[:24]
            watch = C.Watch(watch_id=watch_id,type='hail',fields=[C.FieldRef(farm_id=farm,field_id=r['field_id']) for r,_,_ in hits],
                opened_at=cm.iso(valid), expires_at=cm.iso(valid+timedelta(days=14)),
                detail=dict(valid_time=cm.iso(valid),max_hail_mm=max(h.max_hail_mm for _,h,_ in hits),
                    fields={r['field_id']:{**h.model_dump(mode='json'),'swath_key':a+'.tif','swath_png_key':a+'.png'} for r,h,a in hits}))
            store_watch(watch); opened += 1
    return opened


def handler(event, context=None):
    if 'Records' not in event or not any('body' in r for r in event['Records']):
        return {'opened':process(event)}
    failures=[]
    for record in event['Records']:
        try: process(json.loads(record['body']))
        except Exception:
            logging.exception('hail delivery failed')
            failures.append({'itemIdentifier':record['messageId']})
    return {'batchItemFailures':failures}
