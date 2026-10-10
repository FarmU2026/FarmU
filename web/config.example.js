/* Copy to config.js. In a deployed site, FieldWatch-Frontend generates config.js for you from CDK context. */
window.FIELDWATCH_CONFIG = {
  // API Gateway HTTP API base URL, no trailing slash. Pages call <apiUrl>/v1/... and <apiUrl>/api/farms/...
  // An empty string means "same origin as this page".
  apiUrl: "https://abc123.execute-api.us-west-2.amazonaws.com",
  // AppSync Events API: HTTP endpoint (host or URL), realtime endpoint (host or URL) and an API key.
  // Leave appsyncRealtime or appsyncApiKey empty and the dashboard polls every 10 seconds instead.
  appsyncHttp: "abc123.appsync-api.us-west-2.amazonaws.com",
  appsyncRealtime: "abc123.appsync-realtime-api.us-west-2.amazonaws.com",
  appsyncApiKey: "da2-xxxxxxxxxxxxxxxxxxxxxxxxxx",
};
