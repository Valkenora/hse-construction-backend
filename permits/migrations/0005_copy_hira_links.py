from django.db import migrations


def copy_up(apps, schema_editor):
    """Every permit keeps its single HIRA activity, now as one link, and remembers its document."""
    Permit = apps.get_model('permits', 'Permit')
    Link = apps.get_model('permits', 'PermitHIRAActivity')
    for permit in Permit.objects.exclude(hira_activity=None).select_related('hira_activity__group'):
        Permit.objects.filter(pk=permit.pk).update(hira_document_id=permit.hira_activity.group.hira_document_id)
        Link.objects.get_or_create(permit=permit, hira_activity_id=permit.hira_activity_id)


def copy_down(apps, schema_editor):
    """Going back: the old single column gets the permit's first linked activity."""
    Permit = apps.get_model('permits', 'Permit')
    Link = apps.get_model('permits', 'PermitHIRAActivity')
    for permit in Permit.objects.all():
        first = Link.objects.filter(permit=permit).order_by('pk').first()
        if first is not None:
            Permit.objects.filter(pk=permit.pk).update(hira_activity_id=first.hira_activity_id)


class Migration(migrations.Migration):

    dependencies = [
        ('permits', '0004_multi_hira_and_attachments'),
    ]

    operations = [
        migrations.RunPython(copy_up, copy_down),
    ]
