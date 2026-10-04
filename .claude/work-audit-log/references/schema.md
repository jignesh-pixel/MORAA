# Phase log schema

One JSON file = one phase of development. The reasons, decisions and errors live at the top level, once. Tasks stay short.

## Top level

| Field | Required | Notes |
|---|---|---|
| `project` | yes | Name shown on the title and in the footer of every page. |
| `phase` | recommended | Name of this phase, e.g. "WhatsApp intake". Shown under the title and used in the file name. |
| `why` | yes | Why this phase of work is being done, from first principles: the problem or goal, and what breaks without it. A few sentences. |
| `approach` | no | A short "what was done, in short" paragraph. |
| `decisions` | when any were made | List of decisions (below). Numbered D1, D2... by position. |
| `errors` | when any occurred | List of errors (below). Numbered E1, E2... by position. |
| `domains` | yes | Ordered list. Numbered 1, 2, 3... by position. |

## Decision

| Field | Required | Notes |
|---|---|---|
| `decision` | yes | The choice made, as one sentence. |
| `why` | yes | Why this choice. If there was only one viable path, say so here and what ruled the others out. |
| `alternatives` | recommended | List of `{ "option": "...", "why_not": "..." }`. Only real options that were weighed. |
| `tasks` | recommended | Which tasks this relates to, e.g. `"1.2, 2.1"`. |

## Error

| Field | Required | Notes |
|---|---|---|
| `error` | yes | The key line of the error message, verbatim where possible. |
| `cause` | recommended | The root cause, not just the symptom. |
| `fix` | recommended | What fixed it. Say if it is a workaround. |
| `task` | recommended | The task it came up in, e.g. `"2.1"`. |

## Domain

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Short area name, e.g. "Payments". |
| `goal` | no | One line on what this area is for. |
| `tasks` | yes | Ordered list. Numbered 1.1, 1.2... by position. |

## Task

| Field | Required | Notes |
|---|---|---|
| `title` | yes | Outcome-phrased: what is true when it is finished. |
| `status` | yes | `todo`, `in_progress`, `done`, `blocked`, `dropped`. Claude's own tracking: NOT printed (the PDF leaves a blank box for the user to write the status), except that `dropped` tasks get a "DROPPED" note. |
| `verify` | for `done` | One or two lines a non-programmer can follow to check it works, plus what good quality looks like. Printed as "HOW TO CHECK". |
| `blocked_by` | for `blocked` | What is needed to unblock. |
| `files` | no | List of file paths touched. |
| `date` | no | `YYYY-MM-DD` the task was logged. |
| `why`, `solution`, `why_this`, `alternatives`, `errors` | rarely | Per-task detail, printed under the task only when present. Use only when one task is too complex to explain in the phase-level sections. `alternatives` and `errors` have the same shape as above (errors without `task`). |

Text fields may contain line breaks (`\n`). Plain text only: no markdown or HTML.
An empty or missing `decisions` or `errors` list prints "None logged." on the page.

## Example

```json
{
  "project": "Example Project",
  "phase": "WhatsApp intake",
  "why": "Customers send product photos on WhatsApp, and every generation costs real money. Without a check at intake, blurry or empty photos waste that cost and give the customer a bad result. This phase makes intake reliable before anything paid runs.",
  "approach": "A quality check now runs before generation, and the customer gets a WhatsApp reply when a photo is rejected.",
  "decisions": [
    {
      "decision": "Use a rule-based photo check instead of asking the generation model to fail",
      "why": "The rules are free, fast and predictable, and catch the common failures (blur, too small, no product in frame).",
      "alternatives": [
        { "option": "Let the generation model fail on its own", "why_not": "The cost is already spent by the time it fails, and the error is not customer-friendly." },
        { "option": "Manual review of each photo", "why_not": "Doesn't scale and breaks the promise of a one-message service." }
      ],
      "tasks": "1.1"
    }
  ],
  "errors": [
    {
      "error": "TypeError: Cannot read properties of undefined (reading 'width')",
      "cause": "WhatsApp sends some photos without EXIF metadata, so the size lookup returned undefined.",
      "fix": "Read dimensions from the decoded image instead of the metadata.",
      "task": "1.1"
    }
  ],
  "domains": [
    {
      "name": "Photo intake",
      "tasks": [
        {
          "title": "Reject unusable photos before any paid generation runs",
          "status": "done",
          "files": ["intake/validate.js", "intake/reply.js"],
          "verify": "Send a deliberately blurry photo to the test number. Expect a reply asking for a clearer photo and no generation charge in the log."
        },
        {
          "title": "Send the finished images back in the same chat",
          "status": "todo"
        }
      ]
    }
  ]
}
```
