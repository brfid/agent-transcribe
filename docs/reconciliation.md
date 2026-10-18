# Deliberately changing a filed reading

`reconcile` updates a chosen reading or its gap declaration for existing filed
sources. Use it after an explicit evidence decision, such as selecting a targeted
OCR retry. It preserves original scans, source order, membership, raw readings,
and prior published history. It does not identify documents, silently replace
changed bytes, move sources between folders, or perform a new extraction.

Read the document README and run `check` before deciding. Preserve the earlier
text and note why the new reading is preferred. Human evidence/interpretation
belongs in the README; machine exports remain generated artifacts.

## Pin the current snapshot

Create a UTF-8 JSON recipe outside the document folder. Compute the SHA-256 of
its current `.filing-current.json`, the selected new text, and any complementary
readings. The required snapshot pin prevents applying a decision to later work.
The fictional placeholders below must be replaced with actual hashes:

```json
{
  "schema": 1,
  "expected_receipt_sha256": "<current .filing-current.json SHA-256>",
  "reason": "Prefer the explicit rotation retry for this capture",
  "evidence": "Original inspected for orientation; retry retains the missing lines",
  "text_root": "../organized/Needs review/IMG_1001 rotated retry",
  "changes": [
    {
      "source": "Box 2/IMG_1001.jpg",
      "preferred_text": {
        "path": "pages/by-source/Box 2/IMG_1001.jpg/extract.txt",
        "sha256": "<64 lowercase hexadecimal characters>"
      }
    }
  ]
}
```

`schema` is `1`; `expected_receipt_sha256`, nonempty `reason`, nonempty `evidence`,
and a nonempty `changes` list are required. Optional `text_root` resolves relative
to the recipe; when omitted, reading paths resolve from the recipe directory.
Specify it explicitly when the selected readings live elsewhere. Only documented fields are
accepted. Each change names an existing `source` locator and may set:

| Field | Meaning |
| --- | --- |
| `preferred_text` | `{path, sha256}` for a new reading, or `null` to remove it |
| `complementary_text` | A list of additional `{path, sha256}` readings; `[]` clears it |
| `gap_reason` | A nonempty explicit gap reason; `null` requests clearing |
| `gap_clear_reason` | Nonempty evidence-based justification required to clear a gap |

Omitted fields stay unchanged. Removing preferred text requires an explicit gap;
selecting nonempty text does not automatically erase an earlier recognition gap.
A gap is cleared only with a stated justification. Do not equate a better machine
reading with visually verified transcription.

```sh
agent-transcribe reconcile \
  --document '/archive/organized/Selected document' \
  --changes '/archive/batch-inputs/reading-change.json' --fetch
agent-transcribe check \
  --document '/archive/organized/Selected document' --verify-sources --fetch
```

Use `--fetch` for exact selected iCloud controls/readings. The operation checks
the pinned receipt and current source/output state before guarded publication.
It stages the new exports/combined reading/managed map, retains old exports,
notes, and receipt in history, and records the reason/evidence. Scan copies and
raw extraction variants stay intact. Human README prose remains outside the
managed region.

## Interruption and limits

Resume only with the same pending recipe. Keep its bytes stable until publication
finishes. Another recipe, changed pin, modified current output, or missing/changed
prepared bytes requires inspection; do not remove pending state or history to
bypass the check. Re-run `check`, inspect differences, and report the actual
mechanical result and remaining gaps separately from identification/visual review.

Reconciliation requires a current assembly-version-1 filing snapshot. Unsupported
states are rejected without conversion. Membership regrouping is a separate
explicit archival decision outside this reading-change command.
