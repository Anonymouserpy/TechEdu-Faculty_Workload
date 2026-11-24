"""
Shared class-program building blocks.

Both `class_program_view` (renders class_program.html) and
`class_program_data` (JSON for the All Class Programs modal) previously
carried near-identical copies of the grid/workload code. Everything that
turns schedules into a printable grid now lives here so the two views can
never drift apart again.
"""

from datetime import datetime, timedelta

from . import models

DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY']
DUMMY_DATE = datetime(2000, 1, 1)
GRID_START = '7:00'
GRID_END = '19:00'


def _inject(schedules_by_time, day, start_dt, end_dt, info):
    """Mark every 30-minute slot covered by one schedule/assignment."""
    slot_dt = start_dt
    slot_keys = []
    while slot_dt < end_dt:
        slot_keys.append(slot_dt.strftime('%H:%M'))
        slot_dt += timedelta(minutes=30)
    total_slots = len(slot_keys)
    for idx, time_key in enumerate(slot_keys):
        day_map = schedules_by_time.setdefault(time_key, {})
        if day in day_map:
            continue
        day_map[day] = {
            'info': info,
            'is_start': (idx == 0),
            'rowspan': total_slots if idx == 0 else 0,
        }


def _schedule_info(schedule, faculty):
    subj = schedule.offering.subject if schedule.offering else None
    return {
        'id': schedule.id,
        'subject_code': subj.subject_code if subj else '',
        'subject_description': subj.subject_description if subj else '',
        'section_display': subj.get_subject_display() if subj else '',
        'section_short': subj.section_display if subj else '',
        'room': schedule.room.room_code if schedule.room else '',
        'room_id': schedule.room.id if schedule.room else None,
        'subject_id': subj.id if subj else None,
        'is_lab': schedule.schedule_type == 'LAB',
        'schedule_type': schedule.schedule_type,
        'start_time': schedule.start_time.strftime('%I:%M %p'),
        'end_time': schedule.end_time.strftime('%I:%M %p'),
        'start_time_raw': schedule.start_time.strftime('%H:%M'),
        'end_time_raw': schedule.end_time.strftime('%H:%M'),
        'faculty_name': faculty.full_name,
        'subject_title': subj.subject_description if subj else '',
    }


def _other_info(assignment, faculty):
    return {
        'id': assignment.id,
        'subject_code': assignment.get_work_type_display().upper(),
        'subject_description': assignment.title,
        'section_display': '',
        'section_short': '',
        'room': assignment.room.room_code if assignment.room else '',
        'room_id': assignment.room.id if assignment.room else None,
        'subject_id': None,
        'is_lab': False,
        'schedule_type': 'OTHER',
        'start_time': assignment.start_time.strftime('%I:%M %p'),
        'end_time': assignment.end_time.strftime('%I:%M %p'),
        'start_time_raw': assignment.start_time.strftime('%H:%M'),
        'end_time_raw': assignment.end_time.strftime('%H:%M'),
        'faculty_name': faculty.full_name,
        'subject_title': assignment.title,
    }


def build_schedule_map(faculty, semester):
    """Return {time_str: {day: cell}} for classes + other assignments."""
    schedules_qs = models.ClassSchedule.objects.filter(
        offering__subject__faculty=faculty,
        offering__subject__is_active=True,
        offering__is_active=True,
    ).select_related('offering__subject', 'room').order_by('day', 'start_time')
    if semester is not None:
        schedules_qs = schedules_qs.filter(offering__semester=semester)
    schedules = list(schedules_qs)

    schedules_by_time = {}
    for schedule in schedules:
        _inject(
            schedules_by_time,
            schedule.day,
            datetime.combine(DUMMY_DATE, schedule.start_time),
            datetime.combine(DUMMY_DATE, schedule.end_time),
            _schedule_info(schedule, faculty),
        )

    other_qs = models.FacultyOtherAssignment.objects.filter(
        faculty=faculty,
        is_active=True,
        day__isnull=False,
        start_time__isnull=False,
        end_time__isnull=False,
    ).select_related('room')
    if semester is not None:
        other_qs = other_qs.filter(semester=semester)

    for assignment in other_qs:
        _inject(
            schedules_by_time,
            assignment.day,
            datetime.combine(DUMMY_DATE, assignment.start_time),
            datetime.combine(DUMMY_DATE, assignment.end_time),
            _other_info(assignment, faculty),
        )

    return schedules, schedules_by_time


def build_time_slots(schedules_by_time):
    """Build the 7:00–19:00 half-hour rows, collapsing 12:00–13:00 to lunch."""
    time_slots = []
    current = datetime.strptime(GRID_START, '%H:%M')
    end_time = datetime.strptime(GRID_END, '%H:%M')

    while current < end_time:
        time_str = current.strftime('%H:%M')
        display_str = current.strftime('%I:%M').lstrip('0')

        if time_str == '12:00':
            noon_end = current + timedelta(hours=1)
            time_slots.append({
                'key': time_str,
                'display': f"{display_str} - {noon_end.strftime('%I:%M').lstrip('0')}",
                'is_noon_break': True,
                'is_lunch': True,
                'schedules': {},
            })
            current = noon_end
            continue

        next_time = current + timedelta(minutes=30)
        slot_schedules = {}
        if time_str in schedules_by_time:
            for day in DAYS:
                if day in schedules_by_time[time_str]:
                    slot_schedules[day] = schedules_by_time[time_str][day]

        time_slots.append({
            'key': time_str,
            'display': f"{display_str} - {next_time.strftime('%I:%M').lstrip('0')}",
            'is_noon_break': False,
            'is_lunch': False,
            'schedules': slot_schedules,
        })
        current = next_time

    return time_slots


def get_faculty_subjects(faculty, semester):
    subjects = models.Subject.objects.filter(
        is_active=True,
        faculty__is_active=True,
        faculty=faculty,
    )
    if semester is not None:
        subjects = subjects.filter(
            offerings__semester=semester,
            offerings__is_active=True,
        ).distinct()
    return list(
        subjects.select_related('year_section').order_by('subject_code')
    )


def build_workload(faculty_subjects, semester):
    """Teaching-load rows + totals, excluding subjects without schedules."""
    rows = []
    total_lec = 0.0
    total_lab = 0.0
    total_lab_raw = 0.0
    total_students = 0
    total_weighted = 0.0

    for subject in faculty_subjects:
        sched_qs = models.ClassSchedule.objects.filter(
            offering__subject=subject,
            offering__is_active=True,
        )
        if semester is not None:
            sched_qs = sched_qs.filter(offering__semester=semester)

        subject_schedules = list(sched_qs)
        lec_hours = sum(
            sch.duration_hours() for sch in subject_schedules
            if sch.schedule_type == 'LECTURE'
        )
        lab_hours = sum(
            sch.duration_hours() for sch in subject_schedules
            if sch.schedule_type == 'LAB'
        )
        schedule_count = len(subject_schedules)
        if schedule_count == 0:
            continue

        effective_lab_hours = round(
            lab_hours * getattr(subject, 'LAB_HOUR_MULTIPLIER', 0.7), 1
        )
        students = subject.total_students or 0
        weighted_hours = round(lec_hours + (lab_hours * 0.7), 2)

        total_lec += lec_hours
        total_lab += effective_lab_hours
        total_lab_raw += lab_hours
        total_students += students
        total_weighted += weighted_hours

        rows.append({
            'subject_id': subject.id,
            'subject_code': subject.subject_code,
            'subject_description': subject.subject_description,
            'section_display': subject.get_subject_display(),
            'lec_hours': round(lec_hours, 1),
            'lab_hours': round(lab_hours, 1),
            'lab_hours_effective': effective_lab_hours,
            'students': students,
            'schedule_count': schedule_count,
            'weighted_hours': weighted_hours,
        })

    return {
        'rows': rows,
        'total_lec': round(total_lec, 1),
        'total_lab': round(total_lab, 1),
        'total_lab_raw': round(total_lab_raw, 1),
        'total_weighted': round(total_weighted, 1),
        'total_students': total_students,
        'no_preparations': len({
            (row['subject_code'], row['section_display']) for row in rows
        }),
    }


def _assignment_payload(assignment):
    return {
        'title': assignment.title,
        'hours': float(assignment.hours_per_week or 0),
        'room': assignment.room.room_code if assignment.room else '',
        'day': assignment.day or '',
        'start': assignment.start_time.strftime('%H:%M') if assignment.start_time else '',
        'end': assignment.end_time.strftime('%H:%M') if assignment.end_time else '',
    }


def build_class_program_payload(faculty, semester):
    """Everything both class-program views need, computed once."""
    schedules, schedules_by_time = build_schedule_map(faculty, semester)
    time_slots = build_time_slots(schedules_by_time)
    faculty_subjects = get_faculty_subjects(faculty, semester)
    workload = build_workload(faculty_subjects, semester)

    other_qs = models.FacultyOtherAssignment.objects.filter(
        faculty=faculty,
        is_active=True,
    ).select_related('room').order_by('category', 'work_type', 'title')
    if semester is not None:
        other_qs = other_qs.filter(semester=semester)
    other_list = list(other_qs)
    other = [a for a in other_list if a.category == 'OTHER_FUNCTION']
    designations = [a for a in other_list if a.category == 'DESIGNATION']

    other_hours_total = round(
        sum(float(a.hours_per_week or 0) for a in other), 1
    )
    desig_hours_total = round(
        sum(float(a.hours_per_week or 0) for a in designations), 1
    )

    actions = {
        a.action_key: {'name': a.name, 'position': a.position}
        for a in models.ActionTaken.objects.filter(faculty=faculty)
    }

    return {
        'schedules': schedules,
        'time_slots': time_slots,
        'faculty_subjects': faculty_subjects,
        'workload_rows': workload['rows'],
        'total_lec': workload['total_lec'],
        'total_lab': workload['total_lab'],
        'total_lab_raw': workload['total_lab_raw'],
        'total_weighted': workload['total_weighted'],
        'total_students': workload['total_students'],
        'no_preparations': workload['no_preparations'],
        'other_assignments': other_list,
        'other_assignments_other': other,
        'other_assignments_desig': designations,
        'other_data': [_assignment_payload(a) for a in other],
        'desig_data': [_assignment_payload(a) for a in designations],
        'other_hours_total': other_hours_total,
        'desig_hours_total': desig_hours_total,
        'grand_total_hours': round(
            workload['total_weighted'] + other_hours_total + desig_hours_total, 1
        ),
        'actions': actions,
    }
