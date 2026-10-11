/* Field editing client. One small module so the endpoints live in one place.
   The deployed API (agent/cloud/api/routes.py) only has POST /api/farms (which replaces the whole field list on sign-up),
   GET /api/farms/{id}, /check, /fields/{fid}/ask, /reply and the image route. These three REST routes do not exist yet:
     POST   /api/farms/{farm_id}/fields                 {name, crop, polygon:[[lat,lon],...]}     -> farm payload (or the new field)
     PATCH  /api/farms/{farm_id}/fields/{field_id}      {name?, crop?, polygon?:[[lat,lon],...]}  -> farm payload (or the field)
     DELETE /api/farms/{farm_id}/fields/{field_id}                                                -> 204 / farm payload
   Polygons are sent as [lat, lon] pairs, the same as POST /api/farms. On 404/405/501 we throw an error with
   code "UNAVAILABLE" so the UI can show one calm line instead of breaking. */
(function () {
  "use strict";
  const { api } = FW;
  const base = (id) => `/api/farms/${encodeURIComponent(id)}/fields`;
  const MSG = "Editing fields isn't available yet. Your fields are safe and still being watched.";

  async function call(path, opts) {
    try {
      return await api(path, opts);
    } catch (e) {
      if (e && (e.status === 404 || e.status === 405 || e.status === 501)) {
        const u = new Error(MSG); u.code = "UNAVAILABLE"; u.status = e.status; throw u;
      }
      throw e;
    }
  }

  FW.fieldsApi = {
    MSG,
    add: (farmId, f) => call(base(farmId), { method: "POST", body: { name: f.name, crop: f.crop, polygon: f.polygon } }),
    update: (farmId, fid, patch) => call(`${base(farmId)}/${encodeURIComponent(fid)}`, { method: "PATCH", body: patch }),
    remove: (farmId, fid) => call(`${base(farmId)}/${encodeURIComponent(fid)}`, { method: "DELETE" }),
  };
})();
