# Limitations

ReplyAI is a well-instrumented prototype built to study how LLM agents fail and which
engineering controls contain those failures. It is not a production service. This page
states what has and has not been established.

## Scripted simulation is not model intelligence

Simulation mode and the automated test suites replace the LLMs with **scripted
outputs** chosen by scenario. They show that ReplyAI:

- routes each kind of comment to the intended decision;
- stores, displays and explains decisions correctly;
- retries, recovers and refuses duplicates as designed.

They do **not** show that a real model writes good replies, triages accurately, notices
sarcasm, or resists a determined prompt injection. A scripted "pass" means the plumbing
around the model works.

## Not yet validated

| Area | Status |
| --- | --- |
| Reply quality with real LLMs | Not measured |
| Accuracy of triage, sarcasm, answer-source and language labels | Not measured with real models; the routing rules were checked with injected correct labels (an upper bound) |
| Prompt-injection attack success rate | Not measured |
| Hallucination rate with the grounding variants | Not measured |
| Publication to a real YouTube channel | Manual, human-confirmed posting exercised on a test channel; **automatic** live publication not performed |
| OAuth and YouTube calls | Exercised manually on a test channel; the automated tests use mocks |
| Independent security audit | None |

An opt-in real-model evaluation harness exists in the development repository but has
not been run.

## Provider rate and token limits

- Default request budgets (28/min, 900/day) are set below Groq's free-plan limits; other
  providers and plans differ. Configure them for your plan.
- Free-tier limits run out quickly: each comment may need up to eight model requests
  including retries. Expect `RETRY` decisions under load.
- Budgets use fixed one-minute and one-day windows, not sliding windows.

## Local capacity estimates are not provider quota

The **Settings → LLM usage** meter and the replies-remaining estimate are computed from
ReplyAI's own records against ceilings you configure (`LLM_*_TOKENS_PER_DAY`). They cannot
see:

- other applications using the same API key;
- the provider's actual remaining quota and its reset schedule;
- requests made before the usage ledger existed.

The estimate appears only after 10 completed replies and assumes future replies cost
about as much as recent ones. The YouTube unit budget is likewise ReplyAI's own
counter, not Google's quota page.

## Human review and safe publication

- **Manual review is the default** and the only mode that always waits for a person.
- Automatic modes can only be as good as their inputs. Deterministic rules catch known
  patterns; novel phrasing can get past them.
- Revalidation runs immediately before posting, but a comment edited or answered in the
  seconds between revalidation and posting can't be detected.
- Duplicate protection covers replies made *by ReplyAI*. Replies posted with other tools
  are invisible to the guard until revalidation sees them.
- A lost publication response leaves an `unknown` attempt that a person has to resolve.
- Recommended practice for any real channel: Manual review → Dry run → low caps.

## Scope limits

- Top-level comments only; replies inside threads are not drafted.
- English, Hindi and Hinglish only; other languages are skipped.
- One channel per session; no multi-user roles or team permissions.
- Comment discovery is polling-based (default every 5 minutes).
- Localhost-oriented defaults (`127.0.0.1`, development cookies). Running it as a hosted
  service would need TLS, `APP_ENVIRONMENT=production`, secret management and a
  deployment review.

## Experimental components

These are off by default or exist only for comparison:

| Setting | Status |
| --- | --- |
| `ROUTING_MODE=tiered` | Skips model calls that can't change the outcome; off until validated |
| `GROUNDING_VARIANT=v2` / `v3` | Alternative grounding strategies; default is `v1` |
| `SANITIZE_MODEL_INPUT`, `SPOTLIGHT_UNTRUSTED` | Injection hardening options; off by default |
| `TRIAGE_SKIP_THRESHOLD` | Mechanism present; threshold not calibrated (default `0.0`) |
| Transcript retrieval, multi-turn context selection | Offline experiments only; not in this release |

## Testing material

The automated test suites, evaluation fixtures and research notes stay in the
development repository and are not part of this public release. Test counts in the
README are reported from that repository.
