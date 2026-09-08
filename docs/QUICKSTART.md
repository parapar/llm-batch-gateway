# Student Quickstart

This service runs batch inference jobs against the class's shared
llama.cpp servers. You submit a file of requests, it runs them as
capacity allows, and you download the results when they're done. It
speaks the same API as OpenAI's Batch API, so the official `openai`
Python SDK works against it without modification.

## 1. Get an API key

If your instructor has set up the **web portal**, that's the easiest
route: go to the portal URL they gave you (something like
`https://batchsvc.class.example.edu/portal`), sign in with your usual
university username and password, and click **Generate an API key**.

The key is shown **once**, on that page, right after you generate it.
Copy it somewhere safe immediately -- the server only keeps a hash of
it, so it genuinely cannot be shown again. If you lose it, generate
another one (which replaces the old one, so anything still using the old
key stops working).

The portal also shows, at a glance:
- how many tokens you have left, how many you've used, and how many are
  currently held for jobs still running;
- each recent job, its status, and what it actually cost you;
- where your allowance came from.

If there's no portal at your site, ask your instructor for:

- The server's base URL (e.g. `http://batchsvc.class.example.edu:8000`)
- Your API key (looks like `sk-...`) -- it's shown to them only once
  when they create it, so if you lose it, ask for a new one rather than
  trying to recover the old one.

Keep your key secret -- anyone with it can submit jobs against your
token budget. Don't commit it to a public repo or notebook you share.

## 2. Check your budget

The portal shows this on its front page. From the command line:

```bash
curl -s $BASE_URL/v1/budget -H "Authorization: Bearer $API_KEY"
```

```json
{
  "granted_tokens": 100000,
  "reserved_tokens": 0,
  "used_tokens": 0,
  "available_tokens": 100000
}
```

- `granted_tokens` -- your total allowance for the course/assignment.
- `reserved_tokens` -- held for batches you've submitted that haven't
  finished yet (released back as they complete).
- `used_tokens` -- actually consumed so far (input + output tokens,
  combined).
- `available_tokens` -- what you can still spend. If a batch would
  need more than this, submission is rejected outright (HTTP 429,
  `insufficient_quota`) -- nothing is partially charged.

## 3. Install the SDK

```bash
pip install openai
```

## 4. Write your batch input file

One JSON object per line, each shaped like a normal chat completion
request. `model` is accepted but ignored -- this server always serves
whichever single model it's configured with.

```jsonl
{"custom_id": "q1", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Summarize photosynthesis in one sentence."}], "max_tokens": 200}}
{"custom_id": "q2", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "What is the capital of France?"}], "max_tokens": 50}}
```

Notes:
- `custom_id` must be unique within the file -- it's how you match
  results back to requests; use it, not line order.
- `max_tokens` matters for your budget: it's the worst-case output
  length this line can reserve. Set it to something reasonable for
  your task, not the model's absolute maximum -- an unnecessarily high
  `max_tokens` reserves (and briefly locks up) more of your budget than
  the request will likely use, even though the reservation is released
  back down to what was actually generated once the request finishes.
- If you omit `max_tokens`, the server applies its own default cap.

## 5. Submit the batch

```python
from openai import OpenAI

client = OpenAI(api_key="sk-...", base_url="http://batchsvc.class.example.edu:8000/v1")

batch_file = client.files.create(file=open("questions.jsonl", "rb"), purpose="batch")
batch = client.batches.create(
    input_file_id=batch_file.id,
    endpoint="/v1/chat/completions",
    completion_window="24h",
)
print(batch.id, batch.status)
```

If your file has a malformed line, or your budget can't cover the
worst case for the whole file, this call fails immediately (before
anything is queued) with a clear error message -- fix the file/request
and resubmit.

## 6. Check status (and estimated time remaining)

```python
status = client.batches.retrieve(batch.id)
print(status.status, status.request_counts)
```

The raw response also includes two extensions the official SDK doesn't
know about by name (but you can still read as plain fields/dict keys):

```json
{
  "status": "in_progress",
  "request_counts": {"total": 2, "completed": 1, "failed": 0},
  "x_tokens": {"reserved": 412, "consumed": 187},
  "x_eta": {
    "estimated_seconds_remaining": 340,
    "estimated_completion_at": 1732650000,
    "queue_position": 2,
    "confidence": "low"
  }
}
```

- `x_eta.confidence` is `"low"` early on (before the server has seen
  enough completed requests to trust its own throughput estimate) or
  `"unavailable"` once your batch is no longer in progress, or if
  nothing is currently able to run it. Treat the number as a rough
  guide, not a promise -- these machines are slow, and load varies.
- `queue_position` counts how many other still-running batches were
  submitted before yours, not your exact position in line (submissions
  are served fairly across students, not strictly first-come-first-served).

Batch status moves through: `validating` -> `in_progress` ->
`finalizing` -> `completed` (or `failed`/`cancelled`/`expired`).

## 7. Download results

Only once `status` is `completed`:

```python
output = client.files.content(status.output_file_id)
for line in output.text.splitlines():
    print(line)  # one JSON object per request, in {"custom_id", "response", "error"} shape
```

If any lines failed (a node error that didn't recover after retries),
`status.error_file_id` points at a second file with the same shape,
`error` populated instead of `response`. A partial failure doesn't
fail the whole batch -- check both files.

Results aren't kept forever -- ask your instructor how long
(`result_retention_days` in their config, commonly about a week).
Download what you need promptly.

## 8. Cancel a batch you don't need anymore

```python
client.batches.cancel(batch.id)
```

Only works while the batch is still `validating` or `in_progress`.
Cancelling releases whatever budget was reserved for the
not-yet-finished requests back to you immediately.

## Common errors

| HTTP | code | what it means |
|---|---|---|
| 401 | `invalid_api_key` | missing/wrong/revoked key |
| 404 | `not_found` | no such file/batch, or it belongs to someone else |
| 429 | `insufficient_quota` | this request needs more tokens than you have available |
| 400 | `invalid_request` | malformed input file, unsupported endpoint, etc. -- the message says exactly which line/field |
| 409 | `conflict` | e.g. trying to cancel a batch that's already finished |

Every error comes back as `{"error": {"message", "type", "code",
"param"}}` -- same shape the `openai` SDK already expects, so its
normal exception handling (`openai.APIStatusError` and friends) works
unmodified.
