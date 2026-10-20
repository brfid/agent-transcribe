# Manager, worker, and reviewer protocol

Agents operate this file-based CLI through their existing harness. The package
has no embedded agent loop, model requirement, OpenAI SDK, or API key. One lead
keeps ownership of the user's request, shared records, integration, and final
claims. Narrow helpers improve throughput only when their coordination and
verification cost is justified.

This manager/helper structure follows the distinction in official OpenAI
[Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration):
a manager retains the user-facing answer while specialists perform bounded work.
The role protocol below is a local archival application of that principle; it
does not require the SDK APIs illustrated on that page.

## Roles and authority

| Role | Owns | Must return |
| --- | --- | --- |
| Lead/manager | Scope, collection records, assignments, integration, closeout | Exact covered set, outputs, gaps, review scope, next action |
| Extraction worker | One declared source/job set and its mechanical outputs | Actual commands/results, original locators, retained text, failures, checkpoints |
| Identification/filing worker | Assigned document set and proposed/authorized filing | Evidence spans, explicit dispositions, title/date/boundary uncertainty, mappings |
| Independent reviewer | Read-only coverage, hashes, outputs, and evidence QA | Checked set/method, discrepancies, limitations, required corrections |

Assign disjoint write sets: separate jobs/document folders, or separate software
modules. A single lead writes shared collection statuses, indexes, plans, and
integration records. Never run two writers against one job/document. Reviewers
do not repair a worker's outputs while reviewing them. If ownership changes,
checkpoint and explicitly hand it over first.

## Economical delegation

Spawn proactively only when the speed or quality benefit exceeds coordination
and verification cost. Budget the whole workflow: context transfer, duplicate
reading, tool use, worker time, review, and integration can outweigh the benefit
of delegation. Model selection and reasoning effort follow the user's universal
instructions and settings.

Delegate clear assignments whose likely errors and important omissions can be
checked. Examples include an exact-list inventory, locator audit, receipt
comparison, or synthetic test adaptation. Escalate to the lead when ambiguity,
consequence, or hard-to-detect omissions make that check inadequate. The lead
verifies in proportion to risk and remains responsible for the final result.

Prefer deterministic scripts for bulk mechanical work such as hashes, counts,
path checks, and schema comparisons. Give each helper small task-specific
context, authoritative file paths and locators, exclusions, success criteria,
and write ownership. Read only relevant spans; avoid duplicated broad searches,
full transcript pastes, and full histories when a bounded handoff suffices.
Reuse verified completed work. Return concise artifacts and evidence pointers
instead of repeating the whole source corpus. Keep stable instructions separate
from batch-specific inputs and version reusable prompts with the code when they
are part of the workflow.

Efficiency never justifies less source coverage, weaker provenance, invented
evidence, or skipped required review. Repeated agent agreement and repeated
exports are not independent historical evidence. If a worker's output quality
is uncertain, review a small bounded pilot against the required checks before
extending the assignment.

Separating stable instructions from dynamic context and versioning reusable
prompts follows official [prompting guidance](https://developers.openai.com/api/docs/guides/prompting).
The source lists, archival verification gates, and local CLI design are project
policies; those pages do not require this package or an SDK.

## Work order

Every delegation states:

- Exact sources/documents, order, permitted destination/files, and exclusions.
- Purpose, time/deadline bound, current checkpoint, and local authoritative records.
- Allowed mutations and shared-record owner; no implicit scope expansion.
- Success criteria, evidence to return, known gaps, and review/stop conditions.

For example: extract only the 50 paths in a named ordered list into one fresh job,
retain all failures, and return the receipt/result. An identification helper may
inspect declared covers/transitions and propose assignments while the lead owns
publication. A reviewer may verify those exact assignments against source hashes
and originals without writing. State which actions are authorized in each order.

Before delegating, the lead records the batch declaration from the
[workflow](workflow.md#declare-and-prepare) and allocates disjoint writes. Workers
save actual commands/results, inspected spans, checkpoints, and unresolved items
in the designated project records. The lead verifies consequential results before
integrating them.

## Independent review

Check all selected-source accounting mechanically. Independently compare affected
sources/copies and selected text with the recorded hashes and metadata rather than
accepting a worker summary. Verify locators, order, gaps, and the claimed review
scope. Direct original inspection to decisive identity/date/boundary evidence and
consequential wording; sampled inspection supports only the sampled spans. A tool
report or repeated agent agreement does not establish recognition accuracy or
independent historical corroboration. Keep review evidence with the document.

The lead applies the [completion standard](workflow.md#closeout-and-resume),
including separate extraction, identification, and filing claims. It owns shared
record updates and corrections; reviewers remain read-only.

## Stop and handoff

Honor the [workflow stop and recovery rules](workflow.md#closeout-and-resume).
A worker must report an integrity or ownership conflict rather than repairing
shared state to make its task pass. Checkpoint before transferring write ownership.

Return exact source/derivative paths and locators, commands, receipt/checkpoint,
verified counts/hashes, actual review spans, gaps, any open-document continuity,
pending filing, and the next action. Link retained evidence rather than pasting
whole sources. The lead retains responsibility for the final result.
