"""End-to-end tests for the cross-relation overlap QC API."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.database import Database
from app.geometry import build_polygon_geojson
from app.main import app
from app.models import RevisionIn

from .factories import node, revision, way


def post_revision(client: TestClient, relation_id: int, payload: dict):
    return client.post(f"/relations/{relation_id}/revisions", json=payload)


def square_payload(rev: int, minx: float, miny: float, maxx: float, maxy: float,
                   start_node: int = 1, way_id: int = 10) -> dict:
    """Build a valid one-way square revision in the given coordinate box."""
    nids = list(range(start_node, start_node + 4))
    nodes = [
        node(nids[0], minx, miny),
        node(nids[1], maxx, miny),
        node(nids[2], maxx, maxy),
        node(nids[3], minx, maxy),
    ]
    return revision(rev, nodes, [way(way_id, nids + [nids[0]])])


def upload_square(client, relation_id: int, rev: int, box, **kwargs) -> None:
    resp = post_revision(client, relation_id, square_payload(rev, *box, **kwargs))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "valid"


# ---------------------------------------------------------------- happy path
def test_overlap_returns_feature_collection_with_pinned_revisions(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r2, 1, (0.5, 0, 1.5, 1))

    resp = client.post("/overlaps", json={"relation_ids": [r2, r1]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 1
    feature = body["features"][0]
    assert feature["properties"]["relations"] == sorted([r1, r2])
    # Both relations are pinned at their current revision.
    assert body["relation_revisions"] == {str(r1): 1, str(r2): 1}


def test_overlap_pins_each_relations_current_revision(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r1, 2, (0, 0, 2, 2))
    upload_square(client, r2, 1, (1, 1, 3, 3))

    body = client.post("/overlaps", json={"relation_ids": [r1, r2]}).json()
    # Revision 2 (the 2x2 square) is pinned, not revision 1.
    assert body["relation_revisions"] == {str(r1): 2, str(r2): 1}
    # Overlap of [0,2]^2 and [1,3]^2 is the unit square [1,2]x[1,2].
    from shapely.geometry import shape

    assert len(body["features"]) == 1
    assert shape(body["features"][0]["geometry"]).area == 1.0


def test_triple_coverage_excludes_pairwise_double_counting(
    client: TestClient, relation_id: int
):
    r1, r2, r3 = (relation_id, relation_id + 1, relation_id + 2)
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r2, 1, (0.5, 0, 1.5, 1))
    upload_square(client, r3, 1, (0.6, 0.6, 1.2, 1.2))

    body = client.post(
        "/overlaps", json={"relation_ids": [r1, r2, r3]}
    ).json()
    subsets = {tuple(f["properties"]["relations"]) for f in body["features"]}
    assert (r1, r2, r3) in subsets
    # No single-relation or empty subsets; triple zone never reappears pairwise.
    assert all(len(s) >= 2 for s in subsets)

    from shapely.geometry import shape

    triple = next(
        f for f in body["features"] if tuple(f["properties"]["relations"]) == (r1, r2, r3)
    )
    assert shape(triple["geometry"]).area == 0.4 * 0.4
    pair_12 = next(
        f for f in body["features"] if tuple(f["properties"]["relations"]) == (r1, r2)
    )
    # Pairwise cell is the 0.5 strip minus the 0.16 triple zone.
    assert shape(pair_12["geometry"]).area == 0.5 - 0.16


def test_edge_only_contact_returns_empty_feature_collection(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r2, 1, (1, 0, 2, 1))

    resp = client.post("/overlaps", json={"relation_ids": [r1, r2]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["features"] == []
    assert body["relation_revisions"] == {str(r1): 1, str(r2): 1}


def test_no_overlap_area_is_empty_but_still_reports_revisions(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r2, 1, (5, 5, 6, 6))

    body = client.post("/overlaps", json={"relation_ids": [r1, r2]}).json()
    assert body["features"] == []
    assert set(body["relation_revisions"]) == {str(r1), str(r2)}


def test_inner_ring_hole_is_not_counted_twice(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    # r1: 2x2 square with a 1x1 hole; r2: covers r1 entirely.
    hole_nodes = [
        node(5, 0.5, 0.5),
        node(6, 1.5, 0.5),
        node(7, 1.5, 1.5),
        node(8, 0.5, 1.5),
    ]
    outer = square_payload(1, 0, 0, 2, 2)
    with_hole = revision(
        1,
        outer["nodes"] + hole_nodes,
        [outer["ways"][0], way(20, [5, 6, 7, 8, 5], role="inner")],
    )
    assert post_revision(client, r1, with_hole).json()["status"] == "valid"
    upload_square(client, r2, 1, (-1, -1, 3, 3))

    from shapely.geometry import shape

    body = client.post("/overlaps", json={"relation_ids": [r1, r2]}).json()
    assert len(body["features"]) == 1
    assert shape(body["features"][0]["geometry"]).area == 4.0 - 1.0


# ------------------------------------------------------------- error semantics
def test_unknown_relation_makes_the_whole_group_404(
    client: TestClient, relation_id: int
):
    r1, missing = relation_id, relation_id + 99
    upload_square(client, r1, 1, (0, 0, 1, 1))

    resp = client.post("/overlaps", json={"relation_ids": [r1, missing]})
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "relation_not_found"
    assert detail["relation_ids"] == [missing]


def test_invalid_current_revision_rejects_group_without_fallback(
    client: TestClient, relation_id: int
):
    r1, r2 = relation_id, relation_id + 1
    # r1: valid rev 1, then invalid rev 2 becomes the current revision.
    upload_square(client, r1, 1, (0, 0, 1, 1))
    bad = revision(2, [node(1, 0, 0)], [way(10, [1, 2, 3, 99, 1])])
    bad_resp = post_revision(client, r1, bad)
    assert bad_resp.json()["status"] == "invalid"
    upload_square(client, r2, 1, (0.5, 0, 1.5, 1))

    resp = client.post("/overlaps", json={"relation_ids": [r1, r2]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "current_revision_invalid"
    assert detail["relation_ids"] == [r1]
    assert detail["current_revisions"] == {str(r1): 2}
    # The stale valid revision 1 must not be used as a fallback.
    assert "features" not in resp.json()
    # The single-relation supply path remains untouched.
    current = client.get(f"/relations/{r1}/current")
    assert current.status_code == 409
    old = client.get(f"/relations/{r1}/revisions/1")
    assert old.status_code == 200 and old.json()["geometry"] is not None


def test_every_relation_must_exist_no_partial_answer(
    client: TestClient, relation_id: int
):
    r1, r2, r3 = relation_id, relation_id + 1, relation_id + 2
    upload_square(client, r1, 1, (0, 0, 1, 1))
    upload_square(client, r2, 1, (0.5, 0, 1.5, 1))
    # r3 never uploaded.
    resp = client.post("/overlaps", json={"relation_ids": [r1, r2, r3]})
    assert resp.status_code == 404
    assert resp.json()["detail"]["relation_ids"] == [r3]


# ------------------------------------------------------------- input validation
def test_relation_count_bounds(client: TestClient, relation_id: int):
    too_few = client.post("/overlaps", json={"relation_ids": [relation_id]})
    assert too_few.status_code == 422

    too_many = client.post(
        "/overlaps", json={"relation_ids": list(range(100, 109))}
    )
    assert too_many.status_code == 422


def test_duplicate_relation_ids_rejected(client: TestClient, relation_id: int):
    resp = client.post(
        "/overlaps", json={"relation_ids": [relation_id, relation_id]}
    )
    assert resp.status_code == 422


def test_up_to_eight_relations_accepted_when_all_known(
    client: TestClient, relation_id: int
):
    ids = [relation_id + i for i in range(8)]
    for i, rid in enumerate(ids):
        upload_square(client, rid, 1, (0, 0, i + 1, i + 1))
    resp = client.post("/overlaps", json={"relation_ids": ids})
    assert resp.status_code == 200
    assert len(resp.json()["relation_revisions"]) == 8


# ------------------------------------------------------------- snapshot pinning
def test_upload_during_request_cannot_mix_pre_and_post_upload_revisions(
    client: TestClient, db: Database, relation_id: int
):
    """Deterministic snapshot-pinning test.

    A barrier holds the overlap request inside its single bulk read until
    a writer has committed a new revision. The read transaction must keep
    its pre-upload WAL snapshot, so the response can only cite revisions
    that existed before the upload began.
    """
    r1, r2 = relation_id, relation_id + 1
    upload_square(client, r1, 1, (0, 0, 10, 10))
    upload_square(client, r2, 1, (0, 0, 10, 10))

    # Events rather than a Barrier: the TestClient dispatches requests to
    # anyio worker threads, so the two synchronization points do not run on
    # a fixed pair of OS threads.
    snapshot_open = threading.Event()
    writer_done = threading.Event()
    original_bulk = db.get_latest_revisions_bulk

    def instrumented_bulk(ids, inside_snapshot=None):
        # The writer path uses save_revision, never this method, so only
        # the overlap reader is held -- after its snapshot-acquiring read.
        def hold_snapshot():
            snapshot_open.set()
            assert writer_done.wait(timeout=10)
        return original_bulk(ids, inside_snapshot=hold_snapshot)

    db.get_latest_revisions_bulk = instrumented_bulk  # type: ignore[method-assign]
    result: dict = {}

    def reader() -> None:
        reader_client = TestClient(app)
        reader_client.__enter__()
        app.state.db = db
        try:
            result["body"] = reader_client.post(
                "/overlaps", json={"relation_ids": [r1, r2]}
            ).json()
        finally:
            reader_client.__exit__(None, None, None)

    thread = threading.Thread(target=reader)
    thread.start()
    assert snapshot_open.wait(timeout=10)   # reader's snapshot is pinned
    # Commit a new revision while the reader's transaction stays open.
    upload_square(client, r1, 2, (0, 0, 10, 10))
    writer_done.set()                       # release the reader
    thread.join(timeout=10)
    assert not thread.is_alive()

    # The response must cite the pre-upload revision 1, never revision 2.
    assert result["body"]["relation_revisions"] == {str(r1): 1, str(r2): 1}
    # A fresh request after the upload naturally sees revision 2.
    later = client.post("/overlaps", json={"relation_ids": [r1, r2]}).json()
    assert later["relation_revisions"][str(r1)] == 2


def test_bulk_read_returns_one_latest_row_per_relation_from_one_call(
    db_path: str, relation_id: int
):
    database = Database(db_path)
    r1, r2, r3 = relation_id, relation_id + 1, relation_id + 2
    payload = square_payload(1, 0, 0, 1, 1)
    geometry = build_polygon_geojson(RevisionIn(**payload))
    database.save_revision(r1, 1, "valid", payload, geometry, None)
    database.save_revision(r1, 2, "valid", payload, geometry, None)
    database.save_revision(r2, 1, "valid", payload, geometry, None)
    # r3 has an invalid current revision.
    database.save_revision(r3, 1, "invalid", payload, None,
                           {"code": "missing_nodes", "message": "x", "way_ids": []})

    rows = database.get_latest_revisions_bulk([r1, r2, r3, r3 + 99])
    assert set(rows) == {r1, r2, r3}
    assert rows[r1]["revision"] == 2 and rows[r1]["status"] == "valid"
    assert rows[r2]["revision"] == 1
    assert rows[r3]["status"] == "invalid"


def test_single_relation_endpoints_remain_available(client: TestClient,
                                                    relation_id: int):
    upload_square(client, relation_id, 1, (0, 0, 1, 1))
    assert client.get(f"/relations/{relation_id}/current").status_code == 200
    assert client.get(f"/relations/{relation_id}/revisions").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/overlaps" in schema["paths"]
    assert "/relations/{relation_id}/current" in schema["paths"]
