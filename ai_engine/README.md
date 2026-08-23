# ai_engine/

The intellectual core. Agents produce normalized domain models; the coordinator
aggregates, scoring runs, then OR-Tools makes the deterministic decision. The LLM
only explains the chosen plan — it never makes or influences the decision.
- agents/  : scout, transport, stay, experience, weather, coordinator
- core/    : pipeline.py, scorer.py, optimizer.py
- utils/   : api_clients.py (provider abstractions + fallbacks)
