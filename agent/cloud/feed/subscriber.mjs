// Prints live FieldWatch feed messages. Node 22+ (global WebSocket), no dependencies.
//   node subscriber.mjs <http-endpoint> <realtime-endpoint> <api-key> [channel]
// e.g. node subscriber.mjs https://xxxx.appsync-api.us-west-2.amazonaws.com/event \
//        wss://xxxx.appsync-realtime-api.us-west-2.amazonaws.com/event/realtime da2-xxxx /fieldwatch/farms/wohleb-farm/feed
const [httpEndpoint, realtime, apiKey, channel = "/fieldwatch/farms/wohleb-farm/feed"] = process.argv.slice(2);
if (!httpEndpoint || !realtime || !apiKey) {
  console.error("usage: node subscriber.mjs <http-endpoint> <realtime-endpoint> <api-key> [channel]");
  process.exit(2);
}
const auth = { host: new URL(httpEndpoint).host, "x-api-key": apiKey };
const header = Buffer.from(JSON.stringify(auth)).toString("base64url");
const ws = new WebSocket(realtime, ["aws-appsync-event-ws", `header-${header}`]);
ws.onopen = () => ws.send(JSON.stringify({ type: "connection_init" }));
ws.onerror = (e) => console.error("websocket error", e.message ?? e);
ws.onclose = () => { console.log("closed"); process.exit(0); };
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === "connection_ack") {
    ws.send(JSON.stringify({ type: "subscribe", id: crypto.randomUUID(), channel, authorization: auth }));
  } else if (m.type === "subscribe_success") {
    console.log(`subscribed to ${channel}; waiting for messages (Ctrl+C to stop)`);
  } else if (m.type === "data") {
    console.log(new Date().toISOString(), m.event);
  } else if (m.type !== "ka") {
    console.log(JSON.stringify(m));
  }
};
