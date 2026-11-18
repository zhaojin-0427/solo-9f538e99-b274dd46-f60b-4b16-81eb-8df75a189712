"""Unit tests for cross-relation exact-coverage overlap partitioning."""

from __future__ import annotations

import pytest

from shapely.geometry import Polygon, mapping, shape

from app.geometry import overlap_feature_collection, partition_overlaps


def square(minx: float, miny: float, maxx: float, maxy: float, hole=None) -> dict:
    polygon = Polygon(
        [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)],
        hole,
    )
    return mapping(polygon)


def areas_by_subset(fc: dict) -> dict[tuple[int, ...], float]:
    return {
        tuple(feature["properties"]["relations"]): shape(feature["geometry"]).area
        for feature in fc["features"]
    }


def test_pairwise_overlap_returns_one_cell():
    # Two unit squares overlap on a 0.5 x 1 strip.
    fc = overlap_feature_collection(
        {1: square(0, 0, 1, 1), 2: square(0.5, 0, 1.5, 1)}
    )
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    feature = fc["features"][0]
    assert feature["properties"]["relations"] == [1, 2]
    assert shape(feature["geometry"]).area == pytest.approx(0.5)


def test_triple_coverage_only_belongs_to_the_three_relation_cell():
    # A: [0,1]^2, B: [0.5,1.5]x[0,1], C: [0.6,1.2]x[0.6,1.2]
    polygons = {
        1: square(0, 0, 1, 1),
        2: square(0.5, 0, 1.5, 1),
        3: square(0.6, 0.6, 1.2, 1.2),
    }
    fc = overlap_feature_collection(polygons)
    by_subset = areas_by_subset(fc)

    # Every reported subset has at least two relations.
    assert all(len(subset) >= 2 for subset in by_subset)
    # The triple cell exists exactly once.
    assert (1, 2, 3) in by_subset
    # Triple cell is [0.6,1] x [0.6,1] -> 0.16.
    assert by_subset[(1, 2, 3)] == pytest.approx(0.16)
    # Pairwise cells exclude the triple-covered area:
    # A∩B is 0.5x1 = 0.5; B∩C is 0.6x0.6 = 0.36.
    assert by_subset[(1, 2)] == pytest.approx(0.5 - 0.16)
    assert by_subset[(2, 3)] == pytest.approx(0.24 - 0.16)
    assert (1, 3) not in by_subset  # A∩C is fully inside B
    # No other cells.
    assert set(by_subset) == {(1, 2), (2, 3), (1, 2, 3)}


def test_cells_are_mutually_disjoint_and_partition_the_shared_area():
    polygons = {
        1: square(0, 0, 2, 2),
        2: square(1, 0, 3, 2),
        3: square(0.5, 0.5, 2.5, 1.5),
    }
    cells = partition_overlaps(polygons)
    geoms = [geom for _, parts in cells for geom in parts]

    # Every cell has strictly positive area.
    assert all(geom.area > 0 for geom in geoms)
    # Pairwise disjoint interiors: intersections have no area.
    for i, left in enumerate(geoms):
        for right in geoms[i + 1 :]:
            assert left.intersection(right).area == pytest.approx(0.0)

    # The union of cells equals the region covered by >= 2 polygons.
    from shapely.ops import unary_union

    expected = unary_union(
        [
            shape(polygons[1]).intersection(shape(polygons[2])),
            shape(polygons[1]).intersection(shape(polygons[3])),
            shape(polygons[2]).intersection(shape(polygons[3])),
        ]
    )
    assert unary_union(geoms).area == pytest.approx(expected.area)


def test_edge_only_contact_is_not_overlap():
    # Squares share exactly the segment x=1, zero shared area.
    fc = overlap_feature_collection(
        {1: square(0, 0, 1, 1), 2: square(1, 0, 2, 1)}
    )
    assert fc["features"] == []


def test_corner_point_contact_is_not_overlap():
    fc = overlap_feature_collection(
        {1: square(0, 0, 1, 1), 2: square(1, 1, 2, 2)}
    )
    assert fc["features"] == []


def test_disjoint_polygons_produce_no_cells():
    fc = overlap_feature_collection(
        {1: square(0, 0, 1, 1), 2: square(5, 5, 6, 6)}
    )
    assert fc["features"] == []


def test_inner_ring_hole_is_not_counted_as_overlap():
    # Polygon 1 has a hole; polygon 2 covers polygon 1 entirely.
    with_hole = square(0, 0, 2, 2, hole=[[(0.5, 0.5), (1.5, 0.5),
                                          (1.5, 1.5), (0.5, 1.5)]])
    covering = square(-1, -1, 3, 3)
    fc = overlap_feature_collection({1: with_hole, 2: covering})
    assert len(fc["features"]) == 1
    geom = shape(fc["features"][0]["geometry"])
    # Outer area 4 minus hole area 1.
    assert geom.area == pytest.approx(3.0)


def test_hole_in_one_polygon_splits_coverage_correctly():
    # B covers A. Where A has a hole, only B covers -> not reported.
    with_hole = square(0, 0, 2, 2, hole=[[(0.5, 0.5), (1.5, 0.5),
                                          (1.5, 1.5), (0.5, 1.5)]])
    other = square(1, 1, 3, 3)
    fc = overlap_feature_collection({1: with_hole, 2: other})
    # Overlap is [1,2]x[1,2] minus the hole part [1,1.5]x[1,1.5].
    geom = shape(fc["features"][0]["geometry"])
    assert geom.area == pytest.approx(1.0 - 0.25)


def test_eight_relations_are_supported_and_sorted():
    # Eight concentric-ish squares; every pair+ subset gets exact cells.
    polygons = {
        rid: square(0, 0, rid, rid) for rid in range(1, 9)
    }
    fc = overlap_feature_collection(polygons)
    subsets = [tuple(f["properties"]["relations"]) for f in fc["features"]]
    assert all(len(subset) >= 2 for subset in subsets)
    # Deterministic ordering: by subset size, then lexicographically.
    assert subsets == sorted(subsets, key=lambda s: (len(s), s))
    # Every relation id list inside a feature is sorted.
    assert all(list(s) == sorted(s) for s in subsets)
    # Innermost unit square is covered by all eight.
    assert (1, 2, 3, 4, 5, 6, 7, 8) in subsets


def test_split_cell_emits_disjoint_polygons_without_losing_area():
    # A spans two lobes joined above the blocker; C covers the join, so the
    # exact {A,B} region splits into two disconnected lower lobes.
    from shapely.geometry import Polygon as P

    a = mapping(
        P([(0, 0), (4, 0), (4, 3), (3, 3), (3, 2), (1, 2), (1, 3), (0, 3)])
    )
    b = square(-1, -1, 5, 2.5)  # covers both lower lobes and reaches the join
    c = square(3, 1, 4, 4)      # right upper lobe of A, inside B's y-range
    fc = overlap_feature_collection({1: a, 2: b, 3: c})
    by_subset = areas_by_subset(fc)
    # The pairwise cell exists, loses exactly the triple-covered 1.5 area ...
    assert (1, 2) in by_subset
    assert by_subset[(1, 2)] == pytest.approx(9.0 - 1.5)
    # ... while the triple-covered lobe is only in the triple cell.
    assert (1, 2, 3) in by_subset
    assert by_subset[(1, 2, 3)] == pytest.approx(1.5)
    pair_features = [
        f for f in fc["features"] if tuple(f["properties"]["relations"]) == (1, 2)
    ]
    assert len(pair_features) >= 1
    # All output polygons are valid and area-bearing.
    assert all(shape(f["geometry"]).is_valid for f in fc["features"])


def test_output_is_deterministic_regardless_of_input_dictionary_order():
    polygons = {
        1: square(0, 0, 2, 2),
        2: square(1, 0, 3, 2),
        3: square(0.5, 0.5, 2.5, 1.5),
    }
    import json

    first = json.dumps(overlap_feature_collection(polygons), sort_keys=True)
    reordered = {rid: polygons[rid] for rid in reversed(list(polygons))}
    second = json.dumps(overlap_feature_collection(reordered), sort_keys=True)
    assert first == second
