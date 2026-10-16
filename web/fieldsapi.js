/* Field editing client. One small module so the endpoints live in one place.
     POST   /api/farms/{farm_id}/fields                 {name, crop, polygon:[[lat,lon],...]}     -> farm payload
     PATCH  /api/farms/{farm_id}/fields/{field_id}      {name?, crop?, polygon?:[[lat,lon],...], planting_date?: "YYYY-MM-DD" | null}  -> farm payload
     DELETE /api/farms/{farm_id}/fields/{field_id}                                                -> farm payload
   Polygons are sent as [lat, lon] pairs, the same as POST /api/farms. Every call resolves to a FULL farm payload (the
   same shape as GET /api/farms/{id}): if the server answers with just the field, or with nothing (204), we GET the
   farm again. Only a genuinely missing route (405, 501, or a bare "Not Found" 404) becomes code "UNAVAILABLE" so the
   UI can show one calm line; a 404 that names the farm or field is a real error and passes through. */
(function () {
  "use strict";
  const { api } = FW;
  const farmPath = (id) => `/api/farms/${encodeURIComponent(id)}`;
  const base = (id) => `${farmPath(id)}/fields`;
  const MSG = "Editing fields isn't available yet. Your fields are safe and still being watched.";

  const routeMissing = (e) => e && (e.status === 405 || e.status === 501 ||
    (e.status === 404 && /^HTTP_404$/.test(e.code || "") && /^\s*(not found|missing authentication token)?\s*$/i.test(e.message || "")));

  async function call(farmId, path, opts) {
    let res;
    try {
      res = await api(path, opts);
    } catch (e) {
      if (routeMissing(e)) { const u = new Error(MSG); u.code = "UNAVAILABLE"; u.status = e.status; throw u; }
      throw e;
    }
    if (res && Array.isArray(res.fields)) return res;
    return api(farmPath(farmId));
  }

  FW.fieldsApi = {
    MSG,
    add: (farmId, f) => call(farmId, base(farmId), { method: "POST", body: { name: f.name, crop: f.crop, polygon: f.polygon, planting_date: f.planting_date || null } }),
    update: (farmId, fid, patch) => call(farmId, `${base(farmId)}/${encodeURIComponent(fid)}`, { method: "PATCH", body: patch }),
    remove: (farmId, fid) => call(farmId, `${base(farmId)}/${encodeURIComponent(fid)}`, { method: "DELETE" }),
  };
})();
