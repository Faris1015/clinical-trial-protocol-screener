---
title: Clinical Trial Protocol Screener
emoji: 🧪
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
pinned: false
---

# Clinical Trial Protocol Screener — live demo

Multi-agent (LangGraph + FastAPI) clinical-trial protocol screener running in
**stub-LLM mode** — deterministic, zero-inference, so it's free to host and needs
no GPU or API key. Upload a protocol (PDF or markdown), watch the agent pipeline
run, approve at the human-in-the-loop gate, and see the patient matches.

> Demo build: canned extractions stand in for the LLM, so the analysis is fixed —
> it exercises the full pipeline (routing, validation, HITL gate, matching), not
> real model quality. Uses fully synthetic patient data.

Source & full project: <https://github.com/Faris1015/clinical-trial-protocol-screener>

<!--
  This Space is built from deploy/huggingface/Dockerfile, which clones the repo
  at build time. To track an un-merged branch, set a build arg REPO_REF in the
  Space's Settings → Variables and secrets.
-->