# notify/ (WS9 notifications, Kurt)

Grows out of `fieldwatch/sms.py` (move it here; keep `send()` and the inbound path working for the web app).
Contracts: `SendMessageRequest/Result`, `InboundMessage`, `MessageSent/Received`.
Rules: opt-in per person, STOP/START/HELP, quiet hours with deferral, daily cap, idempotency keys,
people with roles so work orders reach the repairs person, reply intents matched to the message they answer.
Providers: Twilio (primary), TextBelt, SNS, none; End User Messaging as a stub until a number is registered.
