# BudgetSwarm — Glossary

Shared vocabulary for the codebase and the thesis. Keeping these terms precise avoids confusion between, for example, *scoring*, *constraints*, and *objective* — a distinction that is central to the project.

---

### Agent
A self-contained module responsible for answering one question (e.g. "what's the weather?"). Agents call external providers and/or the database and return **normalized** data. In BudgetSwarm: ScoutAgent, TransportAgent, StayAgent, ExperienceAgent, WeatherAgent, CoordinatorAgent.

### Multi-agent system (MAS)
An architecture where several specialized agents, each with a distinct responsibility, cooperate to solve a problem. Here the five gathering agents feed the CoordinatorAgent.

### Candidate destination
A city proposed by ScoutAgent as a possibility, before the expensive per-destination analysis runs. The short list (k ≈ 4–6) is what the parallel agents process.

### Constraint (hard constraint)
A rule a solution **must** satisfy to be valid at all. Example: `total_cost ≤ budget`. A plan that violates a constraint is **infeasible** and excluded — it is not merely "worse."

### Objective (objective function)
The single quantity the optimizer **maximizes** among all feasible solutions. In BudgetSwarm it is the weighted sum of factor scores minus penalties. Answers "among the allowed plans, which is best?"

### Score (factor score)
A normalized quality number for one aspect of a candidate (preference, experience, weather, accommodation, cost, travel time). Scores are the **inputs** to the objective. Distinct from constraints: a low score makes a plan worse, but does not make it invalid.

### Normalization
Rescaling heterogeneous values (euros, kilometres, rain %) onto a common scale (e.g. 0–1) so they can be combined. Also refers to mapping different providers' response shapes into common internal schemas.

### Optimization
Selecting the best solution from a set of feasible ones with respect to the objective. Here: discrete constraint optimization solved by OR-Tools CP-SAT.

### OR-Tools / CP-SAT
Google's open-source optimization library; CP-SAT is its constraint-programming solver. It makes the **deterministic** decision (destination + itinerary). It is *not* an LLM.

### CoordinatorAgent
The central component that aggregates agent outputs, computes scores, builds and solves the optimization model, and returns the plan. It is a deterministic optimizer, **not** a chatbot.

### LLM (Large Language Model)
Used **only** to turn the already-chosen plan into a natural-language explanation. It never makes or influences the decision.

### Provider / provider abstraction
An interface (e.g. `WeatherProvider`) that hides which concrete external service is used. Agents depend on the interface, so a vendor can be swapped without changing agent code. Each concrete provider (adapter) also normalizes its own responses.

### Fallback
A secondary path used when a primary source fails, is empty, is rate-limited, or lacks coverage. BudgetSwarm chains: primary → fallback → local dataset/estimate.

### Quality flag
A label on every value indicating how firm it is: `real` (from a live source), `cached` (from cache), or `estimated` (from a fallback/estimate). Propagates to the optimizer and UI for honesty.

### Indicative price
A price that is approximate/cached rather than a live bookable fare. Applies to flight prices (Travelpayouts) and bus/train prices (wrappers). Treated as an estimate.

### Curated dataset
A controlled, normalized, real dataset assembled from open sources (e.g. the destinations feature store). **Not** throwaway mock data — it is a deliberate, reproducible data-engineering artifact.

### Reference data / refreshed data / cache / historical data
The four data lifecycle classes (see schema): rarely-changing backbone (destinations), periodically re-ingested (accommodation, POIs, fuel), short-lived TTL cache (weather, routes, prices), and append-only records of generated plans (trips).

### Feature engineering
Deriving useful inputs from raw data: destination feature scores, per-city accommodation price statistics, indoor/outdoor classification, weather risk flags.

### Indoor / outdoor / mixed classification
A tag on each activity (derived from OSM tags/categories) indicating weather sensitivity. The interface between ExperienceAgent and the weather-aware replanning logic.

### Weather conflict
A derived flag on a forecast day where rain probability exceeds a threshold, signaling that outdoor activities should be moved or replaced.

### Dynamic (itinerary) replanning
Adjusting the itinerary in response to conditions (chiefly weather) by moving or swapping activities — combining deterministic rules (the conflict flag) with re-optimization (the coordinator chooses the best feasible change).

### Parallel agent execution
Running TransportAgent, StayAgent, ExperienceAgent, and WeatherAgent concurrently after Scout, since they are independent and I/O-bound. Reduces latency from the *sum* to roughly the *slowest* of the calls.

### Pipeline
The orchestration in `core/pipeline.py`: run Scout, fan out the parallel agents per candidate, collect results, hand to the coordinator.

### Data-quality degradation
Downgrading a candidate (lower score or exclusion) when data is missing or estimated — instead of failing the whole request. "Degrade, don't crash."

### EU Weekly Oil Bulletin
The European Commission's official, citable weekly publication of national fuel prices — the **authoritative source** for fuel costs in this project.

### Ingestion source vs. authoritative source
The *authoritative source* establishes correctness/citability (EU Oil Bulletin); the *ingestion source* is a practical convenience for getting that data (fuel-prices.eu CSV/JSON). Their values are traceable to each other.
