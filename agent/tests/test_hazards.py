from datetime import datetime, timezone
import numpy as np
import pytest

from fieldwatch.hazards import drought_status, summarize_hail, nearby_reports, get_warnings
from cloud.lambdas.hazards import hail_filter

POLYGON = [[-97,42],[-96.99,42],[-96.99,42.01],[-97,42.01]]


def test_mesh_mm_nodata_and_union_area():
    result = summarize_hail('field', '2026-07-01', '2026-07-02', [
        ('2026-07-01', np.array([[-1, 30], [0, 0]], dtype=float)),
        ('2026-07-02', np.array([[-1, 0], [40, 0]], dtype=float))])
    assert result.max_hail_mm == 40
    assert result.pct_area_over_threshold == pytest.approx(2/3)
    assert result.peak_date == '2026-07-02'


def test_drought_cumulative_majority_and_no_future():
    rows = [dict(MapDate='20260707', None_=0, D0=100, D1=60, D2=20, D3=0, D4=0),
            dict(MapDate='20260721', D0=100, D1=100, D2=100, D3=100, D4=100)]
    result = drought_status('31173', '2026-07-14', rows)
    assert result.category == 'D1' and result.as_of == '2026-07-07'
    assert result.pct_by_category['D1'] == 40
    assert result.pct_by_category['D0'] == 40
    with pytest.raises(ValueError):
        drought_status('31173', '2026-06-01', rows)


def test_spc_distance_to_boundary():
    reports = [dict(Lon=-96.99, Lat=42, Size=100), dict(Lon=-95, Lat=42, Size=400)]
    out = nearby_reports(POLYGON, reports)
    assert len(out) == 1 and out[0]['size_mm'] == 25.4


class Response:
    def raise_for_status(self): pass
    def json(self):
        return {'features': [dict(type='Feature', geometry=dict(type='Polygon', coordinates=[POLYGON]),
                                 properties=dict(issue='2026-07-14T12:00:00Z'))]}


def test_historical_warnings_uses_archive():
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs)); return Response()
    result = get_warnings(POLYGON, '2026-07-14', '2026-07-14', http_get=get)
    assert len(result) == 1
    assert 'mesonet' in calls[0][0]
    assert calls[0][1]['params']['ets'] == '2026-07-15T00:00:00Z'


def test_sqs_failures_and_irrelevant_product(monkeypatch):
    seen = []
    def process(event):
        seen.append(event)
        if event.get('key') == 'bad': raise RuntimeError('retry')
    monkeypatch.setattr(hail_filter, 'process', process)
    out = hail_filter.handler({'Records': [dict(messageId='a', body='{"key":"bad"}'),
                                            dict(messageId='b', body='{"key":"good"}')]})
    assert out == {'batchItemFailures': [{'itemIdentifier':'a'}]}
    assert len(seen) == 2


def test_missing_mesh_is_not_zero_hail():
    with pytest.raises(ValueError, match='no valid'):
        summarize_hail('f','2026-07-01','2026-07-01', [('2026-07-01',np.array([[-1]]))])


def test_mesh_georeferencing_masks_outside_field(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    from fieldwatch.hazards import read_mesh
    path=tmp_path/'mesh.tif'
    with rasterio.open(path,'w',driver='GTiff',width=4,height=4,count=1,dtype='float32',crs='EPSG:4326',transform=from_origin(-97,42.04,.01,.01)) as dst:
        dst.write(np.array([[100,100,100,100],[100,-1,30,100],[100,0,40,100],[100,100,100,100]],dtype='float32'),1)
    poly=[[-96.989,42.011],[-96.971,42.011],[-96.971,42.029],[-96.989,42.029]]
    values=read_mesh(str(path),poly)
    assert values.shape == (2,2)
    assert np.isnan(values[0,0]) and np.nanmax(values)==40


class Table:
    def __init__(self,rows=()): self.rows={}; self.initial=list(rows)
    def scan(self,**kwargs): return {'Items':self.initial}
    def get_item(self,Key,**kwargs): return {'Item':self.rows.get(tuple(sorted(Key.items())),{})}
    def put_item(self,Item,**kwargs):
        key={'field_id':Item['field_id'],'opened_at_watch_id':Item['opened_at_watch_id']}
        self.rows[tuple(sorted(key.items()))]=Item
    def update_item(self,Key,ExpressionAttributeValues,**kwargs):
        row=self.rows.setdefault(tuple(sorted(Key.items())),{})
        row['event_emitted' if ':yes' in ExpressionAttributeValues else 'drought'] = ExpressionAttributeValues.get(':yes',ExpressionAttributeValues.get(':d'))


def test_hail_watch_redelivery_and_failed_emit(monkeypatch):
    cm=hail_filter.cm
    fields=Table([dict(farm_id='farm',field_id='field',polygon=POLYGON)])
    watches=Table()
    monkeypatch.setattr(cm,'table',lambda name: {'fields':fields,'watches':watches}[name])
    monkeypatch.setattr(hail_filter,'read_swath',lambda *a:(np.array([[30.,0.]]),None,None))
    monkeypatch.setattr(hail_filter,'save_overlay',lambda *a:None)
    events=[]
    def fail_once(event,**kwargs):
        events.append(event)
        if len(events)==1: raise RuntimeError('events unavailable')
    monkeypatch.setattr(cm,'emit',fail_once)
    event=dict(bucket='noaa-mrms-pds',key='CONUS/MESH_Max_60min_00.50/20260714/MRMS_MESH_Max_60min_00.50_20260714-180000.grib2.gz')
    with pytest.raises(RuntimeError): hail_filter.process(event)
    hail_filter.process(event); hail_filter.process(event)
    assert len(watches.rows)==1 and len(events)==2
    watch=events[-1].watch
    assert watch.detail['max_hail_mm']==30 and watch.opened_at=='2026-07-14T18:00:00Z'
    assert next(iter(watches.rows.values()))['expires_at']==int(datetime(2026,7,28,18,tzinfo=timezone.utc).timestamp())
    assert hail_filter.process(dict(key='CONUS/Other/foo'))==0


def test_sns_s3_and_replay_notifications():
    import json
    key='CONUS/MESH_Max_60min_00.50/a+b'
    event={'Message':json.dumps({'Records':[{'s3':{'bucket':{'name':'noaa-mrms-pds'},'object':{'key':key}}}]})}
    assert hail_filter.notifications(event)[0]['key']==key.replace('+',' ')
    assert hail_filter.notifications({'detail':{'key':'replay'}})==[{'key':'replay'}]


def test_drought_poller_preserves_retry_and_newer_context(monkeypatch):
    from cloud.lambdas.hazards import drought_poller as poll
    fields=Table([dict(farm_id='farm',field_id='f',county_fips='31173')])
    context=Table(); farms=Table()
    monkeypatch.setattr(poll.cm,'table',lambda name:{'fields':fields,'farms':farms,'field-context':context}[name])
    status=drought_status('31173','2026-07-14',[dict(MapDate='20260714',D0=100,D1=90,D2=70,D3=0,D4=0)])
    monkeypatch.setattr(poll,'get_drought',lambda *a:status)
    attempts=[]
    def store(watch):
        attempts.append(watch)
        if len(attempts)==1: raise RuntimeError('retry')
        return True
    monkeypatch.setattr(poll,'store_watch',store)
    with pytest.raises(RuntimeError): poll.handler({'as_of':'2026-07-14'})
    assert not context.rows
    assert poll.handler({'as_of':'2026-07-14'})=={'updated':1,'opened':1}
    assert poll.handler({'as_of':'2026-07-14'})['opened']==0
    context.rows[tuple(sorted({'field_id':'f'}.items()))]['drought']['as_of']='2026-08-01'
    assert poll.handler({'as_of':'2026-07-14'})=={'updated':0,'opened':0}


def test_spc_unknown_and_malformed_reports():
    out=nearby_reports(POLYGON,[dict(Lon=-97,Lat=42,Size='UNK'),dict(Lon='bad',Lat=42,Size=100)])
    assert len(out)==1 and out[0]['size_mm'] is None


def test_spc_operational_day():
    from fieldwatch.hazards import spc_reports
    from datetime import date
    class SPCResponse:
        status_code=200
        text='Time,Size,Location,County,State,Lat,Lon,Comments\n0100,100,Here,County,NE,42,-97,Overnight\n1300,200,Here,County,NE,42,-97,Afternoon\n'
        def raise_for_status(self): pass
    reports=spc_reports(date(2026,7,13),http_get=lambda *a,**k:SPCResponse())
    assert [r['date'] for r in reports]==['2026-07-14','2026-07-13']


def test_closed_watch_redelivery_preserves_state(monkeypatch):
    from fieldwatch.contracts import Watch, FieldRef
    watch=Watch(watch_id='w',type='hail',fields=[FieldRef(farm_id='farm',field_id='f')],detail={},opened_at='2026-07-14T00:00:00Z',expires_at='2026-07-28T00:00:00Z')
    watches=Table(); watches.put_item(Item={'field_id':'f','opened_at_watch_id':watch.opened_at+'#w','status':'closed'})
    monkeypatch.setattr(hail_filter.cm,'table',lambda name:watches)
    monkeypatch.setattr(hail_filter.cm,'emit',lambda *a,**k:None)
    hail_filter.store_watch(watch)
    assert next(iter(watches.rows.values()))['status']=='closed'


def test_context_conditional_race_does_not_overwrite(monkeypatch):
    from cloud.lambdas.hazards import drought_poller as poll
    from botocore.exceptions import ClientError
    fields=Table([dict(farm_id='farm',field_id='f',county_fips='31173')]); context=Table()
    seen=[]
    def update(**kwargs):
        seen.append(kwargs)
        raise ClientError({'Error':{'Code':'ConditionalCheckFailedException'}},'UpdateItem')
    context.update_item=update
    monkeypatch.setattr(poll.cm,'table',lambda name:{'fields':fields,'farms':Table(),'field-context':context}[name])
    monkeypatch.setattr(poll,'get_drought',lambda *a:drought_status('31173','2026-07-14',[dict(MapDate='20260714',D0=100,D1=60,D2=0,D3=0,D4=0)]))
    assert poll.handler({'as_of':'2026-07-14'})['updated']==0
    assert 'drought.#date <= :date' in seen[0]['ConditionExpression']
