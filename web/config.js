/* Dev config: the local FastAPI harness (cd agent; python -m uvicorn web.server:app --port 8000), polling only.
   The deployed site ignores this file: FieldWatch-Frontend generates its own config.js. */
window.FIELDWATCH_CONFIG = {
  apiUrl: "http://localhost:8000",
  appsyncHttp: "",
  appsyncRealtime: "",
  appsyncApiKey: "",
};
