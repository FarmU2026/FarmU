# Live feed (WS13, T28)

EventBridge bus `fieldwatch` -> **projector Lambda** -> AppSync Events -> dashboard and phone panel. The projector is the only publisher.

| File | What |
| --- | --- |
| `mapping.py` | Pure: bus event -> `FeedMessage`s + thread items. Channel names live here. |
| `publisher.py` | SigV4 (`appsync`) POST to the Events HTTP endpoint, injectable transport, batches of 5. |
| `handler.py` | Lambda entry `cloud.feed.handler.handler`: map, write thread items, publish. |
| `subscriber.html` / `subscriber.mjs` | Test subscribers (API key). |
| `send_sample.py` | Puts sample events on the bus (real AWS; Caleb only). |

## Channels (AppSync namespace `fieldwatch`)

- `/fieldwatch/farms/{farm_id}/feed`: everything (dashboard).
- `/fieldwatch/farms/{farm_id}/phone`: only messages whose payload has `kind` (message_out, message_in, investigation_step from the agent, concluded, action_created), i.e. exactly what is also stored as thread history. Those payloads carry `who`, `kind`, `text`, so the phone panel can render them directly.

## Event -> FeedMessage

| Event | Feed messages (`backend: "cloud"`) |
| --- | --- |
| SceneAnalyzed | `scene_analyzed`, `field_status` (`ok` / `too_cloudy` / ...) |
| AnomalyDetected | `field_status` (`status: "anomaly"`, regions with geometry, overlays, `replay`) |
| WatchOpened | one `field_status` (`status: "watching"`) per field |
| InvestigationProgress | `investigation_step`, plus `evidence_added` and/or `hypotheses_updated` when present |
| InvestigationConcluded | `concluded` |
| ReplyReceived | `investigation_step` with `step: "reply"` (feed channel only; the text itself comes as MessageReceived) |
| ApprovalRecorded | `action_created` with `status: approved` or `declined` |
| ActionCompleted | `action_created` with `status: done` |
| MessageSent / MessageReceived | `message_out` / `message_in` |

Every payload has `event_id` (stable per source event) so clients can drop duplicate deliveries. Farm id comes from the event, or from `fw-investigations` (`farm_id` or `field.farm_id`) for investigation/approval/action events; if that item does not exist yet the Lambda raises and EventBridge retries (3 attempts, then the DLQ).

## Thread history (fw-farms)

For message_out, message_in, investigation_step (not the reply step), concluded and action_created the projector writes `{farm_id, item: "THREAD#<occurred_at>#<id>", who, text, kind, at, investigation_id?, field_id?}`.
`who` is `fieldwatch`, `farmer` or `agent`; `kind` is `message`, `step`, `concluded` or `action`. Ids are natural per event (message_id, `<investigation_id>#<seq>`, `<investigation_id>#concluded`, `<approval_id>#approval`, `<action_id>#done`), so a redelivery overwrites the same row. Query: `farm_id = X and begins_with(item, "THREAD#<today>")`.

## Try it

After `cdk deploy FieldWatch-Feed` (outputs `FeedHttpEndpoint`, `FeedRealtimeEndpoint`, `FeedApiKey`):

```bash
node subscriber.mjs <FeedHttpEndpoint> <FeedRealtimeEndpoint> <FeedApiKey> /fieldwatch/farms/wohleb-farm/feed
# or open subscriber.html in a browser and paste the three values
cd agent && python -m cloud.feed.send_sample --farm wohleb-farm --seed-investigation   # second terminal
```

Messages should appear within a second or two. Use channel `.../phone` to see the phone mirror.
