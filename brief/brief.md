---
version: 0
generated: 2026-01-01
source: hand
tv_safe: true        # contains nothing private; checked against brief.tv_forbidden_terms in config
priorities:          # ranked; every kept idea must name one of these ids
  - id: p1
    text: "Build reliable software that uses language models: evaluation, failure handling, cost control"
    signals: [evals, agent reliability, tool calling, prompt design, observability, cost per request]
  - id: p2
    text: "Product craft for software teams: discovery, prioritisation, pricing, metrics"
    signals: [product sense, strategy, roadmaps, experiments, pricing, retention metrics]
  - id: p3
    text: "Practical lessons from small teams shipping real products"
    signals: [shipping, side projects, distribution, validation before building]
active_projects:     # name-drop these when an idea applies directly
  - "A personal podcast digest that turns long episodes into short two-host scripts"
already_known:       # deltas only: never explain these from scratch
  - "LLM basics: embeddings, retrieval, prompt engineering, tool calling"
  - "Agile basics: sprints, backlogs, standups"
ignore:              # cut unless it is a genuine, large shift
  - "funding rounds, valuations, and executive gossip"
  - "model-launch hype without an engineering or product lesson"
  - "generic productivity and motivation advice"
  - "sponsor reads and podcast housekeeping"
depth:
  default: practitioner   # assume the listener builds this; skip 101 framing
  go_deeper_on: [evaluation, reliability]
tone: "Calm and direct. No hype words. A concrete example or analogy for every idea. End with 'what this means for you this week'."
---

# Example relevance brief

This is a generic example. Copy it, then rewrite the priorities, `already_known` and `ignore` lists for yourself.
Everything below the front matter is free text for you and is never shown to the model.
