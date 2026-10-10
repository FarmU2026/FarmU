# FieldWatch: Core Features

Oct 6, 2026 · @Caleb

The farmer never has to open an app: FieldWatch reaches them by text, calls anyone who would rather talk than text, and links to simple web pages for anything visual. The web dashboard is for family, agronomists and the demo stage.

## Principles

Every feature is judged by one question: would a 65-year-old farmer in a tractor cab with one bar of signal actually use it?

- **Meet them where they already are.** Text and phone calls work on any phone, including flip phones, with no install and no password.
- **Voice for anyone who prefers it.** Many farmers are older, and in a noisy cab with gloves on, a call beats reading. Call preference is a per-person setting. Voice is a stretch to build, and nothing else depends on it.
- **Plain words.** Never "NDVI" or "anomaly z-score" in a message. Say "the crop in the northeast corner is struggling."
- **Use their names for things.** Farmers name fields ("the Johnson quarter," "north pivot"); messages use those names, learned and stored in memory.
- **Low bandwidth.** Web pages load fast on a weak rural connection: small images, no heavy app framework, readable without zooming.
- **Respect their time.** Quiet hours, a daily cap on alerts, and silence when nothing is wrong. Crying wolf during harvest is how a farmer stops reading.
- **Humans approve actions.** The system suggests and drafts; it never spends money or contacts a third party without a yes.

## Users and roles

One farm has several people, and each needs a different slice. Farm operations are often a family affair, so who-gets-what is a core feature, not an admin screen.

| Role | Gets | Can do | Channel |
| --- | --- | --- | --- |
| Farmer (owner/operator) | All alerts, weekly digest | Approve actions, add people, set preferences | Text or call, by preference |
| Family delegate (son, daughter, spouse) | Copies of alerts the farmer chooses | Approve if the farmer allows | Text, web dashboard |
| Family member doing repairs | Work orders only, with a map pin | Mark a work order done, send photos | Text |
| Agronomist or crop consultant (optional) | Diagnoses and evidence for the farms they advise | Comment, recommend | Web dashboard, email |

Everyone added gets their own opt-in text before they receive anything.

The demo farm is three generations: a grandfather (the person most likely to prefer a phone call), a father who runs it, and two sons who help with repairs when they're around.

## Feature set

MVP is what the demo needs end to end; should-have makes it feel like a product; stretch is for Saturday night if everything works.

| Area | Feature | What it does | Tier |
| --- | --- | --- | --- |
| Onboarding | Text-to-join | Farmer texts JOIN and an address; gets a map link to tap their fields | MVP |
| Onboarding | Field naming | "What do you call this field?" Names stored and used in every message | Should |
| Onboarding | Call-to-join | Voice agent takes the address and reads the fields back for confirmation | Stretch |
| Monitoring | Satellite watch | Every new clear pass analyzed automatically | MVP |
| Monitoring | Storm heads-up | After radar-detected hail: "I'll check the next clear image" | Should |
| Monitoring | Weekly digest | One short text: all fields normal, or what's being watched | Should |
| Investigation | Multi-source diagnosis | Agents test hypotheses against weather, soil, terrain, hazards | MVP |
| Investigation | Evidence trail | What was checked and ruled out, in plain words | MVP |
| Investigation | Dollar impact | Estimated loss range if unaddressed | MVP |
| Investigation | Photo request | Agent asks for a photo when space can't tell | MVP |
| Conversation | Reply keywords | YES, NO, MORE, CALL, plus STOP and HELP | MVP |
| Conversation | Free-text questions | "Why do you think it's the pivot?" answered by the agent with its tools | Should |
| Actions | Repair work order | Sent to the hired hand with a map pin, after approval | MVP |
| Actions | Scouting pin | Coordinates + what to look for + a navigate link | MVP |
| Actions | Insurance notice draft | Prefilled draft for the farmer to send their agent | Should |
| Preferences | Memory | Quiet hours, call vs. text, who gets copies, learned from replies | Should |
| Voice | Calls by preference | Alerts arrive as a phone call for anyone who prefers calls | Stretch (designed in, not required) |
| Voice | Call anytime | Farmer calls in: "How are my fields?" | Stretch |
| Web | Field report page | The reveal (photo vs. crop health slider), map pin, evidence, approve buttons | MVP |
| Web | Photo upload page | Opens the camera, records location | MVP |
| Web | Dashboard | Farm map, scan sweep, live investigation, phone mirror | MVP (demo surface) |
| Web | Season report | End-of-season summary PDF for a landlord or insurer | Stretch |
| Documents | Soil test upload | Farmer sends a soil test (PDF or a photo of the paper report); values are extracted, confirmed by text, and used as dated evidence. Later: tissue and water tests, application records | Should |
| Web | Agent activity view | Dashboard tab showing every running agent: its field, current step, the images and documents in its context, and its tool-call timeline | Should |

Not building: a native app (install burden for exactly the people least likely to install it) or chat-app integrations.

## Text messaging

Text is the primary channel. Every alert answers five things in under about 300 characters: **what, where, why we think so, what's at stake, what to do.**

Message rules:

- Field names the farmer uses, never IDs.
- One number that matters (dollars or days), not a data dump. Details live behind the link.
- Reply options at the end, in capitals.
- No emoji: one emoji switches the whole text to a different encoding and cuts each segment from 160 to 70 characters.
- At most two alerts a day per farm unless something is urgent; nothing during quiet hours unless urgent.

Reply keywords:

| Reply | Does |
| --- | --- |
| YES / NO | Approve or decline the pending action |
| MORE | Plain-language explanation of the evidence |
| CALL | The voice agent calls them now |
| PHOTO | Sends the photo upload link |
| STOP / HELP | Required opt-out and help responses |
| Anything else | Treated as a question or new information for the agent |

Example texts:

> **Diagnosis + approval:** North pivot: the crop in a ring on the outer edge is stressed. Rain's been normal and it matches the pivot's path, so a nozzle or tower likely isn't watering. At risk if not fixed this week: $9,000–$14,000. Send your son a repair ticket? YES / NO / MORE — see map: \[link\]

> **Photo request:** Something's affecting the SW corner of the Johnson quarter, but I can't tell what from the satellite. Could you snap a leaf near this spot? \[link\]

> **Storm heads-up:** Radar shows hail likely hit the east 80 last night. I'll check the next clear satellite image (likely Thursday) and let you know.

> **Weekly digest:** All 6 fields look normal this week. Watching: east 80 after Monday's storm.

Compliance and setup: US business texting requires registering the sending number (10DLC registration or toll-free verification), documented opt-in, and working STOP/HELP. Registration can take days to weeks, so start it now and build the in-app phone mirror as the guaranteed fallback for the demo. Sending runs through AWS End User Messaging; inbound replies arrive as events that resume the investigation.

## Phone calls

Voice is realistic on AWS now: Nova 2 Sonic is a generally available speech-to-speech model on Bedrock with asynchronous tool calling and direct integrations with Amazon Connect, Twilio and Vonage, and Amazon Connect's AI agents can use Nova Sonic voices in us-east-1 and us-west-2. That makes "the farm calls you" a real feature rather than a mockup.

**When FieldWatch calls**

- The person prefers calls over texts (a per-person setting, chosen at onboarding and changeable anytime).
- A text goes unanswered and the person has asked for a follow-up call in that case (optional).
- The farmer replies CALL, or calls the FieldWatch number themselves.

**Call flow**

1. **Identify:** "Hi, this is FieldWatch calling for Caleb about the farm. Is now an OK time?" ("No" → "I'll text you the details.")
2. **Summarize in about 20 seconds:** what, where, why, what's at stake, in the same plain words as the text.
3. **Offer choices:** "I can send your son a repair ticket, text you the map, or explain more."
4. **Answer questions** with the same agent and tools the text channel uses ("How sure are you it's the pivot?").
5. **Confirm any approval by reading it back:** "To confirm: send your son a repair ticket for the north pivot. Yes?"
6. **Follow up with a text** summarizing the call and linking the field report.

**Voice design for older farmers**

- Slower pace, short sentences, repeat on request ("say 'repeat' anytime").
- Keypad fallback ("press 1 for yes") for noisy cabs.
- Voicemail: a 15-second message plus the text, never the details by voice alone.

**Safety rules**

- Verbal approval only for low-risk actions (work orders, scouting pins); anything involving money or third parties gets confirmed by text too.
- Never asks for passwords, payments or account numbers by phone; say so in onboarding so farmers can spot a scam call pretending to be FieldWatch.
- Respects quiet hours the same way texts do.

**Implementation options**

| Option | How | Trade-off |
| --- | --- | --- |
| Amazon Connect + Nova Sonic | Connect owns the phone number, outbound calls and call flow; its AI agent handles the conversation | Least custom code, most AWS-native; setup has many console steps and outbound calling may need a quota increase |
| Nova 2 Sonic on Bedrock + Twilio | Bridge a Twilio media stream to a Nova Sonic bidirectional stream you host | More code and audio plumbing; more control over the agent and tools |

Voice is a stretch feature: it's in the architecture, but nothing else depends on it. If you want a head start, prototype the Connect path this week as a learning exercise (no app code carried over). Either way, the voice agent should call the same Gateway tools as the text agent so both channels give the same answers.

## Web pages and dashboard

Four lightweight mobile pages open from a text link (no login: each link is a signed, expiring token for one farm and one task), plus one dashboard for desktop.

| Page | Who | What's on it | Tier |
| --- | --- | --- | --- |
| Field report | Farmer, delegates | A slider between the satellite photo and the crop-health view (the reveal); the problem area outlined; a plain-language "what we checked" list; dollar range; big Approve / Decline buttons; a "navigate here" link that opens their maps app | MVP |
| Photo upload | Farmer, hired hand | Opens the camera directly; shows the pin they're near; one button | MVP |
| Onboarding map | New farmer | Field outlines around their address; tap to select; name each field | MVP |
| Work order | Hired hand | Map pin, what to check, "done" button, optional photo | Should |
| Dashboard | Demo, family, agronomist | Farm map with all fields; scan sweep; live investigation with agents and evidence cards appearing; hypothesis list with confidence; phone mirror showing the text thread or call transcript; season timeline | MVP (demo surface) |
| Document upload | Farmer | Same page as photo upload; accepts a PDF or a photo of a paper report; shows which field it applies to | Should |
| Agent view | Judges, agronomists, family, the team while debugging | Agent badges on the map; one lane per agent with field, current step, context thumbnails, tool-call timeline | Should |

Mobile page rules: large type and buttons (body text around 18 px, full-width buttons at least 48 px tall), one primary action per screen, works one-handed, readable in sunlight (high contrast), loads in a few seconds on a weak connection. On screen, say "crop health," "problem area" and "how sure we are," never "NDVI," "anomaly" or "confidence score," and use the same field names everywhere. The dashboard is built for a projector: large type, high contrast, and motion only where it explains something (the sweep, cards arriving).

Dashboard details for the demo:

- **Scan sweep:** a sweep crosses all fields; flagged fields light up with a short label ("ring pattern," "hail swath," "dry streak").
- **Investigation view:** zooms to the field; evidence cards appear as each tool call returns; hypotheses are struck through as they're ruled out.
- **Phone mirror:** the farmer's text thread (or live call transcript) beside the map, so the audience sees the farmer's side and the system's side at once. It doubles as the fallback if real SMS isn't ready.

## Alert escalation

&#91;embedded content: alert delivery · urgency sets timing, preference sets text or call\]

Urgency decides when a message goes out; each person's preference decides whether it's a text or a call. If voice isn't built, everyone follows the text path and nothing else changes.

## What the 3-minute demo shows

The demo touches every MVP channel once, in the order a farmer would experience them.

1. **Story (0:00–0:25):** "This is the farm I grew up on in Thurston County." The dashboard shows the real fields.
2. **Trigger (0:25–0:45):** replay the summer satellite pass; the scan sweep flags three fields with different labels.
3. **Reveal (0:45–1:05):** zoom to the hero field; slide from satellite photo (looks fine) to crop health (the problem appears).
4. **Investigation (1:05–1:40):** evidence cards appear and hypotheses get ruled out live.
5. **Phone moment (1:40–2:15):** the farmer's phone (yours), mirrored on the big screen, buzzes with the text; you reply YES; the repair ticket lands on a teammate's phone playing the son.
6. **Photo beat (2:15–2:40, if time):** the agent asks for a leaf photo on a second field; a teammate uploads a reference image of a known leaf disease; the diagnosis updates.
7. **Close (2:40–3:00):** dollar figure, "free public data, no app to install," and one sentence on the AWS architecture.

If voice works, add one beat: the grandfather's alert arrives as a phone call, played on speaker. It lands the point about older farmers without saying it.

## Open questions to settle this week

- [ ] Voice in or out of the MVP? Decide by Thursday after a Connect + Nova Sonic prototype. \[It's a stretch feature. Include it in the designs and architecture, but the system shouldn't require it to function.\]
- [ ] Will SMS registration clear before Saturday? If not, the phone mirror carries the demo and a judge's phone isn't used. \[We'll just use my and my teammate's phones so we don't have to worry about this.\]
- [ ] Which field names does your family actually use? Use them in the demo texts.
- [ ] Who is "Mike"? Pick a real role on the farm for the work-order recipient (with their OK) or use a clearly fictional name. \[Our farm is family only, it's my grandfather, my dad, and my brother and I help out when we're around.\]
- [ ] Can you get a real affected leaf from the farm for the photo beat, plus a printed backup? \[Probably not. We could just find images of certain diseases or deficiencies online and use them in the demo. I did the agronomy contest for FFA in high school so I know these images exist. Do we have or need an ML model for these as well, even just an open source one?\]
- [ ] Urgent-call threshold: what dollar amount or time sensitivity justifies calling instead of texting? \[I think this is more of a feature for old-school people who prefer a nice phone call over impersonal messages, if that makes sense.\]

## Sources

- [Introducing Amazon Nova 2 Sonic (AWS News Blog)](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-2-sonic-next-generation-speech-to-speech-model-for-conversational-ai)
- [Amazon Connect agentic self-service with Nova Sonic (AWS What's New)](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-connect-agentic-self-service)
