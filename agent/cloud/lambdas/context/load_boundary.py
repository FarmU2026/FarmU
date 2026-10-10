"""Stage 1: LoadBoundary. Normalize the drawn polygon, find the Sentinel-2 tiles, mark the field `building`.

Input (from the Map): {schema_version, farm_id, field_id}. The polygon is read from the fw-fields item
(attribute `polygon`, [[lon, lat], ...], written inline by the web API).
Writes: fw-fields (status=building, acres, centroid, bbox, tile_ids, county_fips, interior_polygon, and
`polygon` only when the drawn ring had to be repaired), fw-tile-index rows {tile_id, field_id, farm_id}.
Returns the `boundary` object the stage Lambdas receive.
"""
from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from . import geo_lookup
from .common import (FIELDS_TABLE, SCHEMA_VERSION, TILE_INDEX_TABLE, BoundaryError, normalize_crop, table,
                     to_ddb)
from .geometry import normalize_polygon

log = logging.getLogger(__name__)


def handler(event, _context=None):
    if str(event.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
        raise BoundaryError(f"unsupported schema_version {event.get('schema_version')!r}")
    farm_id, field_id = event["farm_id"], event["field_id"]

    fields = table(FIELDS_TABLE)
    item = fields.get_item(Key={"farm_id": farm_id, "field_id": field_id}).get("Item")
    if not item:
        raise BoundaryError(f"field {farm_id}/{field_id} not found in fw-fields")
    if "polygon" not in item:
        raise BoundaryError(f"field {farm_id}/{field_id} has no polygon")

    nf = normalize_polygon(item["polygon"])
    lon, lat = nf.centroid
    tile_ids = geo_lookup.tile_ids_for(nf.geojson())
    county_fips = item.get("county_fips") or geo_lookup.county_lookup(lon, lat)

    # Tile index: add this field's rows, drop rows for tiles it no longer touches (redrawn field).
    stale = set(item.get("tile_ids") or []) - set(tile_ids)
    with table(TILE_INDEX_TABLE).batch_writer() as batch:
        for tile_id in tile_ids:
            batch.put_item(Item={"tile_id": tile_id, "field_id": field_id, "farm_id": farm_id})
        for tile_id in stale:
            batch.delete_item(Key={"tile_id": tile_id, "field_id": field_id})

    names = {"#status": "status"}
    values = {":status": "building", ":acres": nf.acres, ":centroid": {"lon": lon, "lat": lat},
              ":bbox": list(nf.bbox), ":tile_ids": tile_ids}
    expr = ["#status = :status", "acres = :acres", "centroid = :centroid", "bbox = :bbox", "tile_ids = :tile_ids"]
    for attr, val in (("county_fips", county_fips), ("interior_polygon", nf.interior_polygon),
                      ("polygon", nf.polygon if nf.repaired else None)):
        if val is not None:
            expr.append(f"{attr} = :{attr}")
            values[f":{attr}"] = val
    try:
        fields.update_item(
            Key={"farm_id": farm_id, "field_id": field_id},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=to_ddb(values),
            ConditionExpression="attribute_exists(field_id)",
        )
    except ClientError as exc:
        raise BoundaryError(f"could not update fw-fields: {exc}") from exc

    log.info("loaded boundary %s/%s: %.1f acres, tiles %s, county %s", farm_id, field_id, nf.acres, tile_ids, county_fips)
    return {
        "acres": nf.acres,
        "centroid": {"lon": lon, "lat": lat},
        "bbox": list(nf.bbox),
        "tile_ids": tile_ids,
        "county_fips": county_fips,
        "crop": normalize_crop(item.get("crop")),
        "polygon": nf.polygon,
        "repaired": nf.repaired,
    }
