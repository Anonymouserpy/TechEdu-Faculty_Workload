from django.db import migrations

def forwards(apps, schema_editor):
    # Use 'workload' as the app label
    SubjectOffering = apps.get_model('workload', 'SubjectOffering')
    Subject = apps.get_model('workload', 'Subject')
    ClassSchedule = apps.get_model('workload', 'ClassSchedule')
    FacultyOtherAssignment = apps.get_model('workload', 'FacultyOtherAssignment')
    Semester = apps.get_model('workload', 'Semester')

    # ── 1) Propagate per-offering faculty overrides into Subject.faculty ──
    overrides = (
        SubjectOffering.objects
        .filter(faculty__isnull=False)
        .values('subject_id', 'faculty_id')
    )
    for row in overrides.iterator():
        Subject.objects.filter(pk=row['subject_id']).update(faculty_id=row['faculty_id'])

    # ── 2) Orphan schedules (offering=None) — remove, but log the count ──
    orphan_qs = ClassSchedule.objects.filter(offering__isnull=True)
    orphan_count = orphan_qs.count()
    if orphan_count:
        print(f"[migration] deleting {orphan_count} orphan ClassSchedule rows")
        orphan_qs.delete()

    # ── 3) Backfill FacultyOtherAssignment.semester ──
    null_count = FacultyOtherAssignment.objects.filter(semester__isnull=True).count()
    if null_count:
        current = (
            Semester.objects.filter(is_active=True)
            .order_by('semester').first()
        )
        if current is None:
            current = Semester.objects.order_by(
                '-academic_year__year_label', 'semester'
            ).first()
        
        if current is None:
            raise RuntimeError(
                f"[migration] {null_count} FacultyOtherAssignment rows have "
                f"semester=NULL, but no Semester exists to backfill from. "
                f"Create an AcademicYear + Semester first, or delete those rows."
            )
        
        FacultyOtherAssignment.objects.filter(semester__isnull=True).update(
            semester=current
        )
        print(f"[migration] backfilled {null_count} FacultyOtherAssignment rows to semester={current!r}")

def backwards(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('workload', '0022_academicyear_semester_subjectoffering_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]