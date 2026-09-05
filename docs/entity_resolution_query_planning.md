# Entity-resolution query planning

The industrial workbench exposed a cold-query failure in exact identity-property
matching. A first Equipment lookup reached the existing 105-second HTTP deadline.
Neo4j still reported its identity count query as `planning` after 4 minutes
34 seconds, with zero page hits and zero page faults. A subsequent request started
another copy of that planning work. This was a query-planning problem, not a
failure to match `EquipmentCode`, an extraction error, or a large result set.

## Query design

`knowledge/entity_resolution.py` now separates its Cypher patterns with
`WITH DISTINCT` query parts:

1. Bind the active publication, published authoritative mentions, and entities.
2. Bind each current record head and exact source Chunk.
3. Bind the active source snapshot and document version.
4. Validate the active published T-Box and all source, evidence, and ACL fields.
5. Evaluate the exact-match predicate against those bindings. For identity
   properties, the fact/subject patterns and fact provenance checks also occupy
   separate query parts.

The change uses standard Cypher variable scoping and duplicate removal, with the
default planner. See Neo4j's [WITH documentation](https://neo4j.com/docs/cypher-manual/5/clauses/with/).
The observed reduction in planning time comes from these explicit query parts;
no server resource, HTTP timeout, provider, or matching-rule change is required.

Every former match and validation predicate remains in place. In particular:

- Tenant, group, current record, active source, active publication, and active
  T-Box restrictions apply before results can be returned.
- Identity facts retain their exact datatype, canonical value, unit, supported
  mention, source metadata, ACL, and quoted-text checks.
- The count covers all authorized matching entities, with no candidate limit.
  Multiple mentions of one entity count once; two distinct matching entities
  remain a conflict.
- Only a unique result permits the second query to fetch a target. That query
  checks the count's publication ID, activation generation, and entity ID and
  repeats the evidence and authorization predicates in the same read transaction.
- The five-item limit applies only to the target's evidence projection.

The shared authority query also serves canonical-key matches, governed aliases,
and authorized candidate listings; those paths use the same preserved predicates.
`DISTINCT` removes repeated bindings, not distinct entity identities, publications,
or evidence mentions.

## Development evidence and repeatable checks

A first execution of both new query texts against the same development database
and unchanged 1 CPU / 1.5 GiB limit returned one authoritative Equipment match:
the count took 1.818 seconds and the evidence fetch took 2.013 seconds. These are
individual development observations, not a production latency guarantee.
The independent four-test integration run passed with a cleared-cache combined
count/fetch time of 7.588 seconds. The final development-suite observation was
4.267 seconds with the same cleared cache and 30-second transaction/wall-time
bound. These observations are retained in the workbench validation record.

The focused unit tests preserve exact-match projection, global conflict counts,
and publication-generation binding:

```sh
uv run --locked python -m unittest tests.unit.test_entity_resolution tests.unit.test_api_knowledge
```

`tests/integration/test_identity_resolution_neo4j.py` exercises the real count and
fetch queries with an explicitly cold query cache, a 30-second test transaction
and wall-time bound, exact values, isolation, duplicate identities, and invalid
source evidence. The repository's disposable database runner includes this suite:

```sh
./scripts/run_stage8_neo4j_tests.sh /tmp/identity-integration.json /tmp/identity-observations
```

Run the full documented Stage 8 workflow when recording a revised baseline.
Its results, like the focused checks above, remain development validation.

The HTTP runtime still releases a running worker only when its backend work
actually finishes; an HTTP timeout does not forcibly stop arbitrary driver work.
This fix removes the demonstrated planning pathology without changing that
existing concurrency and cancellation behavior. Repeated retries are not the
remedy for a recurrence of the original failure.
