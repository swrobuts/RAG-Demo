from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DB
    postgres_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag_apple"
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme"

    # LLM — Gemini
    gemini_api_key: str = ""
    gemini_chat_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embedding_dim: int = 768

    # LLM — local (LM Studio, OpenAI-compatible)
    local_llm_url: str = "http://localhost:1234/v1"
    local_llm_model: str = "google/gemma-3-12b"

    # Source
    wikipedia_url: str = "https://de.wikipedia.org/wiki/Apple"
    wikipedia_user_agent: str = "UC5-RAG-Apple/0.1 (kontakt@example.com)"

    # Limits
    max_llm_calls_per_query: int = 8
    max_tokens_per_call: int = 4096

    # UE1
    ue1_chunk_tokens: int = Field(default=400)
    ue1_chunk_overlap: int = Field(default=50)
    ue1_top_k: int = Field(default=8)

    # UE2 — PageIndex + classical RAG hybrid
    ue2_max_depth: int = Field(default=3)
    ue2_nodes_per_level: int = Field(default=4)
    ue2_top_k: int = Field(default=8)

    # Retrieval pipeline (shared by UE1 & UE2)
    pipeline_initial_k: int = Field(default=30)   # dense+bm25 top-K before fusion
    pipeline_rrf_k: int = Field(default=60)        # RRF constant (Cormack et al. default)
    pipeline_rerank_enabled: bool = Field(default=True)
    pipeline_rerank_top_n: int = Field(default=15) # how many to send to the reranker
    pipeline_mmr_enabled: bool = Field(default=True)
    pipeline_mmr_lambda: float = Field(default=0.7)  # 1.0 = relevance only, 0.0 = diversity only

    # UE3 — GraphRAG
    ue3_top_k_entities: int = Field(default=8)     # how many entities to surface per query (local mode)
    ue3_top_k_communities: int = Field(default=3)  # global mode
    ue3_top_k_chunks: int = Field(default=8)
    ue3_default_mode: str = Field(default="hybrid")  # local | global | hybrid
    ue3_max_chunks_per_entity: int = Field(default=4)  # cap how many MENTIONS chunks per matched entity

    # UE4 — Ontology-RAG (OWL/SPARQL via GraphDB)
    graphdb_url: str = Field(default="http://graphdb:7200")
    graphdb_repo: str = Field(default="uc5_rag_apple")
    graphdb_user: str = Field(default="")          # leave empty if no auth
    graphdb_password: str = Field(default="")
    ue4_top_k_results: int = Field(default=20)     # SPARQL result rows to pull
    ue4_max_chunks_per_entity: int = Field(default=2)  # supplemental text chunks


@lru_cache
def get_settings() -> Settings:
    return Settings()
