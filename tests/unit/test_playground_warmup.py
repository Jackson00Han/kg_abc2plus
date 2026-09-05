"""Bounded recovery for Neo4j query compilation during Playground startup."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from graphrag_prod.playground import PLAYGROUND_RETRIEVAL_LIMITS
from graphrag_prod.retrieval import RetrievalBackendTimeout, RetrievalBackendUnavailable
from scripts.run_playground import _warm_retrieval


def _fixture() -> SimpleNamespace:
    return SimpleNamespace(
        plans=tuple(
            SimpleNamespace(tenant_id=tenant) for tenant in ("alpha", "beta")
        ),
        build=SimpleNamespace(
            questions=tuple(
                {
                    "query": f"question for {tenant}",
                    "answerable": True,
                    "principal": {
                        "tenant_id": tenant,
                        "principal_id": f"expert-{tenant}",
                        "groups": [f"{tenant}-readers"],
                    },
                }
                for tenant in ("alpha", "beta")
            )
        ),
    )


def _result(
    tenant: str, *, chunks: tuple[object, ...] = (object(),)
) -> SimpleNamespace:
    return SimpleNamespace(trace=SimpleNamespace(tenant_id=tenant), chunks=chunks)


def _embedder() -> Mock:
    return Mock(
        embed=Mock(
            return_value=SimpleNamespace(
                vector=(1.0, 0.0), embedding_space_id="warmup-space"
            )
        )
    )


class PlaygroundWarmupTests(unittest.TestCase):
    def test_one_timeout_retries_the_same_read_and_embeds_each_tenant_once(
        self,
    ) -> None:
        engine = Mock(
            retrieve=Mock(
                side_effect=[
                    RetrievalBackendTimeout(), _result("alpha"), _result("beta")
                ]
            )
        )
        embedder = _embedder()

        _warm_retrieval(engine, _fixture(), embedder)

        self.assertEqual(engine.retrieve.call_count, 3)
        first, repeated, second_tenant = [
            item.args[0] for item in engine.retrieve.call_args_list
        ]
        self.assertIs(first, repeated)
        self.assertEqual(first.principal.tenant_id, "alpha")
        self.assertEqual(first.principal.groups, frozenset({"alpha-readers"}))
        self.assertEqual(second_tenant.principal.tenant_id, "beta")
        self.assertEqual(first.query_vector, (1.0, 0.0))
        self.assertEqual(first.query_embedding_space_id, "warmup-space")
        self.assertIs(first.limits, PLAYGROUND_RETRIEVAL_LIMITS)
        self.assertEqual(
            embedder.embed.call_args_list,
            [
                call("question for alpha", tenant_id="alpha"),
                call("question for beta", tenant_id="beta"),
            ],
        )

    def test_second_timeout_fails_startup_without_another_provider_call(self) -> None:
        first = RetrievalBackendTimeout()
        second = RetrievalBackendTimeout()
        engine = Mock(retrieve=Mock(side_effect=[first, second]))
        embedder = _embedder()

        with self.assertRaises(RetrievalBackendTimeout) as caught:
            _warm_retrieval(engine, _fixture(), embedder)

        self.assertIs(caught.exception, second)
        self.assertEqual(engine.retrieve.call_count, 2)
        embedder.embed.assert_called_once_with("question for alpha", tenant_id="alpha")

    def test_non_timeout_retrieval_error_is_not_retried(self) -> None:
        for failure in (
            RetrievalBackendUnavailable(), RuntimeError("invalid retrieval state")
        ):
            with self.subTest(failure=type(failure).__name__):
                engine = Mock(retrieve=Mock(side_effect=failure))
                embedder = _embedder()
                with self.assertRaises(type(failure)) as caught:
                    _warm_retrieval(engine, _fixture(), embedder)
                self.assertIs(caught.exception, failure)
                self.assertEqual(engine.retrieve.call_count, 1)
                self.assertEqual(embedder.embed.call_count, 1)

    def test_provider_timeout_is_not_retried_as_a_database_warmup(self) -> None:
        engine = Mock()
        failure = TimeoutError("provider deadline")
        embedder = Mock(embed=Mock(side_effect=failure))
        with self.assertRaises(TimeoutError) as caught:
            _warm_retrieval(engine, _fixture(), embedder)
        self.assertIs(caught.exception, failure)
        self.assertEqual(embedder.embed.call_count, 1)
        engine.retrieve.assert_not_called()

    def test_retried_result_must_still_prove_tenant_and_nonempty_context(self) -> None:
        for invalid in (_result("other-tenant"), _result("alpha", chunks=())):
            with self.subTest(result=invalid):
                engine = Mock(
                    retrieve=Mock(side_effect=[RetrievalBackendTimeout(), invalid])
                )
                with self.assertRaisesRegex(
                    RuntimeError, "retrieval warm-up failed: alpha"
                ):
                    _warm_retrieval(engine, _fixture(), _embedder())
                self.assertEqual(engine.retrieve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
