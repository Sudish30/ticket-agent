# INTEGRATION.md — ticket-backend contract for the Ticket Understanding Agent

> **Status of this document.** `fake_jira/server.py` is not present in this checkout (nor in
> `~/Downloads/ticket-agent.zip`, which is an identical snapshot). The contract below is therefore
> specified from the *consumer* side: it is exactly the four Jira Cloud REST v3 routes that
> [`ticket_agent/jira_client.py`](ticket_agent/jira_client.py) calls. Any fake Jira server or custom
> ticket UI backend that implements these four routes works with the agent unchanged — just point
> `JIRA_BASE_URL` at it. If your `server.py` exposes different paths, either add a thin adapter that
> maps them to these, or update this document to match the real routes.

## 1. How the agent talks to the backend

| env var | used for |
|---|---|
| `JIRA_BASE_URL` | prefix; every call is `{JIRA_BASE_URL}/rest/api/3/...` (trailing slash stripped) |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | sent as HTTP Basic auth on every request. A fake server may ignore them but must accept the header. |
| `JIRA_AGENT_NAME` | display name the agent expects on *its own* comments (default `Ticket Agent`); comments with this author are never treated as replies |

All requests use `Content-Type: application/json`, a 30 s timeout, and `raise_for_status()` — any
non-2xx aborts the run. GET responses must be JSON; POST responses may be JSON **or an empty body**.

Sequence for `python main.py DEMO-1 --jira`:

```
GET  /issue/DEMO-1                       load the ticket (summary, description, comments…)
      └─ analyze (LLM)
GET  /issue/DEMO-1                       count comments  → before = N
POST /issue/DEMO-1/comment               "@<reporter> <clarification message>"
      └─ every 15 s (up to 1 h):
GET  /issue/DEMO-1                       reply = comments[N+1:] whose author != JIRA_AGENT_NAME
      └─ analyze → (repeat ask) → build_brief (LLM)
GET  /issue/DEMO-1 ; POST …/comment      sign-off message ("Reply 'confirm' to proceed…")
GET  /issue/DEMO-1 (poll)                reply must START with confirm | confirmed | yes | lgtm | looks good | approved
      └─ brief.json written
POST /issue/DEMO-1/comment               only with --post-brief: the full brief as Markdown (same content as brief.md)
```

`JiraClient.transition()` (endpoints 3 and 4) is exposed for downstream agents but is **not** called
by the clarification loop today; implement it for completeness, it is two trivial routes.

## 2. The four endpoints

### 2.1 `GET /rest/api/3/issue/{key}` — fetch a ticket (also used to list comments)

Response `200` JSON. Fields the client reads (everything else is ignored):

| path | type | required? | notes |
|---|---|---|---|
| `key` | string | **yes** | `KeyError` if missing |
| `fields` | object | **yes** | `KeyError` if missing |
| `fields.summary` | string | no (default `""`) | |
| `fields.description` | ADF doc \| **plain string** \| null | no | passed through `adf_to_text`, which accepts a plain string as-is |
| `fields.issuetype.name` | string | no (default `Task`) | `issuetype` must be an object or **absent — not null** |
| `fields.priority.name` | string | no | `priority` may be null |
| `fields.labels` | string[] | no | |
| `fields.components[].name` | string | no | |
| `fields.reporter.displayName` | string | no | may be null. **This is who the agent @-mentions and waits on** (unless `--contact` overrides it) |
| `fields.assignee.displayName` | string | no | may be null |
| `fields.issuelinks[].outwardIssue.key` / `.inwardIssue.key` | string | no | |
| `fields.parent.key` | string | no | surfaced as the epic |
| `fields.comment.comments[]` | array | no | `comment` must be an object or **absent — not null** |
| `…comments[].author.displayName` | string | **yes per comment** | `KeyError` if missing |
| `…comments[].body` | ADF doc \| **plain string** \| null | no | |
| `…comments[].created` | string | no (default `""`) | shown to the model; ISO date is fine |

**Hard requirements for the clarification loop to work:**

1. `comments` are returned **oldest first** and **unpaginated** — the client counts them before
   posting and then slices `comments[before+1:]` to find replies.
2. A comment accepted by `POST …/comment` must appear in this list **immediately and at the end**.
   If it is not persisted, the slice skips the human's first reply and the agent waits forever.
3. Comments the agent posted must carry `author.displayName == JIRA_AGENT_NAME`; the human's reply
   must carry any other name (normally the reporter's).

Minimal valid example:

```json
{
  "key": "DEMO-1",
  "fields": {
    "summary": "Login times out for some users",
    "description": "Some users get logged out randomly since the last release.",
    "issuetype": {"name": "Bug"},
    "priority": {"name": "High"},
    "labels": ["auth"],
    "components": [{"name": "web-app"}],
    "reporter": {"displayName": "Priya"},
    "assignee": null,
    "issuelinks": [],
    "comment": {
      "comments": [
        {"author": {"displayName": "Dan"}, "body": "could be the SameSite cookie change", "created": "2026-08-22"}
      ]
    }
  }
}
```

If you prefer real ADF, `adf_to_text` understands: `text`, `mention` (→ `@name`), `hardBreak`,
`paragraph` / `heading` / `listItem` / `codeBlock` / `blockquote` (children + newline),
`bulletList` / `orderedList` (`- ` per item). Unknown node types fall through to their children.

### 2.2 `POST /rest/api/3/issue/{key}/comment` — add a comment

Request body (always ADF; one `paragraph` per non-empty line of the message; the text begins with
`@{contact} `):

```json
{
  "body": {
    "type": "doc", "version": 1,
    "content": [
      {"type": "paragraph", "content": [{"type": "text", "text": "@Priya Hi Priya! My read on DEMO-1: …"}]},
      {"type": "paragraph", "content": [{"type": "text", "text": "1. What action triggers the logout? (a) … (b) …"}]}
    ]
  }
}
```

Server must: append the comment to the ticket (author = `JIRA_AGENT_NAME`, `created` = now) and
return any `2xx`. The response body is ignored (`{}` or empty is fine).

Your UI should render this comment and let the reporter reply as a **new comment** on the same
ticket — that reply (all new non-agent comments, bodies joined with `\n`) is what the agent reads.

### 2.3 `GET /rest/api/3/issue/{key}/transitions` — list workflow transitions

```json
{"transitions": [{"id": "21", "name": "In Progress"}, {"id": "31", "name": "Done"}]}
```

`name` is matched case-insensitively; `id` is echoed back in 2.4.

### 2.4 `POST /rest/api/3/issue/{key}/transitions` — apply a transition

```json
{"transition": {"id": "21"}}
```

Return any `2xx`; body ignored.

## 3. Smoke test against your server

```bash
export JIRA_BASE_URL=http://localhost:8055 JIRA_EMAIL=agent@example.com JIRA_API_TOKEN=x JIRA_AGENT_NAME="Ticket Agent"

# raw routes
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" "$JIRA_BASE_URL/rest/api/3/issue/DEMO-1" | python3 -m json.tool
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"body":{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"ping"}]}]}}' \
  "$JIRA_BASE_URL/rest/api/3/issue/DEMO-1/comment"

# through the client (prints the ticket exactly as the model will see it)
PYTHONPATH=. python -c "from ticket_agent import JiraClient; print(JiraClient().get_ticket('DEMO-1').as_text())"

# full loop, answering in the browser:      python main.py DEMO-1 --jira
# full loop, answering in the terminal:     python main.py DEMO-1 --jira --terminal
# ...and leave the Markdown brief on the ticket for the UI to show:
python main.py DEMO-1 --jira --post-brief
```

The `--post-brief` comment is `TaskBrief.to_markdown()` — byte-for-byte the `brief.md` the run writes next to
`brief.json`. Shape:

```markdown
# DEMO-1 — Task brief (confidence 0.92)
_Type: bug · clarification contact: Priya_
## Problem            (paragraph)
## Goal               (paragraph)
## Acceptance criteria   - [ ] checklist items
## Repro steps        1. numbered
## Suspected files    | Path | Confidence | Reason | table (or "_None (no codebase connected)._")
## Affected areas / ## Constraints / ## Out of scope / ## Assumptions / ## Related findings / ## Evidence   bullet lists
<details><summary>Q&amp;A transcript (N questions)</summary> … one ### qN block per question with **Q:** / **A:** … </details>
```

`JiraClient.add_comment` sends it as one ADF `paragraph` per non-empty line, so a UI that renders Markdown gets the
full structure back; plain Jira Cloud shows it as text lines (blank lines dropped, `<details>` shown literally).

## 4. Checklist for a custom ticket UI

- [ ] Serves `GET /rest/api/3/issue/{key}` with at least `key`, `fields.summary`, `fields.reporter.displayName`, `fields.comment.comments[]`.
- [ ] Comments oldest-first, unpaginated, each with `author.displayName` and `body`.
- [ ] `POST …/comment` persists immediately, stamps the author as `JIRA_AGENT_NAME`, returns 2xx.
- [ ] Reporter can post a follow-up comment from the UI (that is the only way to answer the agent).
- [ ] Reporter's sign-off comment starts with `confirm` (or `yes` / `lgtm` / `looks good` / `approved`).
- [ ] (optional) Renders the final `--post-brief` comment as Markdown (it arrives through the same `POST …/comment` route, authored as `JIRA_AGENT_NAME`; one paragraph per line — join paragraphs with `\n` before rendering).
- [ ] (optional) `GET`/`POST …/transitions` for downstream agents that move the ticket.
