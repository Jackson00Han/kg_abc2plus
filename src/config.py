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

# Path to the data/ directory containing sample documents
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Index names — these are created in step 01 after SimpleKGPipeline
# populates the graph. Steps 02-05 reference them for retrieval.
VECTOR_INDEX_NAME = "chunkEmbeddings"
FULLTEXT_INDEX_NAME = "chunkFulltext"

# text-embedding-3-small produces 1536-dimensional vectors.
# The vector index must be created with matching dimensions.
EMBEDDING_DIMENSIONS = 1536


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

    gpt-5-mini is used for both the SimpleKGPipeline (structured entity
    extraction) and the GraphRAG answer generation step.
    """
    return OpenAILLM(model_name="gpt-5-mini")


def get_embedder() -> OpenAIEmbeddings:
    """Create an OpenAI embedder for chunk and query embedding.

    text-embedding-3-small is a cost-effective model that produces
    1536-dimensional vectors suitable for cosine similarity search.
    """
    return OpenAIEmbeddings(model="text-embedding-3-small")
