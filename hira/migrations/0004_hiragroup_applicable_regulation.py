from django.db import migrations, models


def copy_up(apps, schema_editor):
    """Move each activity's regulations onto its group: all distinct lines, in order."""
    Group = apps.get_model('hira', 'HIRAGroup')
    Activity = apps.get_model('hira', 'HIRAActivity')
    for group in Group.objects.all():
        seen, lines = set(), []
        for text in Activity.objects.filter(group=group).order_by('order', 'id') \
                .values_list('applicable_regulation', flat=True):
            for line in (text or '').splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        if lines:
            group.applicable_regulation = '\n'.join(lines)
            group.save(update_fields=['applicable_regulation'])


def copy_down(apps, schema_editor):
    """Reverse: give every activity its group's regulations again."""
    Group = apps.get_model('hira', 'HIRAGroup')
    Activity = apps.get_model('hira', 'HIRAActivity')
    for group in Group.objects.exclude(applicable_regulation=''):
        Activity.objects.filter(group=group).update(applicable_regulation=group.applicable_regulation)


class Migration(migrations.Migration):

    dependencies = [
        ('hira', '0003_remove_hiradocument_external_procedure_url_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='hiragroup',
            name='applicable_regulation',
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(copy_up, copy_down),
    ]
