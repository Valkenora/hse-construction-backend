from rest_framework import serializers

from hira.models import HIRAActivity, HIRADocument
from hira.serializers import HIRAActivitySerializer, RemarksSerializer, UserBriefSerializer
from projects.models import ProjectMember
from users.models import User
from . import mk, routing, services
from .forms import ENABLED_TYPES, FORMS
from .models import (MKApprovalRequest, Permit, PermitApproval, PermitAttachment, PermitGeoStamp,
                     PermitPhoto)

S = Permit.Status


class PermitApprovalSerializer(serializers.ModelSerializer):
    decided_by = UserBriefSerializer(read_only=True)             # null for MK
    decided_by_external = serializers.SerializerMethodField()    # {name, email} for MK, else null

    class Meta:
        model = PermitApproval
        fields = ['id', 'step', 'decision', 'decided_by', 'decided_by_external', 'remarks',
                  'submission_round', 'decided_at']

    def get_decided_by_external(self, obj):
        if obj.decided_by_id is not None:
            return None
        return {'name': obj.external_name, 'email': obj.external_email}


class MKRequestSerializer(serializers.ModelSerializer):
    """Delivery status of an MK link, for staff. Never exposes the token or the code."""
    contact_name = serializers.CharField(source='contact.full_name', read_only=True)
    contact_email = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()      # active | expired | decided | superseded
    email_sent = serializers.SerializerMethodField()
    email_error = serializers.SerializerMethodField()

    class Meta:
        model = MKApprovalRequest
        fields = ['id', 'contact_name', 'contact_email', 'submission_round', 'state', 'expires_at',
                  'email_sent', 'email_sent_at', 'email_error', 'otp_sent_count', 'created_at']
        read_only_fields = fields

    def get_contact_email(self, obj):
        return mk.mask_email(obj.contact.email)

    def get_state(self, obj):
        return mk.effective_status(obj)

    def get_email_sent(self, obj):
        return obj.email_sent_at is not None

    def get_email_error(self, obj):
        return obj.email_error or None


LOCATION_FIELDS = ('latitude', 'longitude', 'accuracy', 'captured_at')


class LocationFields(serializers.Serializer):
    """The device position as the browser reports it (navigator.geolocation). Stored as sent;
    a person in the approval chain checks it against the site."""
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    accuracy = serializers.FloatField(min_value=0)          # metres
    captured_at = serializers.DateTimeField()               # when the device took the fix


class SubmitSerializer(LocationFields):
    """POST .../submit/. The Subkontraktor's location is mandatory."""

    def validate(self, attrs):
        return {'location': {k: attrs[k] for k in LOCATION_FIELDS}}


class DecisionSerializer(RemarksSerializer):
    """POST .../approve/ and .../reject/. Location is optional here: the service requires it
    when a Supervisor or HSE Officer approves, and ignores it for everyone else."""
    latitude = serializers.FloatField(required=False, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, min_value=-180, max_value=180)
    accuracy = serializers.FloatField(required=False, min_value=0)
    captured_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        given = [k for k in LOCATION_FIELDS if k in attrs]
        if given and len(given) < len(LOCATION_FIELDS):
            raise serializers.ValidationError({k: 'Required when any location field is sent.'
                                               for k in LOCATION_FIELDS if k not in attrs})
        location = {k: attrs.pop(k) for k in given}
        return {**attrs, 'location': location or None}


class PermitPhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()   # short-lived signed link; null if storage is down
    uploaded_by = UserBriefSerializer(read_only=True)

    class Meta:
        model = PermitPhoto
        fields = ['id', 'stage', 'original_name', 'content_type', 'size', 'uploaded_by',
                  'uploaded_at', 'url']
        read_only_fields = fields

    def get_url(self, obj):
        return self.context.get('urls', {}).get(obj.storage_path)


class PermitAttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()   # short-lived signed link; null if storage is down
    uploaded_by = UserBriefSerializer(read_only=True)

    class Meta:
        model = PermitAttachment
        fields = ['id', 'kind', 'original_name', 'size', 'uploaded_by', 'uploaded_at', 'url']
        read_only_fields = fields

    def get_url(self, obj):
        return self.context.get('urls', {}).get(obj.storage_path)


def hira_layout(permit):
    """The linked HIRA, laid out document by document like the HIRA documents themselves but only
    with the selected activities (what gets printed after the permit form).
    None until at least one activity is linked."""
    activities = sorted(permit.hira_activities.all(),
                        key=lambda a: (a.group.hira_document_id, a.group.order, a.group_id, a.order, a.id))
    if not activities:
        return None
    documents, flat = [], []
    for activity in activities:
        group, document = activity.group, activity.group.hira_document
        if not documents or documents[-1]['document']['id'] != document.id:
            documents.append({
                'document': {'id': document.id, 'work_package': document.work_package,
                             'work_area': document.work_area, 'document_number': document.document_number,
                             'document_date': document.document_date, 'status': document.status,
                             'created_by_name': document.created_by.full_name},
                'groups': []})
        groups = documents[-1]['groups']
        if not groups or groups[-1]['id'] != group.id:
            entry = {'id': group.id, 'location_function': group.location_function,
                     'applicable_regulation': group.applicable_regulation, 'order': group.order,
                     'activities': []}
            groups.append(entry)
            flat.append({**entry, 'document_id': document.id, 'activities': entry['activities']})
        groups[-1]['activities'].append(HIRAActivitySerializer(activity).data)
    return {
        'documents': documents,
        # Deprecated, kept so screens built before 9 Nov keep working: the first document, and the
        # groups of every document in one flat list (each with its `document_id`).
        'document': documents[0]['document'],
        'groups': flat,
    }


class HIRAOptionSerializer(serializers.ModelSerializer):
    """One approved HIRA document of the project with its groups and activities: what a
    subcontractor ticks from when linking a permit. Any subcontractor's document, `mine` says whose."""
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    mine = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()

    class Meta:
        model = HIRADocument
        fields = ['id', 'work_package', 'work_area', 'document_number', 'document_date',
                  'created_by_name', 'mine', 'groups']
        read_only_fields = fields

    def get_mine(self, obj):
        return obj.created_by_id == self.context['request'].user.id

    def get_groups(self, obj):
        out = []
        for g in sorted(obj.groups.all(), key=lambda g: (g.order, g.id)):
            acts = sorted(g.activities.all(), key=lambda a: (a.order, a.id))
            out.append({'id': g.id, 'location_function': g.location_function,
                        'applicable_regulation': g.applicable_regulation, 'order': g.order,
                        'activities': HIRAActivitySerializer(acts, many=True).data})
        return out


class PermitGeoStampSerializer(serializers.ModelSerializer):
    recorded_by = UserBriefSerializer(read_only=True)

    class Meta:
        model = PermitGeoStamp
        fields = ['id', 'step', 'submission_round', 'latitude', 'longitude', 'accuracy',
                  'captured_at', 'recorded_by', 'recorded_at']
        read_only_fields = fields


class PermitListSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    created_by = UserBriefSerializer(read_only=True)

    class Meta:
        model = Permit
        fields = ['id', 'project', 'project_name', 'permit_type', 'document_number',
                  'work_date', 'work_location', 'work_description',
                  'status', 'risk_category', 'submission_round', 'created_by',
                  'created_at', 'updated_at']
        read_only_fields = fields


class PermitDetailSerializer(PermitListSerializer):
    # Writable: tick up to 10 activities from approved HIRA documents of this project.
    hira_activity_ids = serializers.PrimaryKeyRelatedField(
        many=True, required=False, source='hira_activities',
        queryset=HIRAActivity.objects.select_related('group__hira_document'))
    hira_document_ids = serializers.SerializerMethodField()         # read-only: the documents they come from
    hira_document = serializers.SerializerMethodField()             # DEPRECATED read-only, see get_hira_document
    hira = serializers.SerializerMethodField()                      # read-only layout for display and print
    highest_initial_category = serializers.SerializerMethodField()  # preview of the chain, before submit
    cancelled_by = UserBriefSerializer(read_only=True)
    closed_by = UserBriefSerializer(read_only=True)
    approvals = PermitApprovalSerializer(many=True, read_only=True)
    photos = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    geo_stamps = PermitGeoStampSerializer(many=True, read_only=True)
    approval_chain = serializers.SerializerMethodField()
    mk_requests = serializers.SerializerMethodField()
    form = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta(PermitListSerializer.Meta):
        fields = PermitListSerializer.Meta.fields + [
            'hira_activity_ids', 'hira_document_ids', 'hira_document', 'hira', 'highest_initial_category',
            'work_start', 'work_end', 'equipment_used', 'person_in_charge', 'worker_count',
            'safety_areas', 'checklist', 'ppe', 'ppe_other',
            'approvals', 'approval_chain', 'mk_requests', 'photos', 'attachments', 'geo_stamps',
            'cancelled_by', 'cancelled_at', 'cancel_reason',
            'post_work_evaluation', 'close_requested_at', 'closed_by', 'closed_at',
            'form', 'allowed_actions',
        ]
        read_only_fields = ['document_number', 'status', 'risk_category', 'submission_round',
                            'created_at', 'updated_at', 'cancelled_at', 'cancel_reason',
                            'post_work_evaluation', 'close_requested_at', 'closed_at']

    # --- validation (writes) ---
    def validate(self, attrs):
        inst = self.instance
        ptype = attrs.get('permit_type', inst.permit_type if inst else None)
        project = attrs.get('project', inst.project if inst else None)

        if inst is None:
            if not project.is_active:
                raise serializers.ValidationError({'project': 'This project is not active.'})
            user = self.context['request'].user
            if not ProjectMember.objects.filter(project=project, user=user).exists():
                raise serializers.ValidationError({'project': 'You are not a member of this project.'})
        else:
            if project != inst.project:
                raise serializers.ValidationError({'project': 'Project cannot be changed after creation.'})
            if ptype != inst.permit_type:
                raise serializers.ValidationError({'permit_type': 'Permit type cannot be changed.'})
        if ptype not in ENABLED_TYPES:
            raise serializers.ValidationError({'permit_type': 'This permit type is not available yet.'})

        # The HIRA rules run when the choice is being made (create, or a PATCH that touches it).
        # An edit that leaves the HIRA alone doesn't re-check it: submit and approve do.
        if 'hira_activities' in attrs:
            attrs['hira_activities'] = list(dict.fromkeys(attrs['hira_activities']))   # drop duplicates
            problems = services.validate_hira_selection(project.id, attrs['hira_activities'])
            if problems:
                raise serializers.ValidationError(problems)

        form = FORMS[ptype]
        errors = {}
        if 'safety_areas' in attrs:
            v = attrs['safety_areas']
            if not isinstance(v, list) or any(a not in form['safety_areas'] for a in v):
                errors['safety_areas'] = f'List of: {", ".join(form["safety_areas"])}.'
            else:
                attrs['safety_areas'] = sorted(set(v))
        if 'checklist' in attrs:
            v = attrs['checklist']
            if (not isinstance(v, dict) or any(k not in form['checklist'] for k in v)
                    or any(not isinstance(x, bool) for x in v.values())):
                errors['checklist'] = 'Object of item number -> true/false, items 1-%d.' % len(form['checklist'])
        if 'ppe' in attrs:
            v = attrs['ppe']
            if not isinstance(v, list) or any(p not in form['ppe'] for p in v):
                errors['ppe'] = f'List of: {", ".join(form["ppe"])}.'
        if 'ppe_other' in attrs:
            v = attrs['ppe_other']
            if (not isinstance(v, list) or len(v) > form['ppe_other_max']
                    or any(not isinstance(x, str) or not x.strip() for x in v)):
                errors['ppe_other'] = f'At most {form["ppe_other_max"]} non-empty text entries.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    # --- computed ---
    def get_form(self, obj):
        f = FORMS[obj.permit_type]
        return {'code': f['code'], 'revision': f['revision']}

    def get_photos(self, obj):
        photos = list(obj.photos.all())
        urls = services.signed_urls([p.storage_path for p in photos]) if photos else {}
        return PermitPhotoSerializer(photos, many=True, context={'urls': urls}).data

    def get_mk_requests(self, obj):
        """Links for the current round only, newest first."""
        rows = [r for r in obj.mk_requests.all() if r.submission_round == obj.submission_round]
        return MKRequestSerializer(rows, many=True).data

    def get_hira(self, obj):
        return hira_layout(obj)

    def get_hira_document_ids(self, obj):
        return sorted({a.group.hira_document_id for a in obj.hira_activities.all()})

    def get_hira_document(self, obj):
        """Deprecated, for screens built before 9 Nov: the document id when every ticked activity comes
        from one document, else null. Ignored on input. Use hira_document_ids."""
        ids = self.get_hira_document_ids(obj)
        return ids[0] if len(ids) == 1 else None

    def get_highest_initial_category(self, obj):
        activities = list(obj.hira_activities.all())
        return routing.highest(a.initial_category for a in activities) if activities else None

    def get_attachments(self, obj):
        rows = list(obj.attachments.all())
        urls = services.signed_urls([a.storage_path for a in rows]) if rows else {}
        return PermitAttachmentSerializer(rows, many=True, context={'urls': urls}).data

    def get_approval_chain(self, obj):
        if not obj.risk_category:
            return []
        done = {a.step: a for a in obj.approvals.all() if a.submission_round == obj.submission_round}
        out = []
        for step in routing.steps_for(obj.risk_category):
            a = done.get(step)
            if a:
                state = a.decision
            elif obj.status == routing.STEP_STATUS[step]:
                state = 'pending'
            else:
                state = 'waiting'
            out.append({'step': step, 'state': state})
        return out

    def get_allowed_actions(self, obj):
        u = self.context['request'].user
        own_editable = u.id == obj.created_by_id and obj.status in services.EDITABLE_STATUSES
        return {
            'can_edit': own_editable,
            'can_submit': own_editable,
            'can_decide': services.can_decide(obj, u),
            'can_cancel': u.role == User.Role.HSE_COORDINATOR and obj.status not in services.FINAL_STATUSES,
            'can_request_close': u.id == obj.created_by_id and obj.status == S.APPROVED,
            'can_confirm_close': u.role == User.Role.HSE_OFFICER and obj.status == S.PENDING_CLOSE,
            'can_upload_before_photos': own_editable,
            'can_manage_attachments': own_editable,
            'can_upload_after_photos': u.id == obj.created_by_id and obj.status == S.APPROVED,
            # the frontend must send latitude/longitude/accuracy/captured_at with these actions
            'decide_requires_location': (services.can_decide(obj, u)
                                         and u.role in services.GEO_STEPS),
            'can_resend_mk_link': (u.role in (User.Role.HSE_OFFICER, User.Role.HSE_COORDINATOR)
                                   and obj.status == S.PENDING_MK),
            'can_set_number': u.role in (User.Role.HSE_OFFICER, User.Role.HSE_COORDINATOR)
                              and obj.status != S.CANCELLED,
        }


class CloseRequestSerializer(serializers.Serializer):
    post_work_evaluation = serializers.CharField()