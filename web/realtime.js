/* AppSync Events subscriber (WebSocket, API-key auth), no libraries.

   Protocol (AWS docs, "Event API WebSocket protocol"):
     - open  wss://<id>.appsync-realtime-api.<region>.amazonaws.com/event/realtime
       with subprotocols ["aws-appsync-event-ws", "header-<base64url(JSON auth header)>"]
     - send  {"type":"connection_init"}            -> {"type":"connection_ack","connectionTimeoutMs":N}
     - send  {"type":"subscribe","id","channel","authorization":<auth header>}
                                                   -> subscribe_success | subscribe_error
     - recv  {"type":"data","id","event":"<json string>"}; {"type":"ka"} keep-alives
   The auth header for an API key is {"host": <HTTP endpoint host>, "x-api-key": <key>}.

   FW.Realtime({realtimeUrl, httpUrl, apiKey, channels, onMessage, onStatus}) reconnects with backoff.
   onStatus gets "connecting" | "live" | "down". "live" means every channel subscribed. */
(function () {
  "use strict";

  function b64url(obj) {
    return btoa(unescape(encodeURIComponent(JSON.stringify(obj)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // Accept "abc.appsync-api.us-west-2.amazonaws.com", "https://abc.../event" or "wss://abc.../event/realtime".
  function hostAndPath(v) {
    const s = String(v || "").trim().replace(/^[a-z]+:\/\//i, "");
    const i = s.indexOf("/");
    return i < 0 ? { host: s, path: "" } : { host: s.slice(0, i), path: s.slice(i) };
  }

  function endpoints(realtimeUrl, httpUrl) {
    const rt = hostAndPath(realtimeUrl);
    let http = hostAndPath(httpUrl);
    if (!http.host) http = { host: rt.host.replace("appsync-realtime-api", "appsync-api"), path: "" };
    return { wsUrl: "wss://" + rt.host + (rt.path && rt.path !== "/" ? rt.path : "/event/realtime"), httpHost: http.host };
  }

  function Realtime(opts) {
    const ep = endpoints(opts.realtimeUrl, opts.httpUrl);
    const auth = { host: ep.httpHost, "x-api-key": opts.apiKey };
    const channels = opts.channels;
    let ws = null, stopped = false, backoff = 1000, watchdog = null, timeoutMs = 300000, subscribed = 0, timer = null;
    const emit = (s) => { try { opts.onStatus && opts.onStatus(s); } catch (e) { /* ignore */ } };

    function armWatchdog() {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => { try { ws && ws.close(); } catch (e) { /* ignore */ } }, timeoutMs + 10000);
    }

    function connect() {
      if (stopped) return;
      subscribed = 0;
      emit("connecting");
      try {
        ws = new WebSocket(ep.wsUrl, ["aws-appsync-event-ws", "header-" + b64url(auth)]);
      } catch (e) { return scheduleRetry(); }
      const mine = ws;
      mine.onopen = () => mine.send(JSON.stringify({ type: "connection_init" }));
      mine.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        armWatchdog();
        switch (m.type) {
          case "connection_ack":
            if (m.connectionTimeoutMs) timeoutMs = m.connectionTimeoutMs;
            armWatchdog();
            channels.forEach((ch, i) => mine.send(JSON.stringify({ type: "subscribe", id: "fw-" + i, channel: ch, authorization: auth })));
            break;
          case "subscribe_success":
            if (++subscribed >= channels.length) { backoff = 1000; emit("live"); }
            break;
          case "subscribe_error":
          case "connection_error":
          case "error":
            try { mine.close(); } catch (e) { /* ignore */ }
            break;
          case "data": {
            let ev2 = m.event;
            if (typeof ev2 === "string") { try { ev2 = JSON.parse(ev2); } catch (e) { return; } }
            if (Array.isArray(ev2)) ev2.forEach((x) => opts.onMessage(typeof x === "string" ? safeParse(x) : x));
            else opts.onMessage(ev2);
            break;
          }
          default: break; // "ka" and anything new
        }
      };
      mine.onerror = () => { /* onclose follows */ };
      mine.onclose = () => { if (mine === ws) { clearTimeout(watchdog); emit("down"); scheduleRetry(); } };
    }

    function safeParse(s) { try { return JSON.parse(s); } catch (e) { return null; } }

    function scheduleRetry() {
      if (stopped) return;
      clearTimeout(timer);
      timer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    }

    connect();
    return { close() { stopped = true; clearTimeout(timer); clearTimeout(watchdog); try { ws && ws.close(); } catch (e) { /* ignore */ } } };
  }

  window.FW = window.FW || {};
  window.FW.Realtime = Realtime;
  window.FW._rt = { b64url, endpoints };
})();
