from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.core.exceptions import ValidationError
from django.utils import timezone


# FACULTY MODEL
class FacultyProfile(models.Model):
    EMPLOYMENT_TYPE = [
        ('FULL', 'Full Time'),
        ('PART', 'Part Time'),
        ('CONTRACT', 'Contractual'),
    ]

    employee_id = models.CharField(max_length=20, unique=True, primary_key=True)
    full_name = models.CharField(max_length=100)
    specialization = models.CharField(max_length=100)
    attained_degree = models.CharField(max_length=50)
    employment_type = models.CharField(max_length=10, choices=EMPLOYMENT_TYPE, default='PART')
    department = models.CharField(max_length=100, default='Department of Technology Education')
    is_chairman = models.BooleanField(default=False)

    password = models.CharField(max_length=128, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    last_login = models.DateTimeField(null=True, blank=True)

    # models.py - inside class FacultyProfile

    # ... existing fields ...
    effective_date = models.CharField(max_length=64, blank=True, default='Aug. 3, 2026')

    # ── NEW: Editable term ──
    term = models.CharField(
        max_length=64, blank=True, default='First Semester 2026-2027',
        help_text="Academic term shown on the class program."
    )

    # ── ADD THESE FIELDS ──
    overload_hours = models.CharField(
        max_length=64, blank=True, default='',
        help_text="Overload hours/text."
    )
    overload_subjects = models.TextField(
        blank=True, default='',
        help_text="List of overload subjects."
    )
    service_credits = models.CharField(
        max_length=64, blank=True, default='',
        help_text="Service credits text."
    )
    adviser_notes = models.TextField(
        blank=True, default='',
        help_text="Adviser / Notes text."
    )

    class Meta:
        ordering = ['full_name']
        indexes = [
            models.Index(fields=['employee_id']),
            models.Index(fields=['is_chairman']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        role = "Chairman" if self.is_chairman else "Faculty"
        return f"{self.full_name} ({role})"

    def set_password(self, raw_password):
        if raw_password:
            self.password = make_password(raw_password)
        else:
            self.password = None

    def check_password(self, raw_password):
        if self.password is None or not raw_password:
            return False
        return check_password(raw_password, self.password)

    @classmethod
    def authenticate(cls, employee_id, password):
        try:
            faculty = cls.objects.get(employee_id=employee_id, is_active=True)
            if faculty.check_password(password):
                return faculty
        except cls.DoesNotExist:
            pass
        return None


# ROOM MODEL
class Room(models.Model):
    PROGRAM_CHOICES = [
        ('BTLED',  'Bachelor of Technology and Livelihood Education'),
        ('BTVTED', 'Bachelor of Technical-Vocational Teacher Education'),
    ]

    DEPARTMENT_MAP = {
        'BTLED':  'DEPARTMENT OF TECHNOLOGY EDUCATION',
        'BTVTED': 'DEPARTMENT OF TECHNOLOGY EDUCATION',
    }

    program = models.CharField(
        max_length=20,
        choices=PROGRAM_CHOICES,
        default='BTVTED',
        help_text="Program/department this room belongs to"
    )

    room_name = models.CharField(max_length=50)
    room_code = models.CharField(max_length=20, unique=True)
    floor_number = models.IntegerField()
    building_name = models.CharField(max_length=50)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ['program', 'building_name', 'room_name']
        verbose_name_plural = "Rooms"
        unique_together = ['building_name', 'room_code']

    def __str__(self):
        return f"{self.room_code} - {self.building_name} ({self.get_program_display()})"

    @property
    def department_name(self):
        return self.DEPARTMENT_MAP.get(self.program, 'DEPARTMENT OF TECHNOLOGY EDUCATION')

    @property
    def program_display(self):
        return self.get_program_display()


# YEAR SECTION MODEL (Renamed from Section)
class YearSection(models.Model):
    """Centralized table for student cohorts (e.g., BSIT 3-B)"""
    YEAR_LEVEL_CHOICES = [
        (1, '1st Year'),
        (2, '2nd Year'),
        (3, '3rd Year'),
        (4, '4th Year'),
    ]

    course_code = models.CharField(max_length=50, help_text="e.g., BSIT, BTLED")
    major = models.CharField(max_length=100, blank=True, help_text="e.g., Information Technology")
    year_level = models.IntegerField(choices=YEAR_LEVEL_CHOICES)
    section = models.CharField(max_length=5, help_text="e.g., A, B, C")

    class Meta:
        unique_together = ['course_code', 'major', 'year_level', 'section']
        ordering = ['course_code', 'year_level', 'section']
        verbose_name = "Year & Section"
        verbose_name_plural = "Year & Sections"

    def __str__(self):
        major_str = f" {self.major}" if self.major else ""
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level]
        return f"{self.course_code} {year_roman}-{self.section}{major_str}"


class Subject(models.Model):
    YEAR_LEVEL_CHOICES = YearSection.YEAR_LEVEL_CHOICES

    year_section = models.ForeignKey(
        YearSection,
        on_delete=models.PROTECT,
        related_name='subjects',
    )

    subject_code = models.CharField(max_length=20)
    subject_description = models.TextField()

    lecture_hours_per_week = models.DecimalField(max_digits=4, decimal_places=1, default=0)
    lab_hours_per_week = models.DecimalField(max_digits=4, decimal_places=1, default=0)

    faculty = models.ForeignKey(
        'FacultyProfile',
        on_delete=models.CASCADE,
        related_name='subjects',
        help_text="Faculty assigned to teach this subject",
    )

    has_lecture = models.BooleanField(default=True, help_text="Does this subject have lecture component?")
    is_laboratory = models.BooleanField(default=False, help_text="Does this subject have lab component?")
    is_active = models.BooleanField(default=True)

    allow_cohort_overlap = models.BooleanField(
        default=False,
        help_text="If True, this subject is exempt from the section-conflict check.",
    )

    total_students = models.PositiveIntegerField(
        default=0,
        help_text="Total number of students enrolled in this class (set by faculty/chairman).",
    )

    class Meta:
        ordering = [
            'year_section__course_code',
            'year_section__year_level',
            'year_section__section',
        ]
        verbose_name_plural = "Subjects"
        unique_together = [['year_section', 'subject_code']]

    # ── Derived read-only properties (backwards-compatible with old field names) ──
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

    def save(self, *args, **kwargs):
        if self.lab_hours_per_week > 0 and self.lecture_hours_per_week == 0:
            self.is_laboratory, self.has_lecture = True, False
        elif self.lecture_hours_per_week > 0 and self.lab_hours_per_week == 0:
            self.is_laboratory, self.has_lecture = False, True
        elif self.lecture_hours_per_week > 0 and self.lab_hours_per_week > 0:
            self.is_laboratory, self.has_lecture = True, True
        super().save(*args, **kwargs)

    def get_subject_display(self):
        major_str = f" {self.major}" if self.major else ""
        year_roman = ['', 'I', 'II', 'III', 'IV'][self.year_level]
        return f"{self.subject_code} {self.course_code} {year_roman}-{self.section}{major_str}"

    @property
    def total_hours_per_week(self):
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

    def clean(self):
        if not self.has_lecture and self.lecture_hours_per_week > 0:
            raise ValidationError("Lecture hours must be 0 when has_lecture is False")
        if not self.is_laboratory and self.lab_hours_per_week > 0:
            raise ValidationError("Lab hours must be 0 when is_laboratory is False")

    # models.py — inside class Subject

    LAB_HOUR_MULTIPLIER = 0.7  # 3 lab contact hours → 2.1 effective hours

    def effective_lab_hours_per_week(self):
        """Lab hours weighted by 0.7 for scheduling/computation."""
        return round(float(self.lab_hours_per_week or 0) * self.LAB_HOUR_MULTIPLIER, 2)

    def effective_total_hours_per_week(self):
        """Lecture + effective lab hours."""
        lec = float(self.lecture_hours_per_week or 0) if self.has_lecture else 0.0
        lab = self.effective_lab_hours_per_week() if self.is_laboratory else 0.0
        return round(lec + lab, 2)
    

# CLASS SCHEDULE
class ClassSchedule(models.Model):
    DAY_CHOICES = [
        ('MONDAY', 'Monday'),
        ('TUESDAY', 'Tuesday'),
        ('WEDNESDAY', 'Wednesday'),
        ('THURSDAY', 'Thursday'),
        ('FRIDAY', 'Friday'),
        ('SATURDAY', 'Saturday'),
        ('SUNDAY', 'Sunday'),
    ]

    SCHEDULE_TYPE_CHOICES = [
        ('LECTURE', 'Lecture'),
        ('LAB', 'Laboratory'),
    ]

    faculty = models.ForeignKey(
        FacultyProfile,
        on_delete=models.CASCADE,
        related_name='class_schedules',
        null=True,
        blank=True,
        help_text="Optional: Override the faculty for this specific schedule"
    )
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='schedules')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='schedules')

    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()

    schedule_type = models.CharField(
        max_length=10,
        choices=SCHEDULE_TYPE_CHOICES,
        default='LECTURE'
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        FacultyProfile, on_delete=models.SET_NULL, null=True,
        related_name='schedules_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['day', 'start_time']
        indexes = [
            models.Index(fields=['faculty', 'day']),
            models.Index(fields=['room', 'day']),
            models.Index(fields=['subject', 'day']),
            models.Index(fields=['day', 'start_time', 'end_time']),
        ]
        unique_together = [
            ['room', 'day', 'start_time'],
            ['subject', 'day', 'start_time'],
        ]

    def __str__(self):
        faculty_name = self.faculty.full_name if self.faculty else self.subject.faculty.full_name
        return f"{self.subject.subject_code} - {self.subject.get_subject_display()} - {self.day} {self.start_time.strftime('%H:%M')} ({self.schedule_type}) - {faculty_name}"

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
        else:
            return self.subject.lab_hours_per_week

    @property
    def assigned_faculty(self):
        return self.faculty if self.faculty else self.subject.faculty

    @property
    def cohort_key(self):
        return self.subject.cohort_key

    def time_range(self):
        return f"{self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')}"

    def short_ref(self):
        faculty = self.faculty if self.faculty else self.subject.faculty
        return (
            f"{self.subject.subject_code} "
            f"({self.subject.section_label}) on "
            f"{self.day.title()} {self.time_range()} "
            f"in Room {self.room.room_code}"
        )

    def duration_hours(self):
        start = self.start_time.hour + self.start_time.minute / 60
        end = self.end_time.hour + self.end_time.minute / 60
        return max(0, end - start)

    @classmethod
    def hours_used(cls, subject, schedule_type, exclude_pk=None):
        qs = cls.objects.filter(subject=subject, schedule_type=schedule_type)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        total = 0.0
        for s in qs:
            total += s.duration_hours()
        return total

    @classmethod
    def remaining_hours(cls, subject, schedule_type, exclude_pk=None):
        if schedule_type == 'LAB':
            max_hours = float(subject.lab_hours_per_week or 0)
        else:
            max_hours = float(subject.lecture_hours_per_week or 0)
        used = cls.hours_used(subject, schedule_type, exclude_pk=exclude_pk)
        return max(0.0, max_hours - used), max_hours, used

    def check_hour_limit(self):
        if self.schedule_type == 'LAB':
            max_hours = float(self.subject.lab_hours_per_week or 0)
            label = "lab"
        else:
            max_hours = float(self.subject.lecture_hours_per_week or 0)
            label = "lecture"

        if max_hours <= 0:
            raise ValidationError(
                f"⚠️ Cannot schedule {self.subject.subject_code}: "
                f"no {label} hours are allocated for this subject."
            )

        already_used = type(self).hours_used(
            self.subject, self.schedule_type, exclude_pk=self.pk
        )
        this_schedule = self.duration_hours()
        total = already_used + this_schedule

        if total > max_hours:
            over = total - max_hours
            raise ValidationError(
                f"⚠️ {self.subject.subject_code} is over its {label} hour limit. "
                f"Allowed: {max_hours}h per week · "
                f"Currently scheduled: {already_used}h · "
                f"This schedule adds: {this_schedule}h · "
                f"Over by: {over}h."
            )

    def clean(self):
        if self.start_time >= self.end_time:
            raise ValidationError(
                f"⚠️ Invalid time range: end time ({self.end_time.strftime('%H:%M')}) "
                f"must be after start time ({self.start_time.strftime('%H:%M')})."
            )

        if not self.subject.is_active:
            raise ValidationError(
                f"⚠️ {self.subject.subject_code} is inactive and cannot be scheduled."
            )

        if self.schedule_type == 'LECTURE' and not self.subject.has_lecture:
            raise ValidationError(
                f"⚠️ {self.subject.subject_code} has no lecture component — "
                f"cannot create a lecture schedule."
            )

        if self.schedule_type == 'LAB' and not self.subject.is_laboratory:
            raise ValidationError(
                f"⚠️ {self.subject.subject_code} has no lab component — "
                f"cannot create a lab schedule."
            )

        # ── ADD THIS BLOCK ──
        # Check if the faculty has an Other Function scheduled at this time
        actual_faculty = self.faculty if self.faculty else self.subject.faculty
        other_conflicts = FacultyOtherAssignment.objects.filter(
            faculty=actual_faculty,
            is_active=True,
            day=self.day,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time
        )

        if other_conflicts.exists():
            c = other_conflicts.first()
            raise ValidationError(
                f"❌ ERROR: Conflict Time! \n\n"
                f"This class overlaps with the {c.get_work_type_display()}: '{c.title}' "
                f"scheduled on {c.day.title()} "
                f"{c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
            )
        # ────────────────────

        self.check_hour_limit()
        self.check_conflicts()

    def check_conflicts(self):
        conflicts = []

        room_conflicts = ClassSchedule.objects.filter(
            room=self.room,
            day=self.day,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exclude(pk=self.pk).select_related('subject', 'faculty')

        if room_conflicts.exists():
            other = room_conflicts[0]
            other_subj = other.subject.subject_code
            other_sec = other.subject.section_label
            conflicts.append(
                f"🏫 Room conflict: {self.room.room_code} is already booked on "
                f"{self.day.title()} {other.time_range()} by {other_subj} "
                f"({other_sec}). Please pick a different room or time."
            )

        actual_faculty = self.faculty if self.faculty else self.subject.faculty
        faculty_conflicts = ClassSchedule.objects.filter(
            faculty=actual_faculty,
            day=self.day,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exclude(pk=self.pk).select_related('subject', 'room')

        if faculty_conflicts.exists():
            other = faculty_conflicts[0]
            other_subj = other.subject.subject_code
            other_sec = other.subject.section_label
            conflicts.append(
                f"👨‍🏫 Faculty conflict: {actual_faculty.full_name} is already teaching "
                f"{other_subj} ({other_sec}) on "
                f"{self.day.title()} {other.time_range()} in Room {other.room.room_code}."
            )

        subject_conflicts = ClassSchedule.objects.filter(
            subject=self.subject,
            day=self.day,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exclude(pk=self.pk).select_related('room')

        if subject_conflicts.exists():
            other = subject_conflicts[0]
            conflicts.append(
                f"📚 Subject conflict: {self.subject.subject_code} already has a class "
                f"on {self.day.title()} {other.time_range()} in Room {other.room.room_code}."
            )

        section_conflicts = self._find_cohort_conflicts()
        if section_conflicts:
            other = section_conflicts[0]
            section_label = self.subject.section_label
            other_subj = other.subject.subject_code
            conflicts.append(
                f"👥 Section conflict: {section_label} already has {other_subj} "
                f"scheduled on {other.day.title()} {other.time_range()} in "
                f"Room {other.room.room_code}. The same students cannot be in "
                f"two classes at once."
            )

        if conflicts:
            raise ValidationError("\n".join(conflicts))

    def _find_cohort_conflicts(self):
        if getattr(self.subject, 'allow_cohort_overlap', False):
            return ClassSchedule.objects.none()

        subject = self.subject
        if not subject.course_code or not subject.year_level or not subject.section:
            return ClassSchedule.objects.none()

        return (
            ClassSchedule.objects.filter(
                day=self.day,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
                subject__is_active=True,
                subject__year_section__course_code=subject.year_section.course_code,
                subject__year_section__year_level=subject.year_section.year_level,
                subject__year_section__section=subject.year_section.section,
                subject__allow_cohort_overlap=False,
            )
            .exclude(subject=subject)
            .exclude(pk=self.pk)
            .select_related('subject', 'room', 'faculty')
        )


# FACULTY WORKLOAD CALCULATOR
class FacultyWorkloadCalculator:
    @staticmethod
    def calculate_faculty_workload(faculty):
        subjects = Subject.objects.filter(faculty=faculty, is_active=True)

        lecture_hours = 0
        lab_hours = 0
        subjects_data = {}

        for subject in subjects:
            schedules = ClassSchedule.objects.filter(subject=subject)

            subject_lecture_hours = 0
            subject_lab_hours = 0

            for schedule in schedules:
                hours = (schedule.end_time.hour + schedule.end_time.minute / 60) - \
                        (schedule.start_time.hour + schedule.start_time.minute / 60)
                if schedule.schedule_type == 'LECTURE':
                    subject_lecture_hours += hours
                    lecture_hours += hours
                else:
                    subject_lab_hours += hours
                    lab_hours += hours

            subjects_data[subject.subject_code] = {
                'subject': subject,
                'lecture_hours': subject_lecture_hours,
                'lab_hours': subject_lab_hours,
                'total_hours': subject_lecture_hours + subject_lab_hours,
                'has_lecture': subject.has_lecture,
                'is_laboratory': subject.is_laboratory,
                'schedule_count': schedules.count(),
            }

        return {
            'subjects': subjects_data,
            'total_lecture_hours': lecture_hours,
            'total_lab_hours': lab_hours,
            'total_hours': lecture_hours + lab_hours,
            'total_subjects': subjects.count(),
        }

    @staticmethod
    def get_workload_summary(faculty):
        workload = FacultyWorkloadCalculator.calculate_faculty_workload(faculty)

        summary = []
        for subject_code, data in workload['subjects'].items():
            summary.append({
                'subject_display': data['subject'].get_subject_display(),
                'subject_code': subject_code,
                'lecture_hours': data['lecture_hours'],
                'lab_hours': data['lab_hours'],
                'total_hours': data['total_hours'],
                'has_lecture': data['has_lecture'],
                'is_laboratory': data['is_laboratory'],
                'schedule_count': data['schedule_count'],
            })

        summary.append({
            'subject_display': '**SUB-TOTAL**',
            'subject_code': '',
            'lecture_hours': workload['total_lecture_hours'],
            'lab_hours': workload['total_lab_hours'],
            'total_hours': workload['total_hours'],
            'has_lecture': False,
            'is_laboratory': False,
            'schedule_count': workload['total_subjects'],
        })

        return summary


# ROOM UTILIZATION CALCULATOR
class RoomUtilizationCalculator:
    @staticmethod
    def calculate_room_utilization(room):
        used_hours = 0
        schedules = ClassSchedule.objects.filter(room=room)

        for schedule in schedules:
            hours = (schedule.end_time.hour + schedule.end_time.minute / 60) - \
                    (schedule.start_time.hour + schedule.start_time.minute / 60)
            used_hours += hours

        total_available_hours = 8 * 5 * 18

        if total_available_hours > 0:
            return (used_hours / total_available_hours) * 100
        return 0

    @staticmethod
    def get_room_schedule_matrix(room, day=None):
        import datetime

        time_slots = []
        start_hour = 7
        end_hour = 20

        for hour in range(start_hour, end_hour):
            for minute in [0, 30]:
                time_slots.append(f"{hour:02d}:{minute:02d}")

        schedules = ClassSchedule.objects.filter(room=room)
        if day:
            schedules = schedules.filter(day=day)

        matrix = {}
        days = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']

        for time_slot in time_slots:
            matrix[time_slot] = {d: None for d in days}

        DUMMY_DATE = datetime.date(2000, 1, 1)

        for schedule in schedules:
            current = datetime.datetime.combine(DUMMY_DATE, schedule.start_time)
            end = datetime.datetime.combine(DUMMY_DATE, schedule.end_time)

            while current < end:
                time_key = current.strftime('%H:%M')
                if time_key in matrix:
                    subject = schedule.subject
                    year_roman = ['', 'I', 'II', 'III', 'IV'][subject.year_level]
                    assigned_faculty = schedule.faculty if schedule.faculty else subject.faculty

                    info = {
                        'subject': subject.subject_code,
                        'subject_description': subject.subject_description[:30],
                        'subject_display': f"{subject.course_code} {year_roman}-{subject.section}", # Clean section label
                        'section': f"{subject.course_code} {year_roman}-{subject.section}",        # Clean section label
                        'faculty': assigned_faculty.full_name,
                        'faculty_id': assigned_faculty.employee_id,
                        'subject_id': subject.id,
                        'schedule_id': schedule.id,
                        'has_lecture': subject.has_lecture,
                        'is_laboratory': subject.is_laboratory,
                        'schedule_type': schedule.schedule_type,
                        'start_time': schedule.start_time.strftime('%I:%M %p'),
                        'end_time': schedule.end_time.strftime('%I:%M %p'),
                        'year_level': subject.year_section.get_year_level_display(),               # FIXED CRASH
                        'course_code': subject.course_code,
                        'major': subject.major,
                        'lecture_hours': subject.lecture_hours_per_week,
                        'lab_hours': subject.lab_hours_per_week,
                    }
                    matrix[time_key][schedule.day] = info

                current += datetime.timedelta(minutes=30)

        return matrix

    @staticmethod
    def get_department_utilization(department='Technology Education'):
        rooms = Room.objects.filter(is_available=True)
        room_stats = []

        for room in rooms:
            utilization = RoomUtilizationCalculator.calculate_room_utilization(room)
            schedule_count = ClassSchedule.objects.filter(room=room).count()

            room_stats.append({
                'room': room,
                'room_code': room.room_code,
                'building': room.building_name,
                'utilization': utilization,
                'schedule_count': schedule_count,
                'status': 'High' if utilization >= 70 else 'Medium' if utilization >= 40 else 'Low'
            })

        return room_stats
    
class FacultyOtherAssignment(models.Model):
    """
    Non-teaching workload entries: Research, Extension, Designation, etc.
    Can be assigned by the faculty member themselves (or chairman).
    Optionally tied to a Room so it appears in room schedules.
    """
    CATEGORY_CHOICES = [
        ('OTHER_FUNCTION', 'Other Function'),
        ('DESIGNATION', 'Designation'),
    ]
    WORK_TYPE_CHOICES = [
        ('RESEARCH', 'Research'),
        ('EXTENSION', 'Extension'),
        ('CHAIR', 'Chair / Coordinator'),
        ('LESSON_PREP', 'Lesson Preparation'),
        ('ADVISER', 'Adviser'),
        ('OTHER', 'Other'),
    ]

    faculty = models.ForeignKey(
        FacultyProfile,
        on_delete=models.CASCADE,
        related_name='other_assignments',
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    work_type = models.CharField(max_length=30, choices=WORK_TYPE_CHOICES)
    title = models.CharField(max_length=200, help_text='e.g. Chair, TTEd / Research on X')
    hours_per_week = models.DecimalField(
        max_digits=5, decimal_places=1, default=0,
    )
    room = models.ForeignKey(
        'Room', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='other_assignments',
    )
    day = models.CharField(
        max_length=10, blank=True, null=True,
        choices=[
            ('MONDAY','Monday'),('TUESDAY','Tuesday'),('WEDNESDAY','Wednesday'),
            ('THURSDAY','Thursday'),('FRIDAY','Friday'),('SATURDAY','Saturday'),
        ],
    )
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        FacultyProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
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
        # Room + day + time must be all set or all empty
        has_time = bool(self.day and self.start_time and self.end_time)
        if self.room and not has_time:
            raise ValidationError('When a room is assigned, day/start/end time are required.')
        if has_time and self.start_time >= self.end_time:
            raise ValidationError('End time must be after start time.')
            
        # Check for conflicts with teaching load
        if has_time and self.faculty:
            # Use apps.get_model to avoid circular imports
            from django.apps import apps
            ClassSchedule = apps.get_model('workload', 'ClassSchedule') 
            # ⚠️ REPLACE 'faculty_workload' WITH YOUR ACTUAL APP NAME (e.g., 'core', 'workload', 'system', etc.)

            conflicts = ClassSchedule.objects.filter(
                subject__faculty=self.faculty,
                subject__is_active=True,
                day=self.day,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time
            ).select_related('subject')

            if conflicts.exists():
                c = conflicts.first()
                raise ValidationError(
                    f"❌ ERROR: Conflict Time! \n\n"
                    f"This {self.get_work_type_display()} overlaps with the class "
                    f"{c.subject.subject_code} ({c.subject.section_label}) scheduled on "
                    f"{c.day.title()} {c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
                )

class ActionTaken(models.Model):
    """
    Free-form signature-block entries on the class program.
    One row per (faculty, action_key). Name/position are arbitrary text
    typed by the user — NOT tied to a FacultyProfile.
    """
    ACTION_KEYS = [
        ('prepared', 'Prepared by'),
        ('reviewed', 'Reviewed by'),
        ('verified', 'Verified by'),
        ('approved', 'Approved by'),
    ]

    faculty = models.ForeignKey(
        FacultyProfile, on_delete=models.CASCADE,
        related_name='actions_taken',
    )
    action_key = models.CharField(max_length=20, choices=ACTION_KEYS)
    name       = models.CharField(max_length=150, blank=True, default='')
    position   = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        unique_together = ['faculty', 'action_key']
        ordering = ['action_key']

    def __str__(self):
        return f"{self.faculty.full_name} — {self.get_action_key_display()}: {self.name}"

