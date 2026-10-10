"""Field lookup adapter; pure hazard calculations stay free of AWS imports."""
from fieldwatch import config


def field_polygon(field_id):
    if config.TOOLS_BACKEND == 'gateway' or config.BUCKET:
        from cloud.lambdas.hazards import common as cm
        rows = cm.query_all(cm.table('fields'), IndexName='by-field', KeyConditionExpression='field_id = :f', ExpressionAttributeValues={':f':field_id})
        if len(rows) != 1: raise ValueError(f'field lookup expected one result for {field_id}')
        return cm.field_polygon(rows[0])
    import json
    farm = json.loads(config.FARM_FILE.read_text())
    field = next((f for f in farm['fields'] if f.get('id',f.get('field_id')) == field_id or f.get('name','').lower() == field_id.lower()), None)
    if not field: raise ValueError(f'unknown field {field_id}')
    return field['polygon']
