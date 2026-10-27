# Live callout profiles

Each `*.callout.json` file is a validated, data-only guide for one encounter.
`profile_revision` defaults to 1 for older profiles and is separate from the
format's `schema_version`. Increase the revision when triggers or wording change;
the Windows companion and server must install matching revisions. Build the
Windows distribution using `python -m log_backend.build_companion_bundle` and
follow [the companion protocol](../COMPANION_PROTOCOL.md).
The authoring contract is [callout_profile.schema.json](../callout_profile.schema.json).
The backend preloads this directory and selects a profile only when both its
`encounter_id` and `difficulty_ids` match an `ENCOUNTER_START` record.

Profiles contain observed combat-log triggers, never executable Lua. A mechanic
matches an exact event type and spell ID plus a constrained source policy. Its
short `text` is sent to Carter for voice playback only while the matching fight
is active and only inside `max_lateness_seconds`.

Add a reviewed profile here to make it available globally on the next pull; a
backend restart is not required. A guild-specific profile may instead be placed
in `log_backend/data/guilds/GUILD_ID/callout-profiles/`. A guild profile with the
same encounter and difficulty overrides the global profile.

Unknown encounters and unmatched difficulties keep the generic encounter-start
and encounter-end announcements. Invalid profiles are ignored and reported in
the live status payload without disabling valid profiles.

Validate the packaged catalog, or a new file, before a live pull:

```bash
python -m log_backend.live_callouts
python -m log_backend.live_callouts path/to/new-boss.callout.json
```

All current wording is marked `provisional`; review it in live play before
changing `wording_status` to `verified`.
