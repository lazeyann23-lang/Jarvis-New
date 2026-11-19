# Mission experience memory

Dodaios already has persistent user/agent notebooks, review-based learning,
voice input, desktop/browser tools, and an agent team for multi-step missions.
This change adds **reusable experience from successfully approved missions**
without introducing a new model, service, telemetry endpoint or credential.

## New behavior

1. The existing mission orchestrator decomposes complex requests into up to
   five worker tasks. Brief requests naming at least three separate workstreams
   (for example, frontend, backend, tests) are now offered to the planner too.
   Ordinary small requests keep the cheap deterministic one-step path.
2. After a mission is genuinely **APPROVED**, the mission event subscriber
   checks the authoritative mission state and its `MissionPlanReady` event.
   Failures, cancellations, timeouts, incomplete plans, malformed slugs and
   repeated delivery of the same approval are ignored.
3. The local `data/mission-experience.db` learns the plan's **structural**
   information: sanitized step names, number of workers, whether a source
   checkout was required, hashed task keyword index and timestamp.
   **It never copies task instructions, raw prompts, assistant responses,
   tool outputs or files into the new experience database.** The original
   mission database keeps its own history as before. Hashed terms are not
   encryption and may still be vulnerable to dictionary guessing.
4. The existing planner checks for similar approved plans when it was already
   going to call the model for a complex request. It receives a short advisory
   block describing up to three matching structures. Past material is
   explicitly **not** a command and does not grant permissions. This lookup
   needs no extra AI call and has no background polling.
5. The memory is local, append-only by mission ID, survives restarts and grows
   as new approved missions are recorded. When a mission repeats it gets a new
   ID and can be another reference. Existing personal memory and learned agent
   skills keep their own separate retention and review policies.

## Inspect and correct

Run from the repository directory, using the same Python environment as Dodaios:

```bash
python -m jarvis.missions.experience_cli stats
python -m jarvis.missions.experience_cli forget --mission-id MISSION_ID
python -m jarvis.missions.experience_cli clear --yes
```

If your application uses a custom data path, add `--db "PATH/mission-experience.db"`.
`forget` and `clear` only affect this feature's secondary database; original
missions, outputs and other notebooks remain intact. Stop Dodaios before
manually relocating the database.

## Constraints and verification

- The improvement is limited to planning hints; it does not train or fine-tune
  the underlying language model. Better plans, faster responses and wider
  abilities are possible but **not guaranteed** by a stored reference.
- Actual execution depends on the selected connected model, tools, accounts,
  permissions and availability. Existing risk approvals and tool isolation
  remain authoritative.
- No automatic code modification, credential acquisition, or self-granted
  permissions are introduced by this feature.
- Focused tests: `tests/missions/experience/test_experience_memory.py` and
  existing `tests/missions/kontrollierer/test_decomposer.py`.
