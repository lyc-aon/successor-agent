---
name: biomedical-research
description: Use this skill for biomedical literature and registered clinical-study lookup via Europe PMC and ClinicalTrials.gov.
when_to_use: Use when the user asks about papers, studies, trials, recruiting status, phases, abstracts, or biomedical evidence. Prefer this over generic search for medical and life-sciences questions.
allowed-tools: holonet
---

# Biomedical Research

Use `holonet`'s biomedical routes instead of generic web search.

## Provider choice

- Use `biomedical_research` when the user wants both literature and trial
  status in one pass.
- Use `europe_pmc` when the task is specifically about papers, reviews,
  abstracts, journals, DOI, or literature summaries.
- Use `clinicaltrials` when the task is specifically about trial IDs,
  phases, recruiting status, interventions, or registry metadata.

## Working style

1. Start with the biomedical provider that best matches the request.
2. If the user wants both evidence and active studies, use the combined
   biomedical route first.
3. Preserve concrete identifiers in the answer: DOI, journal title, year,
   NCT ID, phase, and recruiting status when available.
4. Keep claims tied to the returned records. Do not infer efficacy or
   safety beyond what the retrieved study metadata supports.

## Avoid

- Do not use Brave or the browser first for a biomedical question unless
  the user specifically asks for general web coverage.
- Do not fabricate trial status, phase, or outcomes when the tool output
  only shows registry metadata.
