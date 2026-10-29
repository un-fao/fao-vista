from __future__ import annotations

import unittest

import numpy as np

from core.phase1_local.process_dekad_composites import (
    DEKAD_AGGREGATION_PRIORITY,
    aggregate_classified_blocks,
)


def aggregate_pixel(*values: int) -> int:
    blocks = [np.array([[value]], dtype=np.uint8) for value in values]
    return int(aggregate_classified_blocks(blocks)[0, 0])


class DekadAggregationTests(unittest.TestCase):
    def test_priority_order_is_the_published_rule(self) -> None:
        # Highest priority last: 3 > 2 > 1 > 6 > 5 > 4 > 10 > 11 > 9 > 0.
        self.assertEqual(DEKAD_AGGREGATION_PRIORITY.tolist(), [0, 9, 11, 10, 4, 5, 6, 1, 2, 3])

    def test_flood_class_beats_higher_numeric_codes(self) -> None:
        # Not a numeric maximum: low flood intensity wins over permanent water.
        self.assertEqual(aggregate_pixel(11, 1), 1)
        self.assertEqual(aggregate_pixel(10, 9, 6), 6)

    def test_higher_flood_intensity_wins(self) -> None:
        self.assertEqual(aggregate_pixel(1, 3, 2), 3)
        self.assertEqual(aggregate_pixel(4, 6, 5), 6)

    def test_every_pair_follows_the_priority(self) -> None:
        rank = {int(value): position for position, value in enumerate(DEKAD_AGGREGATION_PRIORITY)}
        for a in rank:
            for b in rank:
                expected = a if rank[a] >= rank[b] else b
                self.assertEqual(aggregate_pixel(a, b), expected, msg=f"inputs {a}, {b}")

    def test_single_composite_is_unchanged(self) -> None:
        for value in DEKAD_AGGREGATION_PRIORITY:
            self.assertEqual(aggregate_pixel(int(value)), int(value))


if __name__ == "__main__":
    unittest.main()
