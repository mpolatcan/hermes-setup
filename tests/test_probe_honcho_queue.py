import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "services" / "honcho" / "probe-honcho-queue.py"
spec = importlib.util.spec_from_file_location("probe_honcho_queue", SCRIPT)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeHonchoQueueTests(unittest.TestCase):
    def test_exact_threshold_is_healthy(self) -> None:
        result = probe.evaluate(
            {
                "polatcan-gaming": {"pending_work_units": 25, "in_progress_work_units": 10},
                "polatcan-finance": {"pending_work_units": 0, "in_progress_work_units": 0},
            },
            pending_threshold=25,
            in_progress_threshold=10,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pending"], 25)
        self.assertEqual(result["in_progress"], 10)

    def test_aggregate_above_threshold_alarms(self) -> None:
        result = probe.evaluate(
            {
                "polatcan-gaming": {"pending_work_units": 20, "in_progress_work_units": 4},
                "polatcan-finance": {"pending_work_units": 6, "in_progress_work_units": 0},
                "polatcan-health": {"pending_work_units": 0, "in_progress_work_units": 7},
            },
            pending_threshold=25,
            in_progress_threshold=10,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["pending"], 26)
        self.assertEqual(result["in_progress"], 11)
        self.assertEqual(result["threshold"], "pending>25,in_progress>10")


if __name__ == "__main__":
    unittest.main()
