"""Shared configuration for sample-graphrag scripts.

Loads credentials from .env and provides factory functions for the three
core dependencies every script needs: a Neo4j driver, an OpenAI LLM, and
an OpenAI embedder. Centralizing these here means credentials and model
choices live in one place.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
import neo4j
from neo4j_graphrag.embeddings import OpenAIEmbeddings
from neo4j_graphrag.llm import OpenAILLM

# Load .env from project root (one directory above src/)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Enable the neo4j-graphrag library's built-in logging so you can see
# what the pipeline is doing (chunking, extraction calls, index creation).
# Set to DEBUG for full request/response details.
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("neo4j_graphrag").setLevel(logging.INFO)

# Neo4j connection details from environment variables.
# NEO4J_URI is the Bolt protocol address (e.g. neo4j://localhost:7687
# for local, or neo4j+s://xxxx.databases.neo4j.io for Aura).
NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_AUTH = ("neo4j", os.environ["NEO4J_PASSWORD"])
NEO4J_DATABASE = "neo4j"

# LLM model exposed by the configured OpenAI-compatible endpoint.
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-5-mini")

# Embedding settings must match both the provider's model and the Neo4j vector
# index. DashScope's text-embedding-v4 defaults to 1024 dimensions; OpenAI's
# text-embedding-3-small defaults to 1536.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))

# Path to the data/ directory containing sample documents
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Index names — these are created in step 01 after SimpleKGPipeline
# populates the graph. Steps 02-05 reference them for retrieval.
VECTOR_INDEX_NAME = "chunkEmbeddings"
FULLTEXT_INDEX_NAME = "chunkFulltext"

def get_driver() -> neo4j.Driver:
    """Create a Neo4j driver and verify the connection is reachable.

    Returns a driver that can be used as a context manager:
        with get_driver() as driver:
            ...
    """
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    # Fail fast if Neo4j isn't running or credentials are wrong,
    # rather than getting a cryptic error during pipeline execution.
    driver.verify_connectivity()
    return driver


def get_llm() -> OpenAILLM:
    """Create an OpenAI LLM instance for entity extraction and answer generation.

    The model is configured through MODEL_NAME so OpenAI-compatible providers
    can select one of their own chat models.
    """
    return OpenAILLM(model_name=MODEL_NAME)


def get_embedder() -> OpenAIEmbeddings:
    """Create an embedder using the configured OpenAI-compatible model."""
    return OpenAIEmbeddings(model=EMBEDDING_MODEL)
