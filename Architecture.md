flowchart TD
  U[User request] -->|HTTP| S[gemini/server]
  S --> L[core/loop]
  
  %% Intent + Recipe
  L --> IP[intent_parser]
  L --> RE[recipe_expander]
  IP --> RE
  RE --> ORCH[orchestrator]
  
  %% Orchestrator connections
  ORCH --> B[core/browser]
  B --> VS[fused_vision]
  VS --> SE[state_evaluator]
  SE --> ORCH
  
  ORCH --> PS[product_selector]
  ORCH --> CC[cart_checker]
  ORCH --> SC[substitution_agent]
  ORCH --> PC[product_confirmer]
  ORCH --> V[verifier]
  V -->|final status| L
  
  %% Browser cycles
  ORCH --> B
  B --> VS
  VS --> ORCH
  
  %% Frontend and sessions
  L -->|SSE updates| FE[frontend UI]
  B -->|screenshot/session| FS[sessions/screenshots]
  
  %% LLM hints
  ORCH --> GC[gemini/core/client (LLM)]
  GC -->|text+vision hints| ORCH
