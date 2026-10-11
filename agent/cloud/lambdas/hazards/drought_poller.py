"""Weekly as-of drought context update and watches on changes to D2 or worse."""
from datetime import timedelta
import hashlib
from fieldwatch import contracts as C
from fieldwatch.hazards import get_drought
from . import common as cm
from .hail_filter import store_watch
from botocore.exceptions import ClientError


def handler(event, context=None):
    as_of = event.get('as_of',cm.now().date().isoformat())
    fields = cm.scan_all(cm.table('fields'))
    farms = {r['farm_id']:r for r in cm.scan_all(cm.table('farms')) if r.get('county_fips')}
    grouped={}
    for field in fields:
        context_county = cm.table('field-context').get_item(Key={'field_id':field['field_id']}).get('Item',{}).get('county_fips')
        county = field.get('county_fips') or context_county or farms.get(field['farm_id'],{}).get('county_fips')
        if county: grouped.setdefault(str(county).zfill(5),[]).append(field)
    updated, opened = 0,0
    for county, rows in grouped.items():
        status = get_drought(str(county),as_of)
        previous = []
        updates = []
        for row in rows:
            old = cm.table('field-context').get_item(Key={'field_id':row['field_id']},ConsistentRead=True).get('Item',{})
            # A historical poll must never overwrite newer live context.
            if old.get('drought',{}).get('as_of','') > status.as_of: continue
            updates.append(row)
            if status.category in ('D2','D3','D4') and old.get('drought',{}).get('category') != status.category:
                previous.append(row)
        by_farm={}
        for row in previous: by_farm.setdefault(row['farm_id'],[]).append(row)
        for farm, affected in by_farm.items():
            valid = cm.parse_time(status.as_of)
            watch = C.Watch(watch_id='drought-'+hashlib.sha256((farm+county+status.as_of+status.category).encode()).hexdigest()[:24],
                type='drought',fields=[C.FieldRef(farm_id=farm,field_id=r['field_id']) for r in affected],
                detail=status.model_dump(mode='json'),opened_at=cm.iso(valid),expires_at=cm.iso(valid+timedelta(days=14)))
            if store_watch(watch): opened += 1
        for row in updates:
            try:
                cm.table('field-context').update_item(Key={'field_id':row['field_id']},
                    UpdateExpression='SET drought = :d',
                    ConditionExpression='attribute_not_exists(drought.#date) OR drought.#date <= :date',
                    ExpressionAttributeNames={'#date':'as_of'},
                    ExpressionAttributeValues={':d':cm.to_ddb(status.model_dump(mode='json')),':date':status.as_of})
                updated += 1
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
    return {'updated':updated,'opened':opened}
