- **Emotion Regulation module** ✅ — makes the highest-effect-size ADHD symptom
  (emotional dysregulation, Hedges' *g* ≈ 1.17) first-class instead of handled only
  indirectly via panic/encouragement. On demand — one tap or a few words
  (`POST /emotion/support`) — the pure core `prefrontal/emotion_regulation.py` offers
  **one** brief, evidence-matched micro-skill fitted to the feeling: ACT acceptance
  (name → allow → one values-aligned step), a DBT distress-tolerance move (paced
  breathing, 5-4-3-2-1 grounding, a cold-water reset, radical acceptance), or
  self-compassion framing for the rejection-sensitive moments ADHD makes sharp ("RSD"
  as lived experience, not a diagnosis the tool asserts). Skills rotate so back-to-back
  requests don't repeat, each hard moment is logged (honestly, judgment-free) into the
  behavioral profile, and an opt-in acceptance line folds into the rough-day recovery
  message (`emotion_recovery_acceptance`). **General-wellness support, not therapy or
  crisis intervention:** the request is screened for self-harm/crisis language *first*
  and, if it trips, answered only with resources (988 / local emergency) and an urge to
  reach a person — never a coping skill. Deterministic and model-free (safety-sensitive
  skill text is delivered verbatim). Covered by `tests/test_emotion_regulation.py`.
