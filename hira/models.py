from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from projects.models import Project
from .risk import SCORE_MAX, SCORE_MIN, calculate

User = settings.AUTH_USER_MODEL

score_validators = [MinValueValidator(SCORE_MIN), MaxValueValidator(SCORE_MAX)]


class HIRADocument(models.Model):

    class Status(models.TextChoices):
        DRAFT           = 'draft',           'Draft'
        PENDING_OFFICER = 'pending_officer', 'Menunggu HSE Officer'
        PENDING_PM      = 'pending_pm',      'Menunggu Project Manager'
        APPROVED        = 'approved',        'Disetujui'
        REJECTED        = 'rejected',        'Ditolak'
        CANCELLED       = 'cancelled',       'Dibatalkan'

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='hira_documents')
    title = models.CharField(max_length=255)
    # Entered manually by HSE Officer / Coordinator; the client's office code, not unique.
    document_number = models.CharField(max_length=100, blank=True)
    document_date = models.DateField(default=timezone.localdate)
    external_procedure_url = models.URLField(blank=True)  # PRD: link to external SOP

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    # Increments on every (re)submission so approvals can be tied to a round.
    submission_round = models.PositiveSmallIntegerField(default=0)

    # Pemrakarsa = the Subkontraktor who prepared it
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='hira_created')

    # HSE Coordinator can invalidate a HIRA at any point
    cancelled_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='hira_cancelled')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'HIRA Document'
        verbose_name_plural = 'HIRA Documents'

    def __str__(self):
        return f'{self.document_number or "(no number)"} — {self.title}'


class HIRAGroup(models.Model):
    """Form column 'Lokasi/Proses/Fungsi'."""
    hira_document = models.ForeignKey(HIRADocument, on_delete=models.CASCADE, related_name='groups')
    location_function = models.CharField(max_length=255)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'HIRA Group'
        verbose_name_plural = 'HIRA Groups'

    def __str__(self):
        return f'{self.hira_document} — {self.location_function}'

    def get_audit_project(self):
        return self.hira_document.project


class HIRAActivity(models.Model):

    class ActivityType(models.TextChoices):
        ROUTINE     = 'R',  'Rutin'
        NON_ROUTINE = 'NR', 'Non Rutin'
        EMERGENCY   = 'E',  'Emergency'

    class RiskCategory(models.TextChoices):
        R = 'R', 'Rendah'
        M = 'M', 'Menengah'
        T = 'T', 'Tinggi'
        E = 'E', 'Ekstrem'

    group = models.ForeignKey(HIRAGroup, on_delete=models.CASCADE, related_name='activities')
    order = models.PositiveSmallIntegerField(default=0)  # the 'No.' column

    # Form columns
    activity_name         = models.CharField(max_length=255)                 # Aktivitas
    activity_type         = models.CharField(max_length=2, choices=ActivityType.choices)  # N/NR
    potential_hazard      = models.TextField()                               # Potensi Bahaya/Aspek Lingkungan
    risk_impact           = models.TextField()                               # Resiko/Dampak Lingkungan
    applicable_regulation = models.TextField(blank=True)                     # Regulasi yang Berlaku
    existing_controls     = models.TextField(blank=True)                     # Pengendalian Awal
    opportunity           = models.TextField(blank=True)                     # Peluang

    # Initial risk (Resiko Awal). Severity = 'R' on the form, Likelihood = 'L'.
    initial_severity   = models.PositiveSmallIntegerField(validators=score_validators)
    initial_likelihood = models.PositiveSmallIntegerField(validators=score_validators)
    initial_score      = models.PositiveSmallIntegerField(editable=False)
    initial_category   = models.CharField(max_length=1, choices=RiskCategory.choices, editable=False)
    initial_acceptable = models.BooleanField(editable=False)

    # Residual risk (Sisa Risiko)
    residual_severity   = models.PositiveSmallIntegerField(validators=score_validators)
    residual_likelihood = models.PositiveSmallIntegerField(validators=score_validators)
    residual_score      = models.PositiveSmallIntegerField(editable=False)
    residual_category   = models.CharField(max_length=1, choices=RiskCategory.choices, editable=False)
    residual_acceptable = models.BooleanField(editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    COMPUTED = (
        'initial_score', 'initial_category', 'initial_acceptable',
        'residual_score', 'residual_category', 'residual_acceptable',
    )

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'HIRA Activity'
        verbose_name_plural = 'HIRA Activities'
        constraints = [
            models.CheckConstraint(
                condition=Q(initial_severity__range=(SCORE_MIN, SCORE_MAX))
                & Q(initial_likelihood__range=(SCORE_MIN, SCORE_MAX))
                & Q(residual_severity__range=(SCORE_MIN, SCORE_MAX))
                & Q(residual_likelihood__range=(SCORE_MIN, SCORE_MAX)),
                name='hira_activity_scores_1_to_5',
            ),
        ]

    def save(self, *args, **kwargs):
        # Server-side calculation: the client never supplies scores (PRD).
        self.initial_score, self.initial_category, self.initial_acceptable = calculate(
            self.initial_likelihood, self.initial_severity)
        self.residual_score, self.residual_category, self.residual_acceptable = calculate(
            self.residual_likelihood, self.residual_severity)
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | set(self.COMPUTED)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.activity_name} (awal {self.initial_category}, sisa Rt={self.residual_score})'

    def get_audit_project(self):
        return self.group.hira_document.project


class HIRAApproval(models.Model):
    """Append-only record of each Officer / PM decision."""

    class Step(models.TextChoices):
        OFFICER = 'hse_officer',     'HSE Officer'
        PM      = 'project_manager', 'Project Manager'

    class Decision(models.TextChoices):
        APPROVED = 'approved', 'Disetujui'
        REJECTED = 'rejected', 'Ditolak'

    # PROTECT: a HIRA with decisions on record can't be deleted.
    hira_document    = models.ForeignKey(HIRADocument, on_delete=models.PROTECT, related_name='approvals')
    step             = models.CharField(max_length=20, choices=Step.choices)
    decision         = models.CharField(max_length=10, choices=Decision.choices)
    decided_by       = models.ForeignKey(User, on_delete=models.PROTECT, related_name='hira_decisions')
    remarks          = models.TextField(blank=True)
    submission_round = models.PositiveSmallIntegerField()
    decided_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['decided_at']
        constraints = [
            # A rejection must always carry a reason.
            models.CheckConstraint(
                condition=Q(decision='approved') | ~Q(remarks=''),
                name='hira_rejection_requires_remarks',
            ),
        ]

    def __str__(self):
        return f'{self.hira_document_id} {self.step} {self.decision} by {self.decided_by}'

    def get_audit_project(self):
        return self.hira_document.project