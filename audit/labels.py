"""Display text for audit rows. Everything a reader sees is defined here, so renaming a term
(for example the HIRA wording) is a one-place change and never touches the API keys."""
import re

ACTION_LABELS = {
    'created': 'Dibuat',
    'updated': 'Diubah',
    'deleted': 'Dihapus',
    'submitted': 'Diajukan',
    'approved': 'Disetujui',
    'rejected': 'Ditolak',
    'cancelled': 'Dibatalkan',
    'close_requested': 'Penutupan diajukan',
    'closed': 'Ditutup',
    'photo_added': 'Foto ditambahkan',
    'photo_removed': 'Foto dihapus',
    'attachment_added': 'Lampiran ditambahkan',
    'attachment_removed': 'Lampiran dihapus',
    'mk_link_issued': 'Tautan persetujuan dikirim ke MK',
    'mk_otp_sent': 'Kode OTP dikirim ke MK',
    'mk_otp_failed': 'Kode OTP MK gagal dikirim',
}

RESOURCE_LABELS = {
    'permits.permit': 'Izin Kerja',
    'hira.hiradocument': 'HIRA',
    'hira.hiragroup': 'Grup HIRA',
    'hira.hiraactivity': 'Aktivitas HIRA',
    'projects.project': 'Proyek',
}

STEP_LABELS = {
    'supervisor': 'Supervisor',
    'hse_officer': 'HSE Officer',
    'hse_coordinator': 'HSE Coordinator',
    'project_manager': 'Project Manager',
    'mk': 'MK',
}

MK_OTP_ACTIONS = ('mk_otp_sent', 'mk_otp_failed')

_STEP = re.compile(r'^\[(\w+)\]\s*')
_LOCATION = re.compile(r'\s*location=\S+ accuracy=\S+ captured_at=\S+')
_RISK = re.compile(r'risk=(\w)')


def action_label(action):
    return ACTION_LABELS.get(action, action.replace('_', ' ').capitalize())


def resource_label(resource_type):
    return RESOURCE_LABELS.get(resource_type, resource_type)


def resource_title(resource_type, snapshot, resource_id):
    """One line that names the record, taken from the state it had at that moment."""
    s = snapshot or {}
    if resource_type == 'permits.permit':
        number = s.get('document_number') or f'#{resource_id}'
        desc = s.get('work_description')
        return f'{number} · {desc}' if desc else number
    if resource_type == 'hira.hiradocument':
        number = s.get('document_number') or f'#{resource_id}'
        pkg = s.get('work_package')
        return f'{number} · {pkg}' if pkg else number
    if resource_type == 'hira.hiragroup':
        return s.get('location_function') or f'#{resource_id}'
    if resource_type == 'hira.hiraactivity':
        return s.get('activity_name') or f'#{resource_id}'
    if resource_type == 'projects.project':
        return s.get('name') or f'#{resource_id}'
    return f'#{resource_id}'


def clean_remarks(action, remarks):
    """Remarks as a reader should see them: no step tag, no coordinates, no file paths, no emails."""
    text = _LOCATION.sub('', remarks or '').strip()
    text = _STEP.sub('', text)
    if action in ('mk_link_issued', 'mk_otp_sent', 'mk_otp_failed'):
        return ''                                    # these carry email addresses or error details
    if action in ('photo_removed', 'attachment_removed'):
        return text.split(':', 1)[0].strip()         # keep "before" / "medical", drop the storage path
    if action == 'submitted':
        return ''                                    # the summary says it better
    return text.replace('document_number:', 'Nomor dokumen:', 1)


def summary(action, remarks, snapshot):
    """A short readable line for the list, for example "Supervisor: Terlalu luas"."""
    s = snapshot or {}
    text = clean_remarks(action, remarks)
    if action == 'submitted':
        parts = []
        if s.get('submission_round'):
            parts.append(f'Putaran {s["submission_round"]}')
        risk = _RISK.search(remarks or '')
        if risk:
            parts.append(f'kategori {risk.group(1)}')
        return ', '.join(parts)
    if action == 'mk_link_issued':
        return f'Putaran {s["submission_round"]}' if s.get('submission_round') else ''
    if action in ('approved', 'rejected'):
        m = _STEP.match(_LOCATION.sub('', remarks or '').strip())
        step = STEP_LABELS.get(m.group(1), m.group(1).replace('_', ' ').title()) if m else ''
        return f'{step}: {text}' if step and text else (step or text)
    return text


def actor_name(user, actor_label):
    """Who acted, as a short name: the user's name, "Budi (MK)" for the external MK approver, or "Sistem"."""
    if user is not None:
        return user.full_name
    if (actor_label or '').startswith('MK:'):        # "MK: Budi <mk@x> (external, ...)"
        return actor_label[3:].split('<')[0].strip() + ' (MK)'
    return 'Sistem'
