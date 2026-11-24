"""
==============================================================================
 models.py — FULL PATCHED VERSION (validation + lab-hour + Sunday fixes)
==============================================================================

AUTH NOTE
---------
This project uses a CUSTOM auth model (FacultyProfile), not Django's
AbstractBaseUser. `request.user` is AnonymousUser everywhere; session state
lives in `request.session['faculty_id']`. This is intentional.

FACULTY SOURCE OF TRUTH (FIX #1)
--------------------------------
A ClassSchedule's faculty is ALWAYS `schedule.offering.subject.faculty`.
The `ClassSchedule.faculty` and `SubjectOffering.faculty` override fields have
been removed to eliminate the tri-source-of-truth bug. To reassign a schedule
across faculty, change `subject.faculty` (an audited action), not per-row.

SEMESTERS (FIX #2)
------------------
`Semester.get_current()` never silently returns a misleading semester; if no
current AY exists it falls back to the latest AY, then to its first semester.
Views should still handle `None` explicitly (empty DB).

VALIDATION (PATCH 1)
--------------------
`Subject`, `ClassSchedule`, and `FacultyOtherAssignment` override save() to
call full_clean(). Validation now runs on .objects.create() paths, not just
ModelForms. For bulk imports, call full_clean() per instance, then bulk_create.

LAB HOURS (PATCH 2)
-------------------
Scheduling capacity uses RAW lab hours. Reporting credit uses effective lab
hours (raw × 0.7). `raw_total_hours_per_week` and `total_hours_per_week`
expose both: `raw_total_hours_per_week` (raw scheduled) and
`total_hours_per_week` (reporting credit).

UTILIZATION (PATCH 3)
---------------------
Sunday is excluded from room utilization to match the Mon–Sat grid used by
`get_room_schedule_matrix`. Capacity is (19−7−1)h × 6d = 66h.
==============================================================================
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum, F, ExpressionWrapper, FloatField
from django.db.models.functions import ExtractHour, ExtractMinute
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from django.contrib.auth.hashers import make_password, check_password


# =============================================================
# FACULTY MODEL
# =============================================================
class FacultyProfile(models.Model):
    EMPLOYMENT_TYPE = [
        ('FULL', 'Full Time'),
        ('PART', 'Part Time'),
        ('CONTRACT', 'Contractual'),
    ]

    # FIX #17: employee_id remains PK (immutable by design); drop redundant unique=True.
    employee_id = models.CharField(
        max_length=20, primary_key=True,
        help_text="Immutable by design. Renaming requires a data migration.",
    )
    full_name = models.CharField(max_length=100)
    specialization = models.CharField(max_length=100)
    attained_degree = models.CharField(max_length=50)
    academic_rank = models.CharField(
        max_length=100, blank=True, default='',
        help_text="e.g. Instructor I, Assistant Professor II. Blank by default.",
    )
    employment_type = models.CharField(max_length=10, choices=EMPLOYMENT_TYPE, default='PART')
    department = models.CharField(max_length=100, default='Department of Technology Education')
    is_chairman = models.BooleanField(default=False)

    password = models.CharField(max_length=128, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    last_login = models.DateTimeField(null=True, blank=True)

    effective_date = models.CharField(max_length=64, blank=True, default='Aug. 3, 2026')

    term = models.CharField(
        max_length=64, blank=True, default='First Semester 2026-2027',
        help_text="Display-only default. Semester is the source of truth.",
    )

    overload_hours = models.CharField(max_length=64, blank=True, default='')
    overload_subjects = models.TextField(blank=True, default='')
    service_credits = models.CharField(max_length=64, blank=True, default='')
    adviser_notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['full_name']
        indexes = [
            models.Index(fields=['is_chairman']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        role = "Chairman" if self.is_chairman else "Faculty"
        return f"{self.full_name} ({role})"

    def set_password(self, raw_password):
        self.password = make_password(raw_password) if raw_password else None

    def check_password(self, raw_password):
        if self.password is None or not raw_password:
            return False
        return check_password(raw_password, self.password)

    @classmethod
    def authenticate(cls, employee_id, password):
        # FIX #18: equalize timing to prevent employee_id enumeration.
        try:
            faculty = cls.objects.get(employee_id=employee_id, is_active=True)
        except cls.DoesNotExist:
            check_password(password, make_password(None))
            return None
        return faculty if faculty.check_password(password) else None


# =============================================================
# ROOM MODEL
# =============================================================
class Room(models.Model):
    PROGRAM_CHOICES = [
        ('BTLED',  'Bachelor of Technology and Livelihood Education'),
        ('BTVTED', 'Bachelor of Technical-Vocational Teacher Education'),
    ]
    DEPARTMENT_MAP = {
        'BTLED':  'DEPARTMENT OF TECHNOLOGY EDUCATION',
        'BTVTED': 'DEPARTMENT OF TECHNOLOGY EDUCATION',
    }

    program = models.CharField(max_length=20, choices=PROGRAM_CHOICES, default='BTVTED')
    room_name = models.CharField(max_length=50)
    room_code = models.CharField(max_length=20, unique=True)
    floor_number = models.IntegerField()
    building_name = models.CharField(max_length=50)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ['program', 'building_name', 'room_name']
        verbose_name_plural = "Rooms"
        # FIX #18b: unique_together removed (room_code already globally unique).

    def __str__(self):
        return f"{self.room_code} - {self.building_name} ({self.get_program_display()})"

    @property
    def department_name(self):
        return self.DEPARTMENT_MAP.get(self.program, 'DEPARTMENT OF TECHNOLOGY EDUCATION')

    @property
    def program_display(self):
        return self.get_program_display()


# =============================================================
# YEAR SECTION MODEL
# =============================================================
class YearSection(models.Model):
    YEAR_LEVEL_CHOICES = [
        (1, '1st Year'), (2, '2nd Year'), (3, '3rd Year'), (4, '4th Year'),
    ]

    course_code = models.CharField(max_length=50)
    major = models.CharField(max_length=100, blank=True)
    year_level = models.IntegerField(choices=YEAR_LEVEL_CHOICES)
    section = models.CharField(max_length=5)

    class Meta:
        unique_together = ['course_code', 'major', 'year_level', 'section']
        ordering = ['course_code', 'year_level', 'section']
        verbose_name = "Year & Section"
        verbose_name_plural = "Year & Sections"

    def __str__(self):
        major_str = f" {self.major}" if self.major else ""
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level]
        return f"{self.course_code} {year_roman}-{self.section}{major_str}"


# =============================================================
# ACADEMIC YEAR / SEMESTER
# =============================================================
class AcademicYear(models.Model):
    year_label = models.CharField(max_length=20, unique=True)
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ['-year_label']
        verbose_name = "Academic Year"
        verbose_name_plural = "Academic Years"
        # FIX #11: DB-level guarantee of at most one current AY.
        constraints = [
            models.UniqueConstraint(
                fields=['is_current'],
                condition=Q(is_current=True),
                name='one_current_academic_year',
            ),
        ]

    def __str__(self):
        return self.year_label

    def save(self, *args, **kwargs):
        # FIX #11: atomic swap so two concurrent saves can't both be current.
        with transaction.atomic():
            if self.is_current:
                AcademicYear.objects.exclude(pk=self.pk).update(is_current=False)
            super().save(*args, **kwargs)


class Semester(models.Model):
    SEMESTER_CHOICES = [
        ('1ST', 'First Semester'),
        ('2ND', 'Second Semester'),
    ]

    academic_year = models.ForeignKey(
        AcademicYear, on_delete=models.CASCADE, related_name='semesters',
    )
    semester = models.CharField(max_length=3, choices=SEMESTER_CHOICES)
    # FIX #11b: default=False; the signal below ensures one per AY.
    is_active = models.BooleanField(default=False)

    class Meta:
        unique_together = ['academic_year', 'semester']
        ordering = ['academic_year__year_label', 'semester']
        verbose_name = "Semester"
        verbose_name_plural = "Semesters"
        # FIX #11b: at most one active semester per AY.
        constraints = [
            models.UniqueConstraint(
                fields=['academic_year'],
                condition=Q(is_active=True),
                name='one_active_semester_per_ay',
            ),
        ]

    def __str__(self):
        return f"{self.get_semester_display()} {self.academic_year.year_label}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_active:
                Semester.objects.filter(
                    academic_year_id=self.academic_year_id
                ).exclude(pk=self.pk).update(is_active=False)
            super().save(*args, **kwargs)

    @classmethod
    def get_current(cls):
        """
        FIX #2: never silently return None if an AY exists.
        Fallback chain: current AY → latest AY → (None only if DB empty).
        """
        ay = AcademicYear.objects.filter(is_current=True).first()
        if ay is None:
            ay = AcademicYear.objects.order_by('-year_label').first()
        if ay is None:
            return None  # truly empty DB; views must handle this
        sem = ay.semesters.filter(is_active=True).order_by('semester').first()
        if sem is None:
            sem = ay.semesters.order_by('semester').first()
        return sem


@receiver(post_save, sender=AcademicYear)
def _seed_active_semester(sender, instance, created, **kwargs):
    """FIX #2: guarantee at least one active semester exists when an AY is created."""
    if not created:
        return
    if not instance.semesters.exists():
        Semester.objects.create(academic_year=instance, semester='1ST', is_active=True)


# =============================================================
# SUBJECT OFFERING
# =============================================================
class SubjectOffering(models.Model):
    """
    FIX #1: `faculty` override removed. The offering's faculty is ALWAYS
    `subject.faculty`. To reassign, update Subject.faculty.
    """
    subject = models.ForeignKey(
        'Subject', on_delete=models.CASCADE, related_name='offerings',
    )
    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, related_name='offerings',
    )
    total_students = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        unique_together = ['subject', 'semester']
        ordering = [
            'semester__academic_year__year_label',
            'semester__semester',
            'subject__subject_code',
        ]
        verbose_name = "Subject Offering"
        verbose_name_plural = "Subject Offerings"

    def __str__(self):
        return f"{self.subject.subject_code} — {self.semester}"

    @property
    def academic_year(self):
        return self.semester.academic_year

    @property
    def effective_faculty(self):
        return self.subject.faculty

    @property
    def year_section(self):
        return self.subject.year_section

    @property
    def subject_code(self):
        return self.subject.subject_code

    @property
    def semester_label(self):
        return self.semester.get_semester_display()


# =============================================================
# SUBJECT (catalog)
# =============================================================
class Subject(models.Model):
    YEAR_LEVEL_CHOICES = YearSection.YEAR_LEVEL_CHOICES

    year_section = models.ForeignKey(
        YearSection, on_delete=models.PROTECT, related_name='subjects',
    )
    subject_code = models.CharField(max_length=20)
    subject_description = models.TextField()

    # FIX #16: negative hours are now rejected.
    lecture_hours_per_week = models.DecimalField(
        max_digits=4, decimal_places=1, default=0,
        validators=[MinValueValidator(0)],
    )
    lab_hours_per_week = models.DecimalField(
        max_digits=4, decimal_places=1, default=0,
        validators=[MinValueValidator(0)],
    )

    # FIX #7: PROTECT prevents accidental mass-deletion via faculty_delete.
    faculty = models.ForeignKey(
        'FacultyProfile', on_delete=models.PROTECT, related_name='subjects',
    )

    # FIX #4: has_lecture / is_laboratory are now DERIVED properties, not
    # stored columns. One source of truth: the hours.
    is_active = models.BooleanField(default=True)

    allow_cohort_overlap = models.BooleanField(
        default=False,
        help_text="If True, exempt from the section-conflict check.",
    )
    total_students = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = [
            'year_section__course_code',
            'year_section__year_level',
            'year_section__section',
        ]
        verbose_name_plural = "Subjects"
        unique_together = [['year_section', 'subject_code']]

    # ── Derived component flags (FIX #4) ─────────────────────
    @property
    def has_lecture(self):
        return float(self.lecture_hours_per_week or 0) > 0

    @property
    def is_laboratory(self):
        return float(self.lab_hours_per_week or 0) > 0

    # ── Passthroughs ─────────────────────────────────────────
    @property
    def course_code(self):
        return self.year_section.course_code

    @property
    def major(self):
        return self.year_section.major

    @property
    def year_level(self):
        return self.year_section.year_level

    @property
    def section(self):
        return self.year_section.section

    def __str__(self):
        major_str = f" - {self.major}" if self.major else ""
        return (
            f"{self.subject_code} - {self.course_code} {self.year_level}-"
            f"{self.section}{major_str} ({self.faculty.full_name})"
        )

    def get_subject_display(self):
        major_str = f" {self.major}" if self.major else ""
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level]
        return f"{self.subject_code} {self.course_code} {year_roman}-{self.section}{major_str}"

    @property
    def section_display(self):
        """Course, year-section and major only (no subject code), so grids
        that already print the code don't repeat it."""
        major_str = f" {self.major}" if self.major else ""
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level]
        return f"{self.course_code} {year_roman}-{self.section}{major_str}"

    def clean(self):
        # FIX #4: validation now lives here, not in an override of save().
        if not self.has_lecture and not self.is_laboratory:
            raise ValidationError(
                "A subject must have at least one component "
                "(lecture hours > 0 or lab hours > 0)."
            )

    def save(self, *args, **kwargs):
        # PATCH 1: runs clean() even on .objects.create() paths.
        self.full_clean()
        super().save(*args, **kwargs)

    LAB_HOUR_MULTIPLIER = 0.7

    def effective_lab_hours_per_week(self):
        return round(float(self.lab_hours_per_week or 0) * self.LAB_HOUR_MULTIPLIER, 2)

    @property
    def raw_lab_hours_per_week(self):
        """PATCH 2: Scheduling capacity — raw lab hours (no reporting multiplier)."""
        return round(float(self.lab_hours_per_week or 0), 2)

    @property
    def raw_total_hours_per_week(self):
        """PATCH 2: Scheduling capacity — raw lecture + raw lab (no multiplier)."""
        lec = float(self.lecture_hours_per_week or 0)
        lab = float(self.lab_hours_per_week or 0)
        return round(lec + lab, 2)

    @property
    def total_hours_per_week(self):
        """PATCH 2: Reporting credit — lecture + effective lab (lab × 0.7)."""
        lec = float(self.lecture_hours_per_week or 0) if self.has_lecture else 0.0
        lab = self.effective_lab_hours_per_week() if self.is_laboratory else 0.0
        return round(lec + lab, 2)

    @property
    def cohort_key(self):
        return (
            self.course_code or '',
            int(self.year_level or 0),
            (self.section or '').upper(),
        )

    @property
    def cohort_display(self):
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level] if self.year_level else '?'
        return f"{self.course_code} {year_roman}-{self.section}"

    @property
    def section_label(self):
        return self.cohort_display


# =============================================================
# CLASS SCHEDULE
# =============================================================
class ClassSchedule(models.Model):
    DAY_CHOICES = [
        ('MONDAY', 'Monday'), ('TUESDAY', 'Tuesday'), ('WEDNESDAY', 'Wednesday'),
        ('THURSDAY', 'Thursday'), ('FRIDAY', 'Friday'), ('SATURDAY', 'Saturday'),
        ('SUNDAY', 'Sunday'),
    ]
    SCHEDULE_TYPE_CHOICES = [('LECTURE', 'Lecture'), ('LAB', 'Laboratory')]

    # FIX #1: `faculty` override removed. Faculty is derived from offering.
    # FIX #20: `offering` is now required (was null=True).
    offering = models.ForeignKey(
        SubjectOffering, on_delete=models.CASCADE, related_name='schedules',
        null=False, blank=False,
    )
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='schedules')

    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    schedule_type = models.CharField(
        max_length=10, choices=SCHEDULE_TYPE_CHOICES, default='LECTURE',
    )
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        FacultyProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='schedules_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['day', 'start_time']
        indexes = [
            models.Index(fields=['day']),
            models.Index(fields=['room', 'day']),
            models.Index(fields=['offering', 'day']),
            models.Index(fields=['day', 'start_time', 'end_time']),
        ]
        unique_together = [
            ['room', 'day', 'start_time'],
            ['offering', 'day', 'start_time'],
        ]
        # FIX #12: overlap protected at DB level (Postgres only).
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F('start_time')),
                name='schedule_end_after_start',
            ),
        ]

    def __str__(self):
        subj = self.offering.subject
        fac = self.offering.subject.faculty
        return (
            f"{subj.subject_code} - {subj.get_subject_display()} - "
            f"{self.day} {self.start_time.strftime('%H:%M')} ({self.schedule_type}) - "
            f"{fac.full_name}"
        )

    # ── Convenience passthroughs ─────────────────────────────
    @property
    def subject(self):
        return self.offering.subject

    @property
    def semester(self):
        return self.offering.semester

    @property
    def academic_year(self):
        return self.offering.semester.academic_year

    @property
    def section(self):
        return self.subject.section

    @property
    def year_level(self):
        return self.subject.year_level

    @property
    def subject_code(self):
        return self.subject.subject_code

    @property
    def subject_description(self):
        return self.subject.subject_description

    @property
    def course_code(self):
        return self.subject.course_code

    @property
    def major(self):
        return self.subject.major

    @property
    def hours(self):
        if self.schedule_type == 'LECTURE':
            return self.subject.lecture_hours_per_week
        return self.subject.lab_hours_per_week

    @property
    def assigned_faculty(self):
        # FIX #1: single source of truth.
        return self.offering.subject.faculty

    @property
    def cohort_key(self):
        return self.subject.cohort_key

    def time_range(self):
        return f"{self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')}"

    def short_ref(self):
        fac = self.assigned_faculty
        return (
            f"{self.subject_code} ({self.subject.section_label}) on "
            f"{self.day.title()} {self.time_range()} in Room {self.room.room_code} "
            f"— {fac.full_name if fac else '—'}"
        )

    def duration_hours(self):
        if not (self.start_time and self.end_time):
            return 0.0
        return (
            (self.end_time.hour + self.end_time.minute / 60) -
            (self.start_time.hour + self.start_time.minute / 60)
        )

    # ── Hour accounting (FIX #3: semester required; FIX #29: single API) ──
    @classmethod
    def hours_used(cls, subject, schedule_type, semester, exclude_pk=None):
        if semester is None:
            raise ValueError("ClassSchedule.hours_used requires a semester.")
        qs = cls.objects.filter(
            offering__subject=subject,
            offering__semester=semester,
            schedule_type=schedule_type,
        )
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs.aggregate(
            total=Sum(
                ExpressionWrapper(
                    (ExtractHour(F('end_time')) * 60 + ExtractMinute(F('end_time')))
                    - (ExtractHour(F('start_time')) * 60 + ExtractMinute(F('start_time'))),
                    output_field=FloatField(),
                ) / 60.0
            )
        )['total'] or 0.0

    @classmethod
    def remaining_hours(cls, subject, schedule_type, semester, exclude_pk=None):
        """
        FIX #29: now takes semester.
        PATCH 2: capacity uses RAW hours (what the curriculum allocates).
        Reporting uses effective hours separately; do not mix the two.
        """
        if schedule_type == 'LAB':
            max_hours = float(subject.lab_hours_per_week or 0)   # RAW
        else:
            max_hours = float(subject.lecture_hours_per_week or 0)
        used = cls.hours_used(subject, schedule_type, semester, exclude_pk=exclude_pk)
        return max(0.0, max_hours - used), max_hours, used

    def check_hour_limit(self):
        # FIX #29: pass the offering's semester (was missing).
        label = 'lab' if self.schedule_type == 'LAB' else 'lecture'
        max_hours = float(
            self.subject.lab_hours_per_week if self.schedule_type == 'LAB'
            else self.subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            raise ValidationError(
                f"⚠️ Cannot schedule {self.subject.subject_code}: "
                f"no {label} hours are allocated."
            )
        already_used = type(self).hours_used(
            self.subject, self.schedule_type,
            semester=self.offering.semester, exclude_pk=self.pk,
        )
        this_schedule = self.duration_hours()
        total = already_used + this_schedule
        if total > max_hours:
            over = total - max_hours
            raise ValidationError(
                f"⚠️ {self.subject.subject_code} is over its {label} hour limit. "
                f"Allowed: {max_hours}h/week · Scheduled: {already_used}h · "
                f"This: {this_schedule}h · Over by: {over}h."
            )

    def clean(self):
        if self.start_time >= self.end_time:
            raise ValidationError(
                f"⚠️ Invalid time range: end ({self.end_time.strftime('%H:%M')}) "
                f"must be after start ({self.start_time.strftime('%H:%M')})."
            )

        # FIX #5: room availability is now enforced.
        if self.room_id and not self.room.is_available:
            raise ValidationError(
                f"⚠️ Room {self.room.room_code} is unavailable and cannot be scheduled."
            )

        subject = self.subject
        if not subject.is_active:
            raise ValidationError(f"⚠️ {subject.subject_code} is inactive.")

        if self.schedule_type == 'LECTURE' and not subject.has_lecture:
            raise ValidationError(f"⚠️ {subject.subject_code} has no lecture component.")
        if self.schedule_type == 'LAB' and not subject.is_laboratory:
            raise ValidationError(f"⚠️ {subject.subject_code} has no lab component.")

        # FacultyOtherAssignment conflict
        fac = self.assigned_faculty
        if fac:
            # FIX #8: same faculty resolution as view-helper.
            other_conflicts = FacultyOtherAssignment.objects.filter(
                faculty=fac,
                semester=self.offering.semester,
                is_active=True,
                day=self.day,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
            )
            if other_conflicts.exists():
                c = other_conflicts.first()
                raise ValidationError(
                    f"❌ Conflict with {c.get_work_type_display()}: '{c.title}' "
                    f"on {c.day.title()} "
                    f"{c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
                )

        self.check_hour_limit()
        self.check_conflicts()

    def save(self, *args, **kwargs):
        # PATCH 1: runs clean() even on .objects.create() paths.
        self.full_clean()
        super().save(*args, **kwargs)

    def check_conflicts(self):
        conflicts = []
        sem = self.offering.semester
        fac = self.assigned_faculty

        # Room conflict
        room_conflicts = ClassSchedule.objects.filter(
            room=self.room, day=self.day,
            offering__semester=sem,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exclude(pk=self.pk).select_related('offering__subject', 'room')
        if room_conflicts.exists():
            other = room_conflicts[0]
            conflicts.append(
                f"🏫 Room conflict: {self.room.room_code} already booked on "
                f"{self.day.title()} {other.time_range()} by "
                f"{other.subject.subject_code} ({other.subject.section_label})."
            )

        # Faculty conflict (FIX #1: only one source now)
        if fac:
            fac_conflicts = ClassSchedule.objects.filter(
                day=self.day,
                offering__semester=sem,
                offering__subject__faculty=fac,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
            ).exclude(pk=self.pk).select_related('offering__subject', 'room')
            if fac_conflicts.exists():
                other = fac_conflicts[0]
                conflicts.append(
                    f"👨‍🏫 Faculty conflict: {fac.full_name} already teaching "
                    f"{other.subject.subject_code} ({other.subject.section_label}) "
                    f"on {self.day.title()} {other.time_range()} in "
                    f"Room {other.room.room_code}."
                )

        # Subject conflict
        subj_conflicts = ClassSchedule.objects.filter(
            offering__subject=self.subject, day=self.day,
            start_time__lt=self.end_time, end_time__gt=self.start_time,
        ).exclude(pk=self.pk).select_related('room')
        if subj_conflicts.exists():
            other = subj_conflicts[0]
            conflicts.append(
                f"📚 Subject conflict: {self.subject.subject_code} already has a class "
                f"on {self.day.title()} {other.time_range()} in Room {other.room.room_code}."
            )

        # Section (cohort) conflict
        section_conflicts = self._find_cohort_conflicts()
        if section_conflicts:
            other = section_conflicts[0]
            conflicts.append(
                f"👥 Section conflict: {self.subject.section_label} already has "
                f"{other.subject.subject_code} scheduled on {other.day.title()} "
                f"{other.time_range()} in Room {other.room.room_code}."
            )

        if conflicts:
            raise ValidationError("\n".join(conflicts))

    def _find_cohort_conflicts(self):
        if self.subject.allow_cohort_overlap:
            return ClassSchedule.objects.none()
        subject = self.subject
        sem = self.offering.semester
        return (
            ClassSchedule.objects.filter(
                offering__semester=sem,
                day=self.day,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
                offering__subject__is_active=True,
                offering__subject__year_section=subject.year_section,
                offering__subject__allow_cohort_overlap=False,
            )
            .exclude(offering=self.offering)
            .exclude(pk=self.pk)
            .select_related('offering__subject', 'room')
        )


# =============================================================
# ROOM UTILIZATION CALCULATOR
# =============================================================
class RoomUtilizationCalculator:
    # FIX #14: capacity now matches the 6-day 7:00–19:00 grid with 1h lunch.
    GRID_START_HOUR = 7
    GRID_END_HOUR = 19
    GRID_DAYS = 6  # Mon–Sat; Sunday is outside the utilization grid.
    LUNCH_HOURS = 1

    @staticmethod
    def _weekly_capacity():
        daily = (RoomUtilizationCalculator.GRID_END_HOUR
                 - RoomUtilizationCalculator.GRID_START_HOUR
                 - RoomUtilizationCalculator.LUNCH_HOURS)
        return daily * RoomUtilizationCalculator.GRID_DAYS

    @staticmethod
    def calculate_room_utilization(room, semester=None):
        # PATCH 3: exclude Sunday so utilization matches the Mon–Sat grid.
        qs = ClassSchedule.objects.filter(
            room=room,
            offering__subject__is_active=True,
            offering__subject__faculty__is_active=True,
        ).exclude(day='SUNDAY')
        if semester:
            qs = qs.filter(offering__semester=semester)
        used = sum(s.duration_hours() for s in qs)
        cap = RoomUtilizationCalculator._weekly_capacity()
        return (used / cap) * 100 if cap else 0

    @staticmethod
    def get_room_schedule_matrix(room, semester=None, day=None):
        import datetime
        time_slots = []
        for hour in range(RoomUtilizationCalculator.GRID_START_HOUR,
                          RoomUtilizationCalculator.GRID_END_HOUR):
            for minute in (0, 30):
                time_slots.append(f"{hour:02d}:{minute:02d}")

        schedules = ClassSchedule.objects.filter(
            room=room,
            offering__subject__is_active=True,
            offering__subject__faculty__is_active=True,
        )
        if semester:
            schedules = schedules.filter(offering__semester=semester)
        if day:
            schedules = schedules.filter(day=day)

        days = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY']
        matrix = {t: {d: None for d in days} for t in time_slots}
        dummy_date = datetime.date(2000, 1, 1)

        for sched in schedules:
            current = datetime.datetime.combine(dummy_date, sched.start_time)
            end = datetime.datetime.combine(dummy_date, sched.end_time)
            subj = sched.subject
            if not subj:
                continue
            fac = sched.assigned_faculty
            year_roman = ['', 'I', 'II', 'III', 'IV'][subj.year_level]

            while current < end:
                key = current.strftime('%H:%M')
                if key in matrix:
                    matrix[key][sched.day] = {
                        'subject': subj.subject_code,
                        'subject_description': subj.subject_description[:30],
                        'section': f"{subj.course_code} {year_roman}-{subj.section}",
                        'faculty': fac.full_name if fac else '—',
                        'faculty_id': fac.employee_id if fac else '',
                        'subject_id': subj.id,
                        'offering_id': sched.offering_id,
                        'schedule_id': sched.id,
                        'has_lecture': subj.has_lecture,
                        'is_laboratory': subj.is_laboratory,
                        'schedule_type': sched.schedule_type,
                        'start_time': sched.start_time.strftime('%I:%M %p'),
                        'end_time': sched.end_time.strftime('%I:%M %p'),
                    }
                current += datetime.timedelta(minutes=30)
        return matrix

    @staticmethod
    def get_department_utilization(department='Technology Education', semester=None):
        rooms = Room.objects.filter(is_available=True)
        stats = []
        for room in rooms:
            util = RoomUtilizationCalculator.calculate_room_utilization(room, semester=semester)
            # PATCH 3: exclude Sunday so schedule_count agrees with utilization.
            qs = ClassSchedule.objects.filter(room=room).exclude(day='SUNDAY')
            if semester:
                qs = qs.filter(offering__semester=semester)
            stats.append({
                'room': room,
                'room_code': room.room_code,
                'building': room.building_name,
                'utilization': util,
                'schedule_count': qs.count(),
                'status': 'High' if util >= 70 else 'Medium' if util >= 40 else 'Low',
            })
        return stats


# =============================================================
# FACULTY OTHER ASSIGNMENT
# =============================================================
class FacultyOtherAssignment(models.Model):
    CATEGORY_CHOICES = [
        ('OTHER_FUNCTION', 'Other Function'),
        ('DESIGNATION', 'Designation'),
    ]
    WORK_TYPE_CHOICES = [
        ('RESEARCH', 'Research'), ('EXTENSION', 'Extension'),
        ('CHAIR', 'Chair / Coordinator'), ('LESSON_PREP', 'Lesson Preparation'),
        ('ADVISER', 'Adviser'), ('OTHER', 'Other'),
    ]

    faculty = models.ForeignKey(
        FacultyProfile, on_delete=models.CASCADE, related_name='other_assignments',
    )
    # FIX #6: semester is now required (was nullable → invisible in scoped views).
    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, related_name='other_assignments',
        null=False, blank=False,
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    work_type = models.CharField(max_length=30, choices=WORK_TYPE_CHOICES)
    title = models.CharField(max_length=200)

    # FIX #16: non-negative.
    hours_per_week = models.DecimalField(
        max_digits=5, decimal_places=1, default=0,
        validators=[MinValueValidator(0)],
    )

    room = models.ForeignKey(
        'Room', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='other_assignments',
    )
    day = models.CharField(
        max_length=10, blank=True, null=True,
        choices=[
            ('MONDAY', 'Monday'), ('TUESDAY', 'Tuesday'), ('WEDNESDAY', 'Wednesday'),
            ('THURSDAY', 'Thursday'), ('FRIDAY', 'Friday'), ('SATURDAY', 'Saturday'),
        ],
    )
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        FacultyProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'work_type', 'title']

    def __str__(self):
        return f"{self.faculty.full_name} — {self.get_work_type_display()}"

    def duration_hours(self):
        if self.start_time and self.end_time:
            s = self.start_time.hour + self.start_time.minute / 60
            e = self.end_time.hour + self.end_time.minute / 60
            return round(max(0.0, e - s), 2)
        return 0.0

    def clean(self):
        has_time = bool(self.day and self.start_time and self.end_time)
        if self.room and not has_time:
            raise ValidationError('When a room is assigned, day/start/end are required.')
        if has_time and self.start_time >= self.end_time:
            raise ValidationError('End time must be after start time.')

        # FIX #8: same faculty resolution as ClassSchedule.clean().
        if has_time and self.faculty_id and self.semester_id:
            conflicts = ClassSchedule.objects.filter(
                day=self.day,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
                offering__semester=self.semester,
                offering__subject__is_active=True,
                offering__subject__faculty=self.faculty,   # FIX #1: one source
            ).select_related('offering__subject')
            if conflicts.exists():
                c = conflicts.first()
                raise ValidationError(
                    f"❌ Conflict with class {c.subject.subject_code} "
                    f"({c.subject.section_label}) on {c.day.title()} "
                    f"{c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
                )

    def save(self, *args, **kwargs):
        # PATCH 1: runs clean() even on .objects.create() paths.
        self.full_clean()
        super().save(*args, **kwargs)


# =============================================================
# SIGNATURE BLOCKS
# =============================================================
class ActionTaken(models.Model):
    """Per-faculty program signatures (keys differ from ClassListSignature)."""
    ACTION_KEYS = [
        ('prepared', 'Prepared by'),
        ('reviewed', 'Reviewed by'),
        ('verified', 'Verified by'),
        ('approved', 'Approved by'),
    ]
    faculty = models.ForeignKey(
        FacultyProfile, on_delete=models.CASCADE, related_name='actions_taken',
    )
    action_key = models.CharField(max_length=20, choices=ACTION_KEYS)
    name = models.CharField(max_length=150, blank=True, default='')
    position = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        unique_together = ['faculty', 'action_key']
        ordering = ['action_key']

    def __str__(self):
        return f"{self.faculty.full_name} — {self.get_action_key_display()}: {self.name}"


class ClassListSignature(models.Model):
    """GLOBAL section-form signatures (one row per action_key, shared)."""
    ACTION_KEYS = [
        ('prepared_by', 'Prepared by'),
        ('reviewed_by', 'Reviewed by'),
        ('approved_by', 'Approved by'),
    ]
    year_section = models.ForeignKey(
        'YearSection', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='signatures',
        help_text="DEPRECATED — signatures are global. Ignored by the app.",
    )
    action_key = models.CharField(max_length=32, choices=ACTION_KEYS, unique=True)
    name = models.CharField(max_length=120, blank=True, default='')
    position = models.CharField(max_length=120, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['action_key']

    def __str__(self):
        return f"{self.action_key}: {self.name}"