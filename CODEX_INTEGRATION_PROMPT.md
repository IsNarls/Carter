# Codex handoff: Carter log analysis, Discord, and voice

Work in https://github.com/IsNarls/Carter.

Fetch `origin/main` and `origin/log-analysis-api`. Create an integration branch from the current `origin/main`. The branches have unrelated histories: do not force-push, replace main, or blindly merge them.

Read `VOICE_DISCORD_INTEGRATION_REVIEW.md` from `origin/log-analysis-api` first, for example with `git show origin/log-analysis-api:VOICE_DISCORD_INTEGRATION_REVIEW.md`. Then read both branches' README files and inspect the current code. Main already contains Carter's Discord voice prototype with local wake-word listening, Gemini Live, and optional OpenAI fallback. Preserve its authorized-user checks, session ownership, interruption, bounded queues, timeouts, cleanup, and provider fallback behavior.

Begin implementing the integration, rather than stopping at a plan:

1. Bring the log-analysis code and documentation into a clearly separated package/directory or service deployment, retaining Carter's main README and avoiding collisions between tests, entry points, and configuration. Inspect internal imports before relocating modules.
2. Add a typed asynchronous HTTP client for the existing analysis API. Configure its base URL through environment configuration and `.env.example`; do not commit secrets. The Windows log follower can remain a separate service from a Linux-hosted bot. Keep loopback defaults and explicitly document any remote connectivity requirements.
3. Add authorized Discord commands for report/fight selection, player lookup, summaries, rankings, and death explanations. Resolve ambiguous player/death/pull references explicitly and support a Discord-user-to-character mapping for "Why did I die?"
4. For death questions, call the combined `/answer` endpoint once. It returns `answer.text`, `analysis`, `replay`, and a base64 SVG `image`. Render SVG to PNG and deliver text plus the matching attachment together. Preserve source/session/attempt identity, confidence, provenance, and missing-image behavior. Do not present the diagram as arena artwork.
5. Wire log-analysis tools into the existing voice AI provider/session path using the provider's supported mechanism. Check current official provider documentation when implementing tool calls. Feed concise evidence-based results back into the active conversation so the bot can speak the answer; post the associated image in the configured text channel. Never include base64 image bytes in text-only tool context. Retain a deterministic text-command path if provider tool support needs additional work.
6. Support current and explicitly selected prior pulls. The existing live API has archived summaries/timelines but no archived-attempt death-answer route. Add and test that support if needed; never silently use the current pull for a "last pull" question.
7. Use cursor polling only where needed for live notifications; deduplicate by session/event identity and recover from cursor expiry/restarts. Keep state-changing timer operations out of ordinary question-answer tools.
8. Add focused offline tests for routing, API failures, identity resolution, PNG attachments, evidence consistency, archived deaths, provider tool handling, and authorization. Use mocked Discord/provider clients and no paid provider calls. Run the existing relevant tests from both codebases.
9. Update setup documentation and provide a concrete smoke-test procedure for Discord text, spoken death questions, and matching image delivery. Describe any credentials/configuration still needed without printing secrets. Do not claim live Discord or voice verification from offline tests.

Commit the implementation on the integration branch and report changed behavior, tests, and remaining deployment steps. Do not deploy a bot or make live provider calls unless separately authorized. Do not force-push or overwrite either source branch.
