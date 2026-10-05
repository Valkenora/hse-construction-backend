"""Single source of truth for HIRA risk scoring."""

SCORE_MIN, SCORE_MAX = 1, 5

# Initial-risk category by Rt = Likelihood x Severity (upper bounds, inclusive).
# PROVISIONAL: derived from the matrix colours on form F.01/P.01. Not confirmed by client.
CATEGORY_UPPER_BOUNDS = (('R', 4), ('M', 9), ('T', 16))  # anything higher -> 'E'

# Per form F.01/P.01 legend: acceptable if Rt <= 6, urgent action if Rt > 10.
# (Confirmed: the form overrides the PRD's "residual <= 4" and the prototype.)
ACCEPTABLE_MAX_RT = 6
URGENT_MIN_RT = 11


def categorize(score):
    for code, upper in CATEGORY_UPPER_BOUNDS:
        if score <= upper:
            return code
    return 'E'


def calculate(likelihood, severity):
    """Returns (score, category, acceptable)."""
    score = likelihood * severity
    return score, categorize(score), score <= ACCEPTABLE_MAX_RT