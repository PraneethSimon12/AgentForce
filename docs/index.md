# Documentation Map — AgentForge

Which file answers which question. If you are looking for something and it is not here, it has
not been written yet — add it to this map first, then write it.

| Question | File | Governs |
| --- | --- | --- |
| What does the system do? Who uses it, and how does it behave at the edges? | `product-spec.md` | **Behaviour** |
| How is it designed, and why is it shaped this way? | `architecture.md` | **Design** |
| What do we build, in what order, and what exactly goes on the wire? | `plan.md` | **Wire format** |
| Why did we choose X over Y? | `decisions.md` | The append-only record of *why* |
| What will I be asked about this in an interview, and what is the answer? | `interview-prep.md` | Study material |
| What does my resume claim, and is it true yet? | `resume-claims.md` | Claim → evidence → status |
| What are the actual numbers, and which run produced them? | `eval-report.md` | **Evidence** |
| How do I work on this repo? What are the rules? | `../CLAUDE.md` | The working agreement |

**Precedence.** If two documents disagree: **behaviour > design > wire format**. The loser gets
corrected rather than tolerated — a stale doc is worse than a missing one, because it is trusted.

**Two rules that keep this set honest:**

1. `decisions.md` is append-only and is the *only* home for a decision's reasoning.
   `architecture.md` links to a decision ID rather than restating it, so an ADR never exists in
   two places and can never half-rot.
2. **No number reaches `resume-claims.md` — or my CV — until `eval-report.md` records the run
   that produced it.** One file is the claim, the other is the evidence, and they are separate on
   purpose.

**Reading order for someone new to the repo** (including me, six months from now):
`product-spec.md` → `architecture.md` → `decisions.md` → the code.
