"""Shared configuration for sample-graphrag scripts.

Loads credentials from .env and provides Neo4j driver, OpenAI LLM,
and OpenAI embedder instances.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
import neo4j
from neo4j_graphrag.embeddings import OpenAIEmbeddings
from neo4j_graphrag.llm import OpenAILLM

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Configure library logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("neo4j_graphrag").setLevel(logging.INFO)

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_AUTH = ("neo4j", os.environ["NEO4J_PASSWORD"])
NEO4J_DATABASE = "neo4j"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Index names — created manually after SimpleKGPipeline runs
VECTOR_INDEX_NAME = "chunkEmbeddings"
FULLTEXT_INDEX_NAME = "chunkFulltext"
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small default


def get_driver() -> neo4j.Driver:
    driver = neo4j.GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    driver.verify_connectivity()
    return driver


def get_llm() -> OpenAILLM:
    return OpenAILLM(model_name="gpt-5-mini")


def get_embedder() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model="text-embedding-3-small")
