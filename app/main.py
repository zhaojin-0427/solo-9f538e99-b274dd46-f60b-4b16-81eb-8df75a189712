"""FastAPI application: upload relation revisions and read water polygons."""

from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import Database, RevisionConflict
from .geometry import GeometryError, build_polygon_geojson, overlap_feature_collection
from .models import OverlapRequest, OverlapResult, RevisionIn, RevisionResult

def _database_path() -> str:
    # Read lazily so tests (and redeployed env) can override FLOOD_DB_PATH.
    return os.environ.get("FLOOD_DB_PATH", "/data/flood.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(_database_path())
    yield


app = FastAPI(
    title="OSM Flood Water-Area API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Submit complete, incrementing revisions of an OSM water relation "
        "(nodes + unordered outer/inner ways). Valid revisions become a "
        "GeoJSON Polygon; invalid revisions retain their raw input and an "
        "error, while the last valid polygon keeps being served."
    ),
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _stored_to_result(row: dict) -> RevisionResult:
    return RevisionResult(
        relation_id=row["relation_id"],
        revision=row["revision"],
        status=row["status"],
        geometry=row["geometry"],
        error=row["error"],
    )


@app.get("/health", tags=["meta"])
def health(request: Request) -> dict:
    db = _db(request)
    return {"status": "ok", "version": __version__, "db": db.path}


@app.post(
    "/relations/{relation_id}/revisions",
    response_model=RevisionResult,
    status_code=200,
    tags=["revisions"],
    summary="Upload one complete relation revision",
)
def upload_revision(relation_id: int, payload: RevisionIn, request: Request) -> RevisionResult:
    """Validate and store a revision.

    The raw input is always stored atomically together with its computed
    geometry/state. Geometric failure yields ``status="invalid"`` (HTTP
    200, the revision is retained); only a non-incrementing revision
    number is rejected (HTTP 409, nothing is stored).
    """
    error = None
    geometry = None
    status = "valid"
    try:
        geometry = build_polygon_geojson(payload)
    except GeometryError as exc:
        status = "invalid"
        error = exc.to_detail()

    db = _db(request)
    try:
        db.save_revision(
            relation_id=relation_id,
            revision=payload.revision,
            status=status,
            input_payload=payload.model_dump(),
            geometry=geometry,
            error=error,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "latest_revision": exc.latest,
            },
        ) from exc

    last_valid = db.get_last_valid(relation_id)
    return RevisionResult(
        relation_id=relation_id,
        revision=payload.revision,
        status=status,
        geometry=geometry,
        error=error,
        last_valid_revision=last_valid["revision"] if last_valid else None,
    )


@app.get(
    "/relations/{relation_id}/revisions/{revision}",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read input, geometry and status of one stored revision",
)
def read_revision(relation_id: int, revision: int, request: Request) -> RevisionResult:
    row = _db(request).get_revision(relation_id, revision)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "revision does not exist"},
        )
    result = _stored_to_result(row)
    result.input = row["input"]
    last_valid_row = _db(request).get_last_valid(relation_id)
    result.last_valid_revision = last_valid_row["revision"] if last_valid_row else None
    return result


@app.get(
    "/relations/{relation_id}/revisions",
    tags=["revisions"],
    summary="List all stored revisions of a relation",
)
def list_revisions(relation_id: int, request: Request) -> dict:
    return {"relation_id": relation_id, "revisions": _db(request).list_revisions(relation_id)}


@app.get(
    "/relations/{relation_id}/current",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read the currently served (latest valid) polygon",
)
def read_current(relation_id: int, request: Request) -> RevisionResult:
    """Current face semantics.

    * latest revision valid  -> serve its polygon;
    * latest revision invalid -> stop serving: 409, but the response
      points at ``last_valid_revision`` which remains fully readable via
      the per-revision endpoint;
    * a later valid revision resumes serving automatically.
    """
    db = _db(request)
    latest = db.get_latest_revision(relation_id)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no revision stored for this relation"},
        )
    last_valid = db.get_last_valid(relation_id)
    if latest["status"] == "valid":
        result = _stored_to_result(latest)
        result.last_valid_revision = last_valid["revision"] if last_valid else None
        return result

    return JSONResponse(
        status_code=409,
        content={
            "relation_id": relation_id,
            "revision": latest["revision"],
            "status": "invalid",
            "geometry": None,
            "error": latest["error"],
            "last_valid_revision": last_valid["revision"] if last_valid else None,
            "detail": (
                "current revision is invalid and the face is not being served; "
                "fetch last_valid_revision for the previous valid polygon"
            ),
        },
    )


@app.post(
    "/overlaps",
    response_model=OverlapResult,
    tags=["overlap-qc"],
    summary="Cross-relation overlap QC: partition shared water area by exact coverage",
)
def compute_overlaps(payload: OverlapRequest, request: Request) -> OverlapResult:
    """Pin each relation's current revision from one committed snapshot.

    The latest revision of every requested relation is read inside a
    single read transaction, so an upload landing while the request runs
    cannot make the result mix revisions from before and after that
    upload; ``relation_revisions`` reports the exact revision used.

    The returned GeoJSON ``FeatureCollection`` contains one feature per
    positive-area exact-coverage cell: each cell is covered by precisely
    the relation ids in ``properties.relations`` (at least two) and by no
    other input relation. Cells are mutually disjoint, so a point covered
    by three relations only appears in the three-relation feature and
    never in any pairwise one. Holes stay holes; boundary-only contact has
    no area and yields no feature.

    The whole group fails (no partial answer, no fallback to an older
    valid revision) when a relation has no stored revision (404) or its
    current revision is invalid (409).
    """
    db = _db(request)
    relation_ids = sorted(set(payload.relation_ids))

    # One read transaction -> one WAL snapshot shared by every relation.
    latest_by_relation = db.get_latest_revisions_bulk(relation_ids)

    missing = [rid for rid in relation_ids if rid not in latest_by_relation]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "relation_not_found",
                "message": "no revision stored for one or more requested relations",
                "relation_ids": missing,
            },
        )

    invalid = [
        rid for rid in relation_ids if latest_by_relation[rid]["status"] != "valid"
    ]
    if invalid:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "current_revision_invalid",
                "message": (
                    "the current revision of one or more relations is invalid; "
                    "the whole group is rejected without falling back to an "
                    "older valid revision"
                ),
                "relation_ids": invalid,
                "current_revisions": {
                    str(rid): latest_by_relation[rid]["revision"] for rid in invalid
                },
                "errors": {
                    str(rid): latest_by_relation[rid]["error"] for rid in invalid
                },
            },
        )

    pinned = {rid: latest_by_relation[rid] for rid in relation_ids}
    polygons_geojson = {rid: row["geometry"] for rid, row in pinned.items()}
    feature_collection = overlap_feature_collection(polygons_geojson)

    return OverlapResult(
        type=feature_collection["type"],
        features=feature_collection["features"],
        relation_revisions={
            str(rid): pinned[rid]["revision"] for rid in relation_ids
        },
    )
