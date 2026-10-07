"""Single source of truth for HIRA risk scoring."""

SCORE_MIN, SCORE_MAX = 1, 5

# Initial-risk category by Rt = Likelihood x Severity (upper bounds, inclusive).
# PROVISIONAL: derived from the matrix colours on form F.01/P.01. Not confirmed by client.
CATEGORY_UPPER_BOUNDS = (('R', 4), ('M', 9), ('T', 16))  # anything higher -> 'E'

# Acceptable if Rt <= 4, urgent action if Rt > 10.
# Source: legend on the company's filled sample (HIRA Penyimpanan Bekisting).
# Matches the PRD's "residual <= 4". The blank template (Revisi Ke 3) says <= 6;
# client to confirm which one is current.
ACCEPTABLE_MAX_RT = 4
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