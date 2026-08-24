# BudgetSwarm — Diagrams (Mermaid)

These render directly in GitHub/GitLab and most Markdown viewers, and can be exported as images for the thesis (e.g. via the Mermaid Live Editor at mermaid.live). They restate the ASCII diagrams from the technical documentation in a cleaner, exportable form.

---

## 1. High-level architecture

```mermaid
flowchart TD
    UI["React Frontend<br/>(TS, Vite, Tailwind, Leaflet)"]
    API["FastAPI Backend<br/>(validation, routing, response shaping)"]
    ENG["AI / Agent Engine"]
    SCOUT["ScoutAgent"]
    subgraph PAR["Parallel agents (per candidate)"]
        TRANS["TransportAgent"]
        STAY["StayAgent"]
        EXP["ExperienceAgent"]
        WEATH["WeatherAgent"]
    end
    COORD["CoordinatorAgent<br/>aggregate + score"]
    OPT["OR-Tools optimizer<br/>(the decision)"]
    LLM["LLM explanation<br/>(presentation only)"]
    DB[("PostgreSQL<br/>reference + cache + history")]
    EXT["External APIs<br/>(behind provider abstractions)"]

    UI -->|"POST /api/generate-trip"| API
    API -->|structured DTO| ENG
    ENG --> SCOUT
    SCOUT -->|candidate destinations| PAR
    PAR --> COORD
    COORD --> OPT
    OPT --> LLM
    LLM --> API
    API -->|JSON| UI

    PAR <--> EXT
    PAR <--> DB
    EXT <-->|cached| DB
```

---

## 2. End-to-end data flow (sequence)

```mermaid
sequenceDiagram
    actor User
    participant React
    participant FastAPI
    participant Engine as AI Engine
    participant Scout as ScoutAgent
    participant Agents as Transport/Stay/Experience/Weather
    participant Coord as CoordinatorAgent
    participant ORT as OR-Tools
    participant LLM

    User->>React: enter budget, duration, transport, preferences
    React->>FastAPI: POST /api/generate-trip
    FastAPI->>FastAPI: validate (Pydantic)
    FastAPI->>Engine: input DTO
    Engine->>Scout: constraints
    Scout-->>Engine: ranked candidate destinations
    par per candidate (parallel)
        Engine->>Agents: gather data
        Agents-->>Engine: normalized TransportOption / Accommodation / Activity / WeatherProfile
    end
    Engine->>Coord: aggregated data
    Coord->>Coord: normalize + factor scores
    Coord->>ORT: model (constraints + objective)
    ORT-->>Coord: optimal destination + itinerary
    Coord->>LLM: chosen plan + numbers
    LLM-->>Coord: explanation text
    Coord-->>Engine: final plan
    Engine-->>FastAPI: structured result
    FastAPI-->>React: JSON (chosen_plan, ranked_candidates, explanation)
    React-->>User: results + comparison + map
```

---

## 3. Database ER diagram

```mermaid
erDiagram
    destinations ||--|| destination_features : has
    destinations ||--o{ accommodations : located_in
    destinations ||--o{ attractions : located_in
    destinations ||--o{ weather_forecasts : forecast_for
    destinations ||--o{ transport_options : destination_of
    destinations ||--o{ trips : chosen_in
    trips ||--o{ trip_activities : contains
    attractions ||--o{ trip_activities : referenced_by
    users ||--o{ trips : owns
    fuel_prices }o--|| destinations : priced_by_country

    destinations {
        int id PK
        string city
        string country
        char country_code
        float latitude
        float longitude
    }
    destination_features {
        int destination_id PK,FK
        numeric cost_of_living
        numeric culture_score
        numeric nature_score
        numeric season_score
    }
    accommodations {
        int id PK
        int destination_id FK
        numeric price_per_night
        numeric rating
        string quality_flag
    }
    attractions {
        int id PK
        int destination_id FK
        string category
        string indoor_outdoor
        int est_duration_min
    }
    weather_forecasts {
        int id PK
        int destination_id FK
        date forecast_date
        numeric precip_prob
        bool weather_conflict
    }
    transport_options {
        int id PK
        int destination_id FK
        string mode
        numeric cost
        int duration_min
        string quality_flag
    }
    fuel_prices {
        char country_code PK
        numeric petrol_e95
        numeric diesel
        date price_date
    }
    trips {
        int id PK
        int user_id FK
        numeric budget
        int chosen_destination FK
        numeric total_cost
        numeric overall_score
    }
    trip_activities {
        int id PK
        int trip_id FK
        int attraction_id FK
        int day_number
        int slot_order
        bool was_rescheduled
    }
    users {
        int id PK
        string email
    }
```

---

## 4. Optimization pipeline

```mermaid
flowchart LR
    A["Agent outputs<br/>(per candidate)"] --> B["Normalize<br/>to 0..1"]
    B --> C["Factor scores<br/>pref / exp / weather / accom / cost / travel"]
    C --> D["Build CP-SAT model"]
    D --> E{"Hard constraints"}
    E -->|"total_cost ≤ budget"| F
    E -->|"duration, no overlaps,<br/>opening hours, weather"| F["Feasible set"]
    F --> G["Maximize objective<br/>(weighted overall_score)"]
    G --> H["Optimal destination<br/>+ itinerary"]
```

---

## 5. Provider abstraction & fallback chain

```mermaid
flowchart TD
    AG["Agent"] --> IFACE["Provider interface"]
    IFACE --> P1["Primary provider"]
    P1 -->|success| OUT["Normalized result<br/>quality_flag = real"]
    P1 -->|fail / empty / quota| P2["Fallback provider"]
    P2 -->|success| OUT2["Normalized result<br/>quality_flag = cached/real"]
    P2 -->|still unavailable| P3["Local dataset / estimate"]
    P3 --> OUT3["Result<br/>quality_flag = estimated"]
```

---

## 6. Weather-aware replanning (hybrid rules + optimization)

```mermaid
flowchart TD
    W["WeatherAgent:<br/>forecast per day"] --> R{"Rain prob ><br/>threshold?"}
    R -->|no| KEEP["Keep outdoor activities"]
    R -->|yes| FLAG["Set weather_conflict for that day"]
    FLAG --> C["CoordinatorAgent<br/>(optimizer)"]
    C --> DEC{"Feasible reshuffle?"}
    DEC -->|yes| MOVE["Move outdoor → clear day<br/>place indoor items on rainy day"]
    DEC -->|no| SWAP["Swap in indoor alternative<br/>from same destination"]
```
