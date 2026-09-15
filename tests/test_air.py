import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from air import AirGestures, GESTURES


class AirGestureTests(unittest.TestCase):
    def setUp(self):
        self.acted = []
        self.air = AirGestures(self.acted.append)

    def press(self, *moves):
        self.air.button(True)
        for dx, dy in moves:
            self.air.motion(dx, dy)
        self.air.button(False)

    def test_a_still_press_is_a_click(self):
        self.press((0.05, -0.02), (-0.03, 0.01))
        self.assertEqual(self.acted, ["click"])
        self.assertEqual(self.air.recognised, 1)

    def test_flicks_name_their_direction(self):
        for moves, expected in (
                ([(0.0, -0.4)] * 5, "up"),
                ([(0.0, 0.4)] * 5, "down"),
                ([(-0.4, 0.0)] * 5, "left"),
                ([(0.4, 0.05)] * 5, "right")):
            self.acted.clear()
            self.press(*moves)
            self.assertEqual(self.acted, [expected])

    def test_a_loop_is_a_circle_and_its_sign_is_the_direction(self):
        # A square path, clockwise on a screen whose y points down.
        clockwise = [(1.0, 0.0)] * 2 + [(0.0, 1.0)] * 2 + [(-1.0, 0.0)] * 2 + [(0.0, -1.0)] * 2
        self.press(*clockwise)
        self.assertEqual(self.acted, ["clockwise"])
        self.acted.clear()
        self.press(*[(-dx, -dy) for dx, dy in reversed(clockwise)])
        self.assertEqual(self.acted, ["counterclockwise"])

    def test_motion_without_the_button_is_ignored(self):
        self.air.motion(5.0, 5.0)
        self.press()
        self.assertEqual(self.acted, ["click"])

    def test_a_release_without_a_press_does_nothing(self):
        self.air.button(False)
        self.assertEqual(self.acted, [])

    def test_reset_drops_a_press_in_progress(self):
        self.air.button(True)
        self.air.motion(2.0, 0.0)
        self.air.reset()
        self.air.button(False)
        self.assertEqual(self.acted, [])

    def test_nonsense_samples_are_dropped(self):
        self.air.button(True)
        for bad in ((float("nan"), 0.0), (0.0, float("inf")), ("2", 0.0), (True, 1.0)):
            self.air.motion(*bad)
        self.air.button(False)
        self.assertEqual(self.acted, ["click"])
        self.assertEqual(self.air.last[4], 0)

    def test_what_a_gesture_measured_is_reported(self):
        self.press(*[(0.0, -0.5)] * 4)
        name, sx, sy, area, samples = self.air.last
        self.assertEqual((name, sx, sy, samples), ("up", 0.0, -2.0, 4))
        self.assertIn(name, GESTURES)

    def test_thresholds_must_be_positive(self):
        with self.assertRaises(ValueError):
            AirGestures(lambda name: None, threshold=0)


if __name__ == "__main__":
    unittest.main()
