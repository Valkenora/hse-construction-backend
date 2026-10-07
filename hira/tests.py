from django.test import SimpleTestCase

from .risk import ACCEPTABLE_MAX_RT, calculate


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
        self.assertEqual(ACCEPTABLE_MAX_RT, 4)
        self.assertTrue(calculate(2, 2)[2])     # Rt 4: highest acceptable
        self.assertFalse(calculate(1, 5)[2])    # Rt 5
        self.assertFalse(calculate(2, 3)[2])    # Rt 6
        self.assertFalse(calculate(5, 5)[2])    # Rt 25

    def test_sample_penyimpanan_bekisting(self):
        """Values from the company's filled sample: (severity, likelihood) -> expected."""
        initial = [
            ((4, 2), (8, 'M', False)),
            ((3, 3), (9, 'M', False)),
            ((3, 3), (9, 'M', False)),
            ((4, 3), (12, 'T', False)),
        ]
        residual = [
            ((2, 1), (2, 'R', True)),
            ((2, 1), (2, 'R', True)),
            ((2, 1), (2, 'R', True)),
            ((2, 2), (4, 'R', True)),
        ]
        for (s, l), expected in initial + residual:
            with self.subTest(s=s, l=l):
                self.assertEqual(calculate(l, s), expected)