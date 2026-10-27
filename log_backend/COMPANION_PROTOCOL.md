# Carter Companion protocol v1

Windows detects encounter boundaries and observed mechanics locally. Carter on
Linux resolves trusted phrases and speaks in its already connected Discord voice
channel. Full combat-log uploads are optional and independent. No predicted speech
or additional Discord bot is part of this protocol.

## Public delivery endpoint

`POST /api/v1/live/events`, HTTPS, with these headers:

```text
X-Carter-Guild-ID: <Discord guild ID>
X-Carter-Live-Token: <that guild's existing live token>
Content-Type: application/json
Content-Length: <encoded JSON byte count>
```

Maximum request size: **65,536 bytes**, including JSON; **1–100 events**.
Chunked transfer is unsupported. Do not follow redirects with the token.
The receiver authenticates before provisioning guild resources or reading events.
A guild's token cannot deliver events to another guild.

Validate requests against `companion_events.schema.json` (JSON Schema 2020-12).
All objects reject additional properties. Duplicate JSON keys, NaN and Infinity
are rejected. A malformed event rejects the entire envelope with HTTP 400 before
any event is applied. Per-event semantic failures use HTTP 200 and result statuses.

```json
{
  "schema_version": 1,
  "events": [
    {
      "event_id": "11111111-1111-4111-8111-111111111111",
      "session_id": "22222222-2222-4222-8222-222222222222",
      "attempt_id": "33333333-3333-4333-8333-333333333333",
      "sequence": 1,
      "type": "ENCOUNTER_STARTED",
      "encounter_id": 3470,
      "difficulty_id": 17,
      "log_timestamp": "2026-10-27T05:27:02.031Z",
      "detected_at": "2026-10-27T05:27:02.060Z"
    }
  ]
}
```

Identifiers are lowercase hyphenated UUIDs (36 characters). Use UUID4 for new
identifiers. Each companion run has one `session_id`; each pull has one
`attempt_id`; each event has one `event_id`. Sequence is a positive integer that
increases across the session, not just the pull. Integer fields are bounded by
9,007,199,254,740,991. Persist pending payloads unchanged across reconnects: do
not rewrite IDs, session, sequence, timestamps or profile revision on retries.

Timestamps must be UTC RFC3339, with `Z` or `+00:00`, and zero through six fractional
digits. Preserve the original combat-log timestamp and correctly apply the WoW
timestamp's timezone offset. `detected_at` records local detection, not upload.
Detection cannot predate the log event. Neither timestamp may be over two seconds
ahead of the server clock. Synchronize both machines' clocks. Original log time
determines freshness; receipt time never makes old events fresh.

## Event fields

All events require exactly the common fields shown above. Additional fields are:

| Type | Additional required fields |
| --- | --- |
| `ENCOUNTER_STARTED` | None |
| `MECHANIC_OBSERVED` | `profile_id`, `profile_revision`, `mechanic_id`, `occurrence` |
| `ENCOUNTER_ENDED` | `result`, either `KILL` or `WIPE` |

For a mechanic, `profile_id` matches `^[a-z0-9][a-z0-9._-]{0,79}$` and
`mechanic_id` matches `^[a-z0-9][a-z0-9_-]{0,79}$`. Revision and occurrence are
positive integers. Occurrence increases separately for each mechanic within a
pull. Example additional fields:

```json
{
  "profile_id": "nekzali-observed-v1",
  "profile_revision": 1,
  "mechanic_id": "hungering_pyre",
  "occurrence": 2
}
```

The receiver requires an exact encounter/difficulty/profile/revision/mechanic
match and resolves the phrase from its own trusted catalog. Never send `text`,
`speech`, `message`, spell evidence fields, boss names, or arbitrary metadata.
Unknown bosses still support start/end announcements; without a matching profile
their start uses a generic `encounter N` label because v1 carries no boss name.

## Acknowledgements, retries and lifecycle

Responses validate against `companion_response.schema.json` and preserve input order:

```json
{
  "schema_version": 1,
  "results": [
    {"event_id": "11111111-1111-4111-8111-111111111111", "status": "accepted"}
  ]
}
```

All four per-event statuses are terminal for that payload:

| Status | Meaning |
| --- | --- |
| `accepted` | Trusted event committed for the announcement feed; speech is not guaranteed. |
| `duplicate` | Already processed, or the same semantic occurrence/boundary is known. |
| `expired` | Too old to announce; lifecycle state may still be applied. |
| `rejected` | Invalid semantic state or disabled source; includes `reason`. |

Rejected reasons include `source_disabled`, `event_id_conflict`,
`invalid_event_time`, `attempt_mismatch`, `attempt_ended`, `out_of_order`,
`missing_attempt`, `profile_mismatch`, `unknown_mechanic`, `before_attempt_start`.
Events older than 24 hours return `expired` with `outside_retention`.
Retrying an unchanged event ID returns `duplicate` even if its original terminal
result was rejected/expired. Reusing an ID with different content is rejected.

HTTP 400/413/415 means fix the request, 401 means fix credentials; do not retry
those unchanged. Retry timeouts, connection failures, HTTP 429 and 5xx with bounded
exponential backoff and jitter, preserving payloads. Send starts before mechanics.
Keep end events available for bounded retry even after their speech expires.
No combat-log byte offsets or HTTP 409 cursor reconciliation apply to this route.

The server retains up to 4,096 event receipts, 128 attempts and 32 sessions per
guild for up to 24 hours, persisted atomically. Deduplication is bounded, not an
unlimited exactly-once guarantee. Live speech history is an in-memory 1,000-item
feed and is not replayed after a backend restart. Pending events acknowledged
before a crash may therefore be silent; they must not be re-created with new IDs.

Accepted/expired starts establish the pull. A new start supersedes prior active
pulls. Mechanics require a known open pull and increasing session sequence.
Repeated/lower mechanic occurrences are duplicates. A late end remains
authoritative even when a higher sequence arrived first. An end without a known
start creates a closed tombstone so later starts/mechanics cannot revive it.
An expired end within retention still cancels queued tactical speech. Events after
a closed pull are rejected. Older sequence numbers cannot replay a fight start.

Lifecycle speech expires after 15 seconds at the receiver. Mechanics use their
profile's `max_lateness_seconds` (currently 5 seconds). Carter also applies its
configured speech-age limit and rechecks expiry before playback. Receipt means
processing succeeded; live mode, voice availability and freshness still govern
whether anyone hears speech.

## Source selection and private operations

The guild defaults to `raw_log` to preserve existing deployments. Before Windows
delivery, select the companion source using Discord:

```text
/logs live mode:on source:companion
/carter join
```

Live mode remains opt-in and is not persisted across Carter restarts. Source
selection is persisted. Changing source clears queued companion feed history and
changes the feed generation. Carter cancels pending speech on feed changes.
Raw-log analysis continues; only the selected feed produces announcements.

Private loopback APIs (guild header required, never publish these routes):

* `GET /api/v1/live/announcement-source` — selection and safe companion counters.
* `POST /api/v1/live/announcement-source` with `{"source":"companion"}` or
  `{"source":"raw_log"}` — save the selection.
* `GET /api/v1/live/announcements/status` — selected source status.
* `GET /api/v1/live/announcements?since=0&limit=100` — Carter's selected feed.

The companion feed and durable ledger have their own lock and storage. Compact
delivery/polling never enters the bulk-ingest lock or full encounter analysis.
Telemetry includes accepted/duplicate/expired/rejected counters, last original
event age, detection delay, transport delay, request duration and receipt time.
No token, raw line, or client path is included. Counters reset with the backend.

The existing Windows test button uses the bulk-ingest feed and tests `raw_log`
mode. In `companion` mode, verify delivery with a fresh start/end from a replay or
live pull; do not infer failure from the bulk test button remaining silent.

## Deployment and reverse proxy

Restart Carter and its managed backend after installing this change. Re-enable
live mode and join the saved voice channel. Keep the API bound to loopback.
The existing guild token is reused; no Discord token belongs on Windows.

If using a reverse proxy, explicitly add an exact, POST-only public route for
`/api/v1/live/events` in addition to the optional existing `/api/v1/live/ingest`.
Allow at most 64 KiB for compact bodies. Forward the two authentication headers
unchanged, retain HTTPS validation, and apply appropriate request rate limits.
Do not expose `/api/v1/live/*` through a broad prefix rule: status, source
configuration, announcements and analysis routes are private and do not have
independent public authentication. Proxy configuration is environment-specific
and is not changed by the code or bundle builder.

## Windows profile bundle

Build with `python -m log_backend.build_companion_bundle`. The ZIP contains
`profiles/*.callout.json`, `schemas/*.json`, this protocol, installation notes,
and dependency-free reference parser/matcher modules under `reference/`.
`manifest.json` records bundle version, each profile revision and every payload
file's SHA-256 and byte length. A sidecar contains the ZIP SHA-256.

Profiles use the existing `carter-observed-callouts/v1` format. Optional
`profile_revision` defaults to 1 for older files and is independent of
`schema_version`. Increment the profile revision whenever trigger/wording policy
changes; install matching revisions on both sides. The shipped profiles explicitly
use revision 1. To include guild overrides, pass `--guild-storage` pointing at that
guild's storage directory when building; overrides must match the server catalog.

Transfer the ZIP and its checksum through a trusted channel. Checksums detect
corruption; they do not authenticate an untrusted download. Windows should verify
all manifest checksums and schema/profile validation into a staging directory,
then activate the bundle atomically between fights. Retain the previous working
bundle and freeze the selected profile for each active attempt. No automatic
profile download endpoint is introduced in v1.

## Copyable Windows handoff

Implement the POST contract above without changing the agreed fields. Deliver
start/mechanic/end events independently of bulk uploads; there are no offsets.
Use UUID4 IDs, increasing per-session sequence, original UTC log timestamps,
revision 1 and matching profile mechanic IDs. Server phrases are authoritative.
Treat all per-event statuses as terminal and retry only transient HTTP/transport
failures with unchanged IDs. Send in order and keep queues bounded. Verify and
activate the supplied profile bundle between fights. Select `companion` on Carter
before testing and re-enable live mode after Carter restarts. No second Discord
bot and no predictive speech are required. For endpoint/auth transport checks,
POSTing a real envelope is sufficient; speech additionally needs a fresh event,
live mode and the existing voice connection.
