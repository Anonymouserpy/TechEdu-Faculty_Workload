from django.db import migrations


def backfill_year_section(apps, schema_editor):
    """
    For every Subject whose year_section is NULL, create (or reuse) the
    matching YearSection from the legacy fields and link it.

    The legacy fields (course_code, major, year_level, section) still
    exist at this point — they're dropped in a later migration, so they
    are safe to read here.
    """
    Subject = apps.get_model('workload', 'Subject')
    YearSection = apps.get_model('workload', 'YearSection')

    orphans = Subject.objects.filter(year_section__isnull=True)
    total = orphans.count()

    if total == 0:
        print("\n  No subjects need backfill — nothing to do.\n")
        return

    print(f"\n  Backfilling {total} subject(s)…\n")

    created = 0
    linked = 0
    skipped = 0

    for subj in orphans:
        course_code = (subj.course_code or '').strip()
        major = (subj.major or '').strip()
        year_level = subj.year_level
        section = (subj.section or '').strip().upper()

        if not course_code or not year_level or not section:
            print(
                f"  SKIP  subject id={subj.pk} code={subj.subject_code!r}: "
                f"missing course_code/year_level/section"
            )
            skipped += 1
            continue

        ys, was_created = YearSection.objects.get_or_create(
            course_code=course_code,
            major=major,
            year_level=year_level,
            section=section,
        )
        if was_created:
            created += 1

        subj.year_section = ys
        subj.save(update_fields=['year_section'])
        linked += 1

    print(
        f"\n  Done. Created {created} new YearSection row(s), "
        f"linked {linked} subject(s), skipped {skipped}.\n"
    )

    leftover = Subject.objects.filter(year_section__isnull=True).count()
    if leftover:
        print(
            f"\n  ⚠️  {leftover} subject(s) still have year_section=NULL. "
            f"Fix them before running the AlterField migration.\n"
        )


def reverse_backfill(apps, schema_editor):
    # Non-destructive reverse — leave the links alone.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('workload', '0008_rename_section_yearsection_alter_yearsection_options_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_year_section, reverse_backfill),
    ]