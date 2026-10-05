from django.test import SimpleTestCase
from .risk import calculate


class RiskCalculationTests(SimpleTestCase):
    def test_score_and_category(self):
        cases = {
            (1, 1): (1, 'R'), (2, 2): (4, 'R'),
            (2, 3): (6, 'M'), (3, 3): (9, 'M'),
            (2, 5): (10, 'T'), (4, 4): (16, 'T'),
            (4, 5): (20, 'E'), (5, 5): (25, 'E'),
        }
        for (l, s), (score, cat) in cases.items():
            with self.subTest(l=l, s=s):
                got = calculate(l, s)
                self.assertEqual((got[0], got[1]), (score, cat))

    def test_acceptable_gate(self):
        self.assertTrue(calculate(2, 3)[2])    # Rt 6, now the highest acceptable
        self.assertFalse(calculate(2, 4)[2])   # Rt 8
        self.assertFalse(calculate(5, 5)[2])   # Rt 25