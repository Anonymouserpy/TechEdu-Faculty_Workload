from django.conf import settings
from django.utils import timezone
from django.contrib.auth.hashers import make_password, check_password
from django.contrib import messages
from django.shortcuts import redirect, render, get_object_or_404
from django.db import IntegrityError
from django.db.models import Q, Count, Sum, Prefetch
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from . import models, services
import json
from django.http import JsonResponse
from datetime import datetime, timedelta
from django.views.decorators.http import require_POST
from django.db import transaction
from django.urls import reverse


# ============================================================
# HELPERS
# ============================================================
# views.py

def _resolve_offering(subject, semester=None, create=False):
    """
    Return the SubjectOffering for `subject` in `semester`.

    Creation is opt-in (`create=True`) and only used by the POST schedule
    assignment endpoints. Read-only callers never get a row written as a
    side effect, and a race between two requests is tolerated (the unique
    constraint decides the winner; we then return the existing row).

    If `semester` is None, fall back to the active semester, then to any
    existing offering for the subject.
    """
    if semester is None:
        semester = models.Semester.get_current()

    if semester is not None:
        offering = models.SubjectOffering.objects.filter(
            subject=subject, semester=semester
        ).first()
        if offering or not create:
            return offering
        try:
            with transaction.atomic():
                return models.SubjectOffering.objects.create(
                    subject=subject,
                    semester=semester,
                    is_active=True,
                )
        except IntegrityError:
            return models.SubjectOffering.objects.filter(
                subject=subject, semester=semester
            ).first()

    # No semester available at all — fall back to any existing offering
    return models.SubjectOffering.objects.filter(subject=subject).first()
    
def _check_other_assignment_conflict(faculty, day, start_time, end_time,
                                     exclude_id=None, semester=None):
    if not day or not start_time or not end_time:
        return None

    # 1. Class schedules
    class_qs = models.ClassSchedule.objects.filter(
        offering__isnull=False,
        offering__subject__faculty=faculty,
        offering__subject__is_active=True,
        day=day,
        start_time__lt=end_time,
        end_time__gt=start_time,
    ).select_related('offering__subject')

    if semester is not None:
        class_qs = class_qs.filter(offering__semester=semester)

    if class_qs.exists():
        c = class_qs.first()
        return (
            f"❌ ERROR: Conflict Time! \n\n"
            f"This schedule overlaps with the class {c.subject.subject_code} "
            f"({c.subject.section_label}) which is scheduled on "
            f"{c.day.title()} {c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
        )

    # 2. Other functions / designations
    other_qs = models.FacultyOtherAssignment.objects.filter(
        faculty=faculty,
        is_active=True,
        day=day,
        start_time__lt=end_time,
        end_time__gt=start_time,
    )

    if semester is not None:
        other_qs = other_qs.filter(semester=semester)

    if exclude_id:
        other_qs = other_qs.exclude(id=exclude_id)

    if other_qs.exists():
        c = other_qs.first()
        return (
            f"❌ ERROR: Conflict Time! \n\n"
            f"This schedule overlaps with the {c.get_work_type_display()}: '{c.title}' "
            f"which is scheduled on "
            f"{c.day.title()} {c.start_time.strftime('%I:%M %p')}–{c.end_time.strftime('%I:%M %p')}."
        )

    return None


def _can_edit_other_assignment(requesting_faculty, target_faculty):
    """
    Chairman can edit anyone's. A faculty can edit only their own.
    """
    return (
        requesting_faculty.is_chairman
        or requesting_faculty.employee_id == target_faculty.employee_id
    )


def _active_subjects():
    """
    Central place for the 'which subjects should be visible' rule.

    A subject is visible only if:
      - the subject itself is active, AND
      - its assigned faculty is active.

    This means deactivating a faculty member automatically hides
    all of their subjects from every listing / report.
    """
    return models.Subject.objects.filter(
        is_active=True,
        faculty__is_active=True,
    )



# views.py — _annotate_subject_progress

def _annotate_subject_progress(subjects, semester=None):
    if semester is None:
        semester = models.Semester.get_current()
    for s in subjects:
        if semester is not None:
            lecture_used = models.ClassSchedule.hours_used(s, 'LECTURE', semester=semester)
            lab_used     = models.ClassSchedule.hours_used(s, 'LAB',     semester=semester)
        else:
            lecture_used = 0.0
            lab_used     = 0.0

        lecture_max = float(s.lecture_hours_per_week or 0)
        # Scheduling capacity uses RAW lab hours; the 0.7 credit multiplier
        # applies only to workload reporting (see Subject.total_hours_per_week).
        lab_max     = s.raw_lab_hours_per_week

        s.lecture_used      = round(lecture_used, 1)
        s.lab_used          = round(lab_used, 1)
        s.lecture_remaining = round(max(0.0, lecture_max - lecture_used), 1)
        s.lab_remaining     = round(max(0.0, lab_max - lab_used), 1)

        lecture_done = (not s.has_lecture) or (s.lecture_remaining <= 0.0)
        lab_done     = (not s.is_laboratory) or (s.lab_remaining <= 0.0)
        s.is_fully_scheduled = lecture_done and lab_done

        if semester is not None:
            s.schedule_count = models.ClassSchedule.objects.filter(
                offering__subject=s, offering__semester=semester
            ).count()
        else:
            s.schedule_count = models.ClassSchedule.objects.filter(
                offering__subject=s
            ).count()
    return subjects

# ============================================================
# CLASS LIST — EDITABLE SIGNATURES
# ============================================================

DEFAULT_CLASS_LIST_SIGNATURES = {
    'prepared_by': {
        'name': 'HAIDE D. SELAPA, PhD',
        'position': 'Chairman',
    },
    'reviewed_by': {
        'name': 'RONALD B. LACABA, REE, MSME',
        'position': 'Dean',
    },
    'approved_by': {
        'name': 'NELSON E. ENAGE, PhD, EdD',
        'position': 'VP-Academic Affairs',
    },
}


def _get_class_list_signatures(section=None):
    """
    GLOBAL signatures — same name/position for every section.
    `section` is accepted for backwards-compatibility but ignored.
    """
    merged = {k: dict(v) for k, v in DEFAULT_CLASS_LIST_SIGNATURES.items()}

    saved = models.ClassListSignature.objects.all()
    for s in saved:
        if s.action_key in merged:
            if s.name:
                merged[s.action_key]['name'] = s.name
            if s.position:
                merged[s.action_key]['position'] = s.position
    return merged


@require_POST
def room_create_ajax(request):
    if not request.session.get('faculty_id'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=401)
    try:
        _current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Faculty not found'}, status=404)
    if not _current.is_chairman:
        return JsonResponse({'success': False, 'error': 'Permission denied.'}, status=403)

    room_name     = request.POST.get('room_name', '').strip()
    room_code     = request.POST.get('room_code', '').strip().upper()
    building_name = request.POST.get('building_name', '').strip()
    floor_number  = request.POST.get('floor_number', '').strip()
    program       = request.POST.get('program', '').strip()
    is_available  = request.POST.get('is_available') == 'on'

    # Basic validation
    if not all([room_name, room_code, building_name, floor_number, program]):
        return JsonResponse({'success': False, 'error': 'Please fill in all required fields.'})

    if models.Room.objects.filter(room_code__iexact=room_code).exists():
        return JsonResponse({'success': False, 'error': f'Room code "{room_code}" already exists.'})

    try:
        room = models.Room(
            room_name=room_name,
            room_code=room_code,
            building_name=building_name,
            floor_number=int(floor_number),
            program=program,
            is_available=is_available,
        )
        room.full_clean()
        room.save()
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({
        'success': True,
        'room': {
            'id': room.id,
            'room_name': room.room_name,
            'room_code': room.room_code,
            'building_name': room.building_name,
            'floor_number': room.floor_number,
            'program': room.program,
            'program_display': room.get_program_display(),
            'is_available': room.is_available,
        },
    })

@require_POST
def update_class_list_signature(request):
    """
    Save a GLOBAL signature slot (applies to every section).
    Chairman only.
    """
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        action_key = (request.POST.get('action_key') or '').strip()
        name       = (request.POST.get('name') or '').strip()
        position   = (request.POST.get('position') or '').strip()

        if action_key not in dict(models.ClassListSignature.ACTION_KEYS):
            return JsonResponse({'error': 'Invalid action key'}, status=400)

        obj, _ = models.ClassListSignature.objects.update_or_create(
            action_key=action_key,
            defaults={'name': name, 'position': position},
        )

        return JsonResponse({
            'success': True,
            'action_key': obj.action_key,
            'name': obj.name,
            'position': obj.position,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

# ============================================================
# TERM / EFFECTIVE DATE / REMARKS / ACTIONS
# ============================================================
@require_POST
def update_term(request, employee_id):
    new_term = (request.POST.get('term') or '').strip()
    if not new_term:
        return JsonResponse({'success': False, 'error': 'Term is required'}, status=400)

    try:
        profile = models.FacultyProfile.objects.get(employee_id=employee_id)
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Faculty not found'}, status=404)

    current = models.FacultyProfile.objects.filter(
        employee_id=request.session.get('faculty_id')
    ).first()
    if not current or (not current.is_chairman and current.employee_id != employee_id):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    profile.term = new_term
    profile.save(update_fields=['term'])
    return JsonResponse({'success': True, 'term': new_term})


@require_POST
def update_effective_date(request, employee_id):
    new_date = (request.POST.get('effective_date') or '').strip()
    if not new_date:
        return JsonResponse({'success': False, 'error': 'Date is required'}, status=400)

    try:
        profile = models.FacultyProfile.objects.get(employee_id=employee_id)
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Faculty not found'}, status=404)

    # Check permission: chairman OR the owner themselves
    current = models.FacultyProfile.objects.filter(
        employee_id=request.session.get('faculty_id')
    ).first()
    if not current or (not current.is_chairman and current.employee_id != employee_id):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    profile.effective_date = new_date
    profile.save(update_fields=['effective_date'])
    return JsonResponse({'success': True, 'effective_date': new_date})



@require_POST
def update_remarks(request, employee_id):
    """
    Save remark fields (overload_subjects, adviser_notes) for a faculty member.
    Allowed: chairman OR the faculty themselves.
    """
    if not request.session.get('faculty_id'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=401)

    allowed = {'overload_hours', 'overload_subjects', 'service_credits', 'adviser_notes'}
    updated = {f: (request.POST.get(f) or '').strip()
               for f in allowed if f in request.POST}

    if not updated:
        return JsonResponse(
            {'success': False, 'error': 'No valid fields'},
            status=400
        )

    try:
        profile = models.FacultyProfile.objects.get(employee_id=employee_id)
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse(
            {'success': False, 'error': 'Faculty not found'},
            status=404
        )

    # Permission: chairman OR the owner themselves (same rule as update_term)
    current = models.FacultyProfile.objects.filter(
        employee_id=request.session.get('faculty_id')
    ).first()
    if not current or (not current.is_chairman and current.employee_id != employee_id):
        return JsonResponse(
            {'success': False, 'error': 'Permission denied'},
            status=403
        )

    for k, v in updated.items():
        setattr(profile, k, v)
    profile.save(update_fields=list(updated.keys()))

    return JsonResponse({
        'success': True,
        'updated': updated,
    })


# NOTE: only ONE definition of update_action_taken now (was defined twice before).
@require_POST
def update_action_taken(request, employee_id):
    """
    Save a single (action_key -> {name, position}) entry.
    Name and position are free-form strings — no FK to FacultyProfile.
    """
    key      = (request.POST.get('action_key') or '').strip()
    name     = (request.POST.get('name') or '').strip()
    position = (request.POST.get('position') or '').strip()

    if key not in dict(models.ActionTaken.ACTION_KEYS):
        return JsonResponse({'success': False, 'error': 'Invalid action key'}, status=400)

    try:
        profile = models.FacultyProfile.objects.get(employee_id=employee_id)
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Faculty not found'}, status=404)

    current = models.FacultyProfile.objects.filter(
        employee_id=request.session.get('faculty_id')
    ).first()
    if not current or (not current.is_chairman and current.employee_id != employee_id):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    obj, _ = models.ActionTaken.objects.update_or_create(
        faculty=profile,
        action_key=key,
        defaults={'name': name, 'position': position},
    )
    return JsonResponse({
        'success': True,
        'action_key': key,
        'name': obj.name,
        'position': obj.position,
    })


# ============================================================
# CHANGE PASSWORD
# ============================================================
@require_POST
def change_password(request):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    current_password = request.POST.get('current_password', '')
    new_password = request.POST.get('new_password', '')

    if not current_password or not new_password:
        return JsonResponse({'error': 'Both current and new passwords are required.'}, status=400)

    if len(new_password) < 8:
        return JsonResponse({'error': 'New password must be at least 8 characters long.'}, status=400)

    try:
        faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )

        # Verify current password
        if not faculty.check_password(current_password):
            return JsonResponse({'error': 'Incorrect current password.'}, status=400)

        # Set and save the new password
        faculty.set_password(new_password)
        faculty.save(update_fields=['password'])

        return JsonResponse({'success': True})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty profile not found.'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================
# AUTH
# ============================================================

def login(request):
    if request.session.get('faculty_id'):
        # Only bounce a session that belongs to a live account; otherwise the
        # inactive/missing session would loop against dashboard.
        if models.FacultyProfile.objects.filter(
            employee_id=request.session['faculty_id'], is_active=True
        ).exists():
            return redirect('dashboard')
        request.session.flush()

    if request.method == 'POST':
        employee_id = request.POST.get('employee_id')
        password = request.POST.get('password')
        remember_me = request.POST.get('remember_me')

        if employee_id and password:
            faculty = models.FacultyProfile.authenticate(employee_id, password)
            if faculty:
                faculty.last_login = timezone.now()
                faculty.save()

                request.session['faculty_id'] = faculty.employee_id
                request.session['faculty_name'] = faculty.full_name
                request.session['is_chairman'] = faculty.is_chairman

                if not remember_me:
                    request.session.set_expiry(0)
                else:
                    request.session.set_expiry(1209600)

                messages.success(request, f'Welcome back, {faculty.full_name}!')
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid employee ID or password.')
        else:
            messages.warning(request, 'Please enter both employee ID and password.')

    return render(request, 'login.html')


def register(request):
    if request.session.get('faculty_id'):
        return redirect('dashboard')

    if not settings.ALLOW_PUBLIC_REGISTRATION:
        messages.info(
            request,
            'Self-registration is disabled. Please ask the chairman to create your account.',
        )
        return redirect('login')

    if request.method == 'POST':
        employee_id = request.POST.get('employee_id', '').strip()
        full_name = request.POST.get('full_name', '').strip()
        specialization = request.POST.get('specialization', '').strip()
        attained_degree = request.POST.get('attained_degree', '').strip()
        department = request.POST.get('department', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        has_error = False

        if not employee_id:
            messages.error(request, 'Employee ID is required.')
            has_error = True
        elif models.FacultyProfile.objects.filter(employee_id=employee_id).exists():
            messages.error(request, 'An account with this Employee ID already exists.')
            has_error = True

        if not full_name:
            messages.error(request, 'Full name is required.')
            has_error = True
        elif len(full_name) < 2:
            messages.error(request, 'Full name must be at least 2 characters long.')
            has_error = True

        if not specialization:
            messages.error(request, 'Specialization is required.')
            has_error = True

        if not attained_degree:
            messages.error(request, 'Attained degree is required.')
            has_error = True

        if not department:
            messages.error(request, 'Department is required.')
            has_error = True

        if not password:
            messages.error(request, 'Password is required.')
            has_error = True
        elif len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters long.')
            has_error = True

        if not confirm_password:
            messages.error(request, 'Please confirm your password.')
            has_error = True
        elif password and confirm_password and password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            has_error = True

        if has_error:
            return render(request, 'register.html', {
                'employee_id': employee_id,
                'full_name': full_name,
                'specialization': specialization,
                'attained_degree': attained_degree,
                'department': department,
            })

        try:
            faculty = models.FacultyProfile(
                employee_id=employee_id,
                full_name=full_name,
                specialization=specialization,
                attained_degree=attained_degree,
                department=department,
                employment_type='PART',
                is_chairman=False,
                is_active=True,
                last_login=timezone.now()
            )
            faculty.set_password(password)
            faculty.save()

            messages.success(request, 'Account created successfully! You can now login with your credentials.')
            return redirect('login')

        except IntegrityError:
            messages.error(request, 'An account with this Employee ID already exists.')
            return render(request, 'register.html', {
                'employee_id': employee_id,
                'full_name': full_name,
                'specialization': specialization,
                'attained_degree': attained_degree,
                'department': department,
            })
        except (ValidationError, IntegrityError, ValueError, TypeError) as e:
            messages.error(request, 'An error occurred while creating your account. Please try again.')
            return render(request, 'register.html', {
                'employee_id': employee_id,
                'full_name': full_name,
                'specialization': specialization,
                'attained_degree': attained_degree,
                'department': department,
            })

    return render(request, 'register.html')


def logout_view(request):
    request.session.flush()
    messages.success(request, 'You have been logged out successfully.')
    return redirect('login')


# ============================================================
# DASHBOARD
# ============================================================

def dashboard(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access the dashboard.')
        return redirect('login')

    faculty = None
    try:
        faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id'],
            is_active=True
        )

        subjects = _active_subjects().filter(
            faculty=faculty,
        ).order_by(
            'year_section__course_code',
            'year_section__year_level',
            'year_section__section',
        )

        today = datetime.now().strftime('%A').upper()
        today_schedule = models.ClassSchedule.objects.filter(
            offering__subject__faculty=faculty,
            offering__subject__is_active=True,
            day=today
        ).select_related('offering__subject', 'room').order_by('start_time')

        total_hours = sum(
            float(s.lecture_hours_per_week or 0) + float(s.effective_lab_hours_per_week() or 0)
            for s in subjects
        )
        total_subjects = subjects.count()
        today_classes = today_schedule.count()

        class_program_count = models.ClassSchedule.objects.filter(
            offering__subject__faculty=faculty,
            offering__subject__is_active=True,
        ).count()
        room_count = models.Room.objects.filter(is_available=True).count()

        context = {
            'faculty': faculty,
            'subjects': subjects,
            'today_schedule': today_schedule,
            'total_subjects': total_subjects,
            'total_hours': total_hours,
            'today_classes': today_classes,
            'subject_count': total_subjects,
            'class_program_count': class_program_count,
            'room_count': room_count,
        }

        if faculty.is_chairman:
            faculty_members = models.FacultyProfile.objects.filter(
                is_active=True
            ).order_by('full_name')

            all_subjects = _active_subjects()

            department_total_hours = sum(
                float(s.total_hours_per_week or 0) for s in all_subjects
            )

            context.update({
                'faculty_members': faculty_members,
                'total_faculty': faculty_members.count(),
                'total_subjects_all': all_subjects.count(),
                'department_total_hours': department_total_hours,
                'departments': models.FacultyProfile.objects.filter(
                    is_active=True
                ).values('department').distinct().count(),
            })
        else:
            context['department_faculty_count'] = models.FacultyProfile.objects.filter(
                is_active=True,
                department=faculty.department
            ).count()

        return render(request, 'dashboard.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        messages.error(request, f'Error loading dashboard: {e}')
        return render(request, 'dashboard.html', {'faculty': faculty})


# ============================================================
# CHAIRMAN-ONLY VIEWS
# ============================================================

def faculty_list(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        faculty_qs = models.FacultyProfile.objects.all().order_by('full_name')

        search_q = request.GET.get('q', '').strip()
        type_q   = request.GET.get('type', 'all').strip()

        if search_q:
            faculty_qs = faculty_qs.filter(
                Q(full_name__icontains=search_q) |
                Q(employee_id__icontains=search_q) |
                Q(department__icontains=search_q) |
                Q(specialization__icontains=search_q)
            )

        if type_q == 'chairman':
            faculty_qs = faculty_qs.filter(is_chairman=True)
        elif type_q == 'active':
            faculty_qs = faculty_qs.filter(is_active=True)
        elif type_q == 'inactive':
            faculty_qs = faculty_qs.filter(is_active=False)
        elif type_q == 'full':
            faculty_qs = faculty_qs.filter(employment_type='FULL')
        elif type_q == 'part':
            faculty_qs = faculty_qs.filter(employment_type='PART')
        elif type_q == 'contract':
            faculty_qs = faculty_qs.filter(employment_type='CONTRACT')

        per_page = 10
        paginator = Paginator(faculty_qs, per_page)
        page_number = request.GET.get('page', 1)

        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        params = request.GET.copy()
        params.pop('page', None)
        querystring = params.urlencode()

        context = {
            'faculty': faculty,
            'faculty_members': page_obj.object_list,
            'total_faculty': paginator.count,
            'page_obj': page_obj,
            'paginator': paginator,
            'querystring': querystring,
            'search_q': search_q,
            'type_q': type_q,
        }
        return render(request, 'faculty_list.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')


def faculty_update(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        member = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        full_name       = (request.POST.get('full_name') or '').strip()
        department      = (request.POST.get('department') or '').strip()
        specialization  = (request.POST.get('specialization') or '').strip()
        attained_degree = (request.POST.get('attained_degree') or '').strip()
        academic_rank   = (request.POST.get('academic_rank') or '').strip()
        employment_type = (request.POST.get('employment_type') or '').strip().upper()
        is_active       = request.POST.get('is_active') == '1'

        if not full_name:
            return JsonResponse({'error': 'Full name is required.'}, status=400)
        if not department:
            return JsonResponse({'error': 'Department is required.'}, status=400)
        if not specialization:
            return JsonResponse({'error': 'Specialization is required.'}, status=400)
        if employment_type not in ('FULL', 'PART', 'CONTRACT'):
            return JsonResponse({'error': 'Invalid employment type.'}, status=400)

        if member.is_chairman and not is_active:
            return JsonResponse({'error': 'The chairman account cannot be deactivated.'}, status=400)

        was_active = member.is_active

        member.full_name       = full_name
        member.department      = department
        member.specialization  = specialization
        member.attained_degree = attained_degree
        member.academic_rank   = academic_rank
        member.employment_type = employment_type
        member.is_active       = is_active
        member.full_clean()
        member.save()

        # Cascade: if active status flipped, sync their subjects.
        if was_active != is_active:
            models.Subject.objects.filter(faculty=member).update(is_active=is_active)

        return JsonResponse({'success': True})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def faculty_create(request):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        employee_id     = (request.POST.get('employee_id') or '').strip()
        full_name       = (request.POST.get('full_name') or '').strip()
        department      = (request.POST.get('department') or '').strip()
        specialization  = (request.POST.get('specialization') or '').strip()
        attained_degree = (request.POST.get('attained_degree') or '').strip()
        academic_rank   = (request.POST.get('academic_rank') or '').strip()
        employment_type = (request.POST.get('employment_type') or 'PART').strip().upper()
        password        = request.POST.get('password') or ''
        confirm_password= request.POST.get('confirm_password') or ''
        is_active       = request.POST.get('is_active') == '1'

        if not employee_id:
            return JsonResponse({'error': 'Employee ID is required.'}, status=400)

        if models.FacultyProfile.objects.filter(employee_id=employee_id).exists():
            return JsonResponse(
                {'error': f'An account with Employee ID "{employee_id}" already exists.'},
                status=400
            )

        if not full_name:
            return JsonResponse({'error': 'Full name is required.'}, status=400)

        if not department:
            return JsonResponse({'error': 'Department is required.'}, status=400)

        if not specialization:
            return JsonResponse({'error': 'Specialization is required.'}, status=400)

        if employment_type not in ('FULL', 'PART', 'CONTRACT'):
            return JsonResponse({'error': 'Invalid employment type.'}, status=400)

        if not password:
            return JsonResponse({'error': 'Password is required.'}, status=400)

        if len(password) < 8:
            return JsonResponse({'error': 'Password must be at least 8 characters.'}, status=400)

        if password != confirm_password:
            return JsonResponse({'error': 'Passwords do not match.'}, status=400)

        new_faculty = models.FacultyProfile(
            employee_id=employee_id,
            full_name=full_name,
            department=department,
            specialization=specialization,
            attained_degree=attained_degree,
            academic_rank=academic_rank,
            employment_type=employment_type,
            is_chairman=False,
            is_active=is_active,
            last_login=timezone.now(),
        )
        new_faculty.set_password(password)
        new_faculty.full_clean()
        new_faculty.save()

        return JsonResponse({
            'success': True,
            'employee_id': new_faculty.employee_id,
        })

    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except IntegrityError:
        return JsonResponse({'error': 'A faculty with this Employee ID already exists.'}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def faculty_toggle_status(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        member = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if member.is_chairman:
            return JsonResponse({'error': 'The chairman account cannot be deactivated.'}, status=400)

        member.is_active = not member.is_active
        member.save(update_fields=['is_active'])

        # ── CASCADE ────────────────────────────────────────────
        models.Subject.objects.filter(faculty=member).update(
            is_active=member.is_active
        )
        # ───────────────────────────────────────────────────────

        return JsonResponse({'success': True, 'is_active': member.is_active})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def faculty_delete(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        member = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if member.is_chairman:
            return JsonResponse({'error': 'The chairman account cannot be deleted.'}, status=400)

        if member.employee_id == current_faculty.employee_id:
            return JsonResponse({'error': 'You cannot delete your own account.'}, status=400)

        member.delete()
        return JsonResponse({'success': True})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def faculty_info_data(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        member = get_object_or_404(
            models.FacultyProfile, employee_id=employee_id
        )

        # ── Semester resolution (?semester= → current) ──
        all_semesters = (
            models.Semester.objects
            .select_related('academic_year')
            .order_by('-academic_year__year_label', 'semester')
        )
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            active_semester = all_semesters.first()

        # ── Subjects (semester-scoped when possible) ──
        if active_semester is not None:
            subjects_qs = (
                _active_subjects()
                .filter(
                    faculty=member,
                    offerings__semester=active_semester,
                    offerings__is_active=True,
                )
                .distinct()
                .order_by('subject_code')
            )
        else:
            subjects_qs = _active_subjects().filter(faculty=member).order_by('subject_code')

        subjects_data = []
        total_hours = 0.0
        fully_scheduled_count = 0
        partial_count = 0
        not_started_count = 0

        for s in subjects_qs:
            lec_used = models.ClassSchedule.hours_used(s, 'LECTURE', semester=active_semester)
            lab_used = models.ClassSchedule.hours_used(s, 'LAB',     semester=active_semester)

            lec_max = float(s.lecture_hours_per_week or 0)
            lab_max = s.raw_lab_hours_per_week

            lec_remaining = max(0.0, lec_max - lec_used)
            lab_remaining = max(0.0, lab_max - lab_used)

            lec_done = (not s.has_lecture) or (lec_remaining <= 0.0)
            lab_done = (not s.is_laboratory) or (lab_remaining <= 0.0)
            is_fully_scheduled = lec_done and lab_done

            if is_fully_scheduled:
                fully_scheduled_count += 1
            elif lec_used > 0 or lab_used > 0:
                partial_count += 1
            else:
                not_started_count += 1

            total_hours += lec_used + lab_used

            subjects_data.append({
                'subject_code': s.subject_code,
                'subject_description': s.subject_description,
                'section_display': s.get_subject_display(),
                'has_lecture': s.has_lecture,
                'is_laboratory': s.is_laboratory,
                'lec_max': round(lec_max, 1),
                'lab_max': round(lab_max, 1),
                'lec_used': round(lec_used, 1),
                'lab_used': round(lab_used, 1),
                'lec_remaining': round(lec_remaining, 1),
                'lab_remaining': round(lab_remaining, 1),
                'is_fully_scheduled': is_fully_scheduled,
            })

        total_sched_qs = models.ClassSchedule.objects.filter(
            offering__subject__faculty=member,
            offering__subject__is_active=True,
            offering__is_active=True,
        )
        if active_semester is not None:
            total_sched_qs = total_sched_qs.filter(offering__semester=active_semester)
        total_schedules = total_sched_qs.count()

        last_login = member.last_login.strftime('%b %d, %Y %I:%M %p') if member.last_login else 'Never'

        return JsonResponse({
            'full_name': member.full_name,
            'employee_id': member.employee_id,
            'department': member.department or '',
            'specialization': member.specialization or '',
            'attained_degree': getattr(member, 'attained_degree', '') or '',
            'academic_rank': member.academic_rank or '',
            'employment_type': member.get_employment_type_display() or '',
            'is_active': member.is_active,
            'is_chairman': member.is_chairman,
            'last_login': last_login,
            'semester_id': active_semester.id if active_semester else None,
            'term': str(active_semester) if active_semester else '',
            'total_subjects': subjects_qs.count(),
            'total_schedules': total_schedules,
            'total_hours': round(total_hours, 1),
            'fully_scheduled_count': fully_scheduled_count,
            'partial_count': partial_count,
            'not_started_count': not_started_count,
            'subjects': subjects_data,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def subject_create(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        faculty_list = models.FacultyProfile.objects.filter(is_active=True).order_by('full_name')
        year_section_list = models.YearSection.objects.all().order_by('course_code', 'year_level', 'section')
        all_semesters = (
            models.Semester.objects.select_related('academic_year')
            .annotate(
                offering_count=Count(
                    'offerings',
                    filter=Q(offerings__is_active=True, offerings__subject__is_active=True)
                )
            )
            .order_by('-academic_year__year_label', 'semester')
        )

        def render_all_subjects(form_data=None, editing=None):
            # Reuse the same semester-scoped list as all_subjects
            sem_choice = (form_data.get('semester') if form_data else None) or request.GET.get('semester')
            active_semester = all_semesters.filter(pk=sem_choice).first() if sem_choice else None
            if active_semester is None:
                active_semester = models.Semester.get_current() or all_semesters.first()

            subjects_qs = (
                models.Subject.objects
                .filter(
                    is_active=True,
                    faculty__is_active=True,
                    offerings__semester=active_semester,
                    offerings__is_active=True,
                )
                .select_related('year_section', 'faculty')
                .prefetch_related(
                    Prefetch(
                        'offerings',
                        queryset=models.SubjectOffering.objects
                            .filter(semester=active_semester, is_active=True)
                            .select_related('subject__faculty', 'semester__academic_year'),
                        to_attr='active_offerings',
                    )
                )
                .distinct()
            )

            paginator = Paginator(subjects_qs, 10)
            page_obj = paginator.get_page(1)
            subjects_page = list(page_obj.object_list)
            _annotate_subject_progress(subjects_page, semester=active_semester)
            for s in subjects_page:
                offering = getattr(s, 'active_offerings', None)
                s.offering = offering[0] if offering else None
                s.lecture_max = float(s.lecture_hours_per_week or 0)
                s.lab_max = s.raw_lab_hours_per_week
            page_obj.object_list = subjects_page

            return render(request, 'all_subjects.html', {
                'faculty': faculty,
                'faculty_list': faculty_list,
                'year_section_list': year_section_list,
                'all_semesters': all_semesters,
                'active_semester': active_semester,
                'subjects': subjects_page,
                'subject_count': paginator.count,
                'page_obj': page_obj,
                'paginator': paginator,
                'querystring': '',
                'form_data': form_data,
                'editing_subject': editing,
                'search_q': '',
                'year_q': '',
                'type_q': '',
            })

        if request.method == 'POST':
            semester_id         = (request.POST.get('semester') or '').strip()
            year_section_id     = request.POST.get('year_section')
            subject_code        = request.POST.get('subject_code', '').strip()
            subject_description = request.POST.get('subject_description', '').strip()
            lecture_hours       = float(request.POST.get('lecture_hours_per_week', 0) or 0)
            lab_hours           = float(request.POST.get('lab_hours_per_week', 0) or 0)
            faculty_id          = request.POST.get('faculty')
            has_lecture         = request.POST.get('has_lecture') == 'on'
            is_laboratory       = request.POST.get('is_laboratory') == 'on'

            has_error = False

            if not semester_id:
                messages.error(request, 'Semester is required.')
                has_error = True
            if not year_section_id:
                messages.error(request, 'Year & Section is required.')
                has_error = True
            if not subject_code:
                messages.error(request, 'Subject code is required.')
                has_error = True
            if not subject_description:
                messages.error(request, 'Subject description is required.')
                has_error = True
            if not faculty_id:
                messages.error(request, 'Please assign a faculty member.')
                has_error = True
            if lecture_hours == 0 and lab_hours == 0:
                messages.error(request, 'Total hours must be greater than 0.')
                has_error = True
            if not has_lecture and lecture_hours > 0:
                messages.error(request, 'Lecture hours must be 0 when "Has Lecture" is unchecked.')
                has_error = True
            if not is_laboratory and lab_hours > 0:
                messages.error(request, 'Lab hours must be 0 when "Is Laboratory" is unchecked.')
                has_error = True

            if has_error:
                return render_all_subjects(form_data=request.POST)

            try:
                semester         = models.Semester.objects.get(id=semester_id)
                assigned_faculty = models.FacultyProfile.objects.get(employee_id=faculty_id)
                year_section     = models.YearSection.objects.get(id=year_section_id)

                with transaction.atomic():
                    # 1. Check if the subject definition already exists for this year_section
                    subject = models.Subject.objects.filter(
                        year_section=year_section,
                        subject_code=subject_code,
                    ).first()

                    if subject:
                        subject.subject_description = subject_description
                        subject.lecture_hours_per_week = lecture_hours
                        subject.lab_hours_per_week = lab_hours
                        subject.faculty = assigned_faculty
                        subject.is_active = True
                        subject.save()
                    else:
                        subject = models.Subject(
                            year_section=year_section,
                            subject_code=subject_code,
                            subject_description=subject_description,
                            lecture_hours_per_week=lecture_hours,
                            lab_hours_per_week=lab_hours,
                            faculty=assigned_faculty,
                            is_active=True,
                        )
                        subject.save()

                    models.SubjectOffering.objects.update_or_create(
                        subject=subject,
                        semester=semester,
                        defaults={'is_active': True},
                    )

                    
                messages.success(
                    request,
                    f'Subject "{subject_code}" created for {semester} '
                    f'and assigned to {assigned_faculty.full_name}!'
                )
                return redirect(f"/all-subjects/?semester={semester.id}")
            
            except models.Semester.DoesNotExist:
                messages.error(request, 'Selected semester not found.')
            except models.FacultyProfile.DoesNotExist:
                messages.error(request, 'Selected faculty member not found.')
            except models.YearSection.DoesNotExist:
                messages.error(request, 'Selected Year & Section not found.')
            except ValidationError as e:
                messages.error(request, f'Validation error: {", ".join(e.messages)}')
            except IntegrityError as e:
                messages.error(request, f'A subject with these details already exists: {e}')
            except (ValidationError, IntegrityError, ValueError, TypeError) as e:
                messages.error(request, f'An error occurred: {str(e)}')

            return render_all_subjects(form_data=request.POST)

        # GET → just go to the list
        return redirect('all_subjects')

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')


def subject_edit(request, subject_id):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        subject = get_object_or_404(models.Subject, id=subject_id)
        faculty_list = models.FacultyProfile.objects.filter(is_active=True).order_by('full_name')
        year_section_list = models.YearSection.objects.all().order_by('course_code', 'year_level', 'section')
        all_semesters = (
            models.Semester.objects.select_related('academic_year')
            .annotate(
                offering_count=Count(
                    'offerings',
                    filter=Q(offerings__is_active=True, offerings__subject__is_active=True)
                )
            )
            .order_by('-academic_year__year_label', 'semester')
        )

        def render_all_subjects(form_data=None, editing=None):
            sem_choice = (form_data.get('semester') if form_data else None) or request.GET.get('semester')
            active_semester = all_semesters.filter(pk=sem_choice).first() if sem_choice else None
            if active_semester is None:
                active_semester = models.Semester.get_current() or all_semesters.first()

            subjects_qs = (
                models.Subject.objects
                .filter(
                    is_active=True,
                    faculty__is_active=True,
                    offerings__semester=active_semester,
                    offerings__is_active=True,
                )
                .select_related('year_section', 'faculty')
                .prefetch_related(
                    Prefetch(
                        'offerings',
                        queryset=models.SubjectOffering.objects
                            .filter(semester=active_semester, is_active=True)
                            .select_related('subject__faculty', 'semester__academic_year'),
                        to_attr='active_offerings',
                    )
                )
                .distinct()
            )

            paginator = Paginator(subjects_qs, 10)
            page_obj = paginator.get_page(1)
            subjects_page = list(page_obj.object_list)
            _annotate_subject_progress(subjects_page, semester=active_semester)
            for s in subjects_page:
                offering = getattr(s, 'active_offerings', None)
                s.offering = offering[0] if offering else None
                s.lecture_max = float(s.lecture_hours_per_week or 0)
                s.lab_max = s.raw_lab_hours_per_week
            page_obj.object_list = subjects_page

            return render(request, 'all_subjects.html', {
                'faculty': faculty,
                'faculty_list': faculty_list,
                'year_section_list': year_section_list,
                'all_semesters': all_semesters,
                'active_semester': active_semester,
                'subjects': subjects_page,
                'subject_count': paginator.count,
                'page_obj': page_obj,
                'paginator': paginator,
                'querystring': '',
                'form_data': form_data,
                'editing_subject': editing,
                'open_edit_modal': True,
                'search_q': '',
                'year_q': '',
                'type_q': '',
            })

        if request.method == 'POST':
            semester_id         = (request.POST.get('semester') or '').strip()
            year_section_id     = request.POST.get('year_section')
            subject_code        = request.POST.get('subject_code', '').strip()
            subject_description = request.POST.get('subject_description', '').strip()
            lecture_hours       = float(request.POST.get('lecture_hours_per_week', 0) or 0)
            lab_hours           = float(request.POST.get('lab_hours_per_week', 0) or 0)
            faculty_id          = request.POST.get('faculty')
            has_lecture         = request.POST.get('has_lecture') == 'on'
            is_laboratory       = request.POST.get('is_laboratory') == 'on'

            has_error = False

            if not semester_id:
                messages.error(request, 'Semester is required.')
                has_error = True
            if not year_section_id:
                messages.error(request, 'Year & Section is required.')
                has_error = True
            if not subject_code:
                messages.error(request, 'Subject code is required.')
                has_error = True
            if not subject_description:
                messages.error(request, 'Subject description is required.')
                has_error = True
            if not faculty_id:
                messages.error(request, 'Please assign a faculty member.')
                has_error = True
            if lecture_hours == 0 and lab_hours == 0:
                messages.error(request, 'Total hours must be greater than 0.')
                has_error = True
            if not has_lecture and lecture_hours > 0:
                messages.error(request, 'Lecture hours must be 0 when "Has Lecture" is unchecked.')
                has_error = True
            if not is_laboratory and lab_hours > 0:
                messages.error(request, 'Lab hours must be 0 when "Is Laboratory" is unchecked.')
                has_error = True

            if has_error:
                return render_all_subjects(form_data=request.POST, editing=subject)

            try:
                semester         = models.Semester.objects.get(id=semester_id)
                assigned_faculty = models.FacultyProfile.objects.get(employee_id=faculty_id)
                year_section     = models.YearSection.objects.get(id=year_section_id)

                duplicate = models.Subject.objects.filter(
                    year_section=year_section,
                    subject_code=subject_code,
                ).exclude(id=subject.id).exists()
                if duplicate:
                    messages.error(request, 'Another subject with these details already exists.')
                    return render_all_subjects(form_data=request.POST, editing=subject)

                with transaction.atomic():
                    # 1. Update the reusable Subject
                    subject.year_section         = year_section
                    subject.subject_code         = subject_code
                    subject.subject_description  = subject_description
                    subject.lecture_hours_per_week = lecture_hours
                    subject.lab_hours_per_week   = lab_hours
                    subject.faculty              = assigned_faculty
                    subject.save()

                    offering, _ = models.SubjectOffering.objects.update_or_create(
                        subject=subject,
                        semester=semester,
                        defaults={'is_active': True},
                    )

                messages.success(
                    request,
                    f'Subject "{subject_code}" updated for {semester}!'
                )
                return redirect(f"{reverse('all_subjects')}?semester={semester.id}")

            except models.Semester.DoesNotExist:
                messages.error(request, 'Selected semester not found.')
            except models.FacultyProfile.DoesNotExist:
                messages.error(request, 'Selected faculty member not found.')
            except models.YearSection.DoesNotExist:
                messages.error(request, 'Selected Year & Section not found.')
            except ValidationError as e:
                messages.error(request, f'Validation error: {", ".join(e.messages)}')
            except IntegrityError:
                messages.error(request, 'Another subject with these details already exists.')
            except (ValidationError, IntegrityError, ValueError, TypeError) as e:
                messages.error(request, f'An error occurred: {str(e)}')

            return render_all_subjects(form_data=request.POST, editing=subject)

        return redirect('all_subjects')

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

def room_create(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        if request.method == 'POST':
            room_name = request.POST.get('room_name', '').strip()
            room_code = request.POST.get('room_code', '').strip().upper()
            building_name = request.POST.get('building_name', '').strip()
            floor_number = request.POST.get('floor_number')
            is_available = request.POST.get('is_available') == 'on'
            program = request.POST.get('program', '').strip()

            has_error = False

            if not room_name:
                messages.error(request, 'Room name is required.')
                has_error = True
            if not room_code:
                messages.error(request, 'Room code is required.')
                has_error = True
            if not building_name:
                messages.error(request, 'Building name is required.')
                has_error = True
            if not floor_number:
                messages.error(request, 'Floor number is required.')
                has_error = True
            if not program:
                messages.error(request, 'Please select a program.')
                has_error = True

            if has_error:
                return render(request, 'room_create.html', {
                    'faculty': faculty,
                    'form_data': request.POST
                })

            try:
                room = models.Room(
                    room_name=room_name,
                    room_code=room_code,
                    building_name=building_name,
                    floor_number=int(floor_number),
                    is_available=is_available,
                    program=program,
                )
                room.full_clean()
                room.save()

                messages.success(request, f'Room "{room_code}" created successfully!')
                return redirect('room_list')

            except ValidationError as e:
                messages.error(request, f'Validation error: {", ".join(e.messages)}')
            except IntegrityError:
                messages.error(request, 'A room with this code already exists.')
            except (ValidationError, IntegrityError, ValueError, TypeError) as e:
                messages.error(request, f'An error occurred: {str(e)}')

            return render(request, 'room_create.html', {
                'faculty': faculty,
                'form_data': request.POST
            })

        return render(request, 'room_create.html', {
            'faculty': faculty,
            'form_data': {},
        })

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

# ============================================================
# TEACHERS LIST (chairman picks teacher to assign)
# ============================================================

def rooms_teachers_list(request):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        # ── Semester resolution (?semester= → current) ──
        all_semesters = (
            models.Semester.objects
            .select_related('academic_year')
            .order_by('-academic_year__year_label', 'semester')
        )
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            active_semester = all_semesters.first()

        # ── Primary: teachers with an active offering in this semester ──
        teachers_qs = models.FacultyProfile.objects.filter(is_active=True)
        if active_semester is not None:
            semester_teachers = teachers_qs.filter(
                subjects__offerings__semester=active_semester,
                subjects__offerings__is_active=True,
                subjects__is_active=True,
            ).distinct().order_by('full_name')

            if semester_teachers.exists():
                teachers_qs = semester_teachers
            else:
                # Fallback: don't leave the picker empty
                teachers_qs = teachers_qs.order_by('full_name')
        else:
            teachers_qs = teachers_qs.order_by('full_name')

        return JsonResponse({
            'teachers': [
                {
                    'id': t.employee_id,
                    'full_name': t.full_name,
                    'department': getattr(t, 'department', '') or '',
                }
                for t in teachers_qs
            ],
            'semester_id': active_semester.id if active_semester else None,
            'semester_scoped': active_semester is not None,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

# ============================================================
# ROOM SCHEDULE DATA  — CHAIRMAN ONLY
# ============================================================
def _get_room_signatures(room):
    """Room sheets share the global class-list signatures.

    RoomSignature was removed (migrations 0017/0019); this used to probe a
    model that no longer exists and swallow the AttributeError.
    """
    return _get_class_list_signatures()

def room_schedule_data(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)

        # ── Semester Filter Logic ──
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()

        try:
            # ✅ Pass the active semester to the matrix generator (if supported)
            # If your matrix function doesn't take a semester yet, we filter below
            matrix = models.RoomUtilizationCalculator.get_room_schedule_matrix(room, semester=active_semester)
        except TypeError:
            # Fallback if the function signature hasn't been updated
            matrix = models.RoomUtilizationCalculator.get_room_schedule_matrix(room)
        except Exception as e:
            return JsonResponse({'error': f'Matrix generation failed: {str(e)}'}, status=500)

        days = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY']

        valid_schedule_ids = set(
            models.ClassSchedule.objects.filter(
                offering__subject__is_active=True,
                offering__subject__faculty__is_active=True,
            ).values_list('id', flat=True)
        )
        schedules = []
        for time_key, day_map in matrix.items():
            for day, cell in day_map.items():
                if cell:
                    # ✅ Only include if the schedule belongs to the active semester
                    # (Matrix generator ideally handles this, but we double-check)
                    sid = cell.get('schedule_id')
                    if sid and sid not in valid_schedule_ids:
                        continue
                        
                    schedules.append({
                        'day': day,
                        'time': time_key,
                        'subject': cell['subject'],
                        'description': cell.get('subject_description', ''),
                        'section': cell.get('section', ''),
                        'faculty': cell.get('faculty', ''),
                        'faculty_id': cell.get('faculty_id', ''),
                        'subject_id': cell.get('subject_id', None),
                        'schedule_id': cell.get('schedule_id', None),
                        'schedule_type': cell.get('schedule_type', 'LECTURE'),
                        'start_time': cell.get('start_time', ''),
                        'end_time': cell.get('end_time', ''),
                    })

        unique = {}
        for s in schedules:
            if s.get('schedule_type') != 'OTHER':
                if s.get('schedule_id') not in valid_schedule_ids:
                    continue
            key = f"{s['day']}|{s['subject']}|{s['start_time']}|{s['end_time']}"
            if key not in unique:
                unique[key] = s
        schedule_list = list(unique.values())

        # ── Filter Other Assignments by Semester ──
        other_qs = models.FacultyOtherAssignment.objects.filter(
            room=room,
            is_active=True,
            faculty__is_active=True,
            day__isnull=False,
            start_time__isnull=False,
            end_time__isnull=False,
        ).select_related('faculty')

        if active_semester:
            other_qs = other_qs.filter(semester=active_semester)

        for a in other_qs:
            schedule_list.append({
                'day': a.day,
                'time': a.start_time.strftime('%H:%M'),
                'subject': a.get_work_type_display().upper(),
                'description': a.title,
                'section': '',
                'faculty': a.faculty.full_name,
                'faculty_id': a.faculty.employee_id,
                'subject_id': None,
                'schedule_id': None,
                'schedule_type': 'OTHER',
                'start_time': a.start_time.strftime('%H:%M'),
                'end_time': a.end_time.strftime('%H:%M'),
            })

        time_rows = []
        start = datetime.strptime('07:00', '%H:%M')
        end   = datetime.strptime('20:00', '%H:%M')
        current = start

        while current < end:
            time_str = current.strftime('%H:%M')
            if time_str == '12:00':
                time_rows.append({
                    'key': '12:00',
                    'display': '12:00 PM – 1:00 PM',
                    'is_lunch': True,
                })
                current = datetime.strptime('13:00', '%H:%M')
                continue

            next_time = current + timedelta(minutes=30)

            def fmt_time(dt):
                return dt.strftime('%I:%M %p').lstrip('0')

            time_rows.append({
                'key': time_str,
                'display': f"{fmt_time(current)} - {fmt_time(next_time)}",
                'is_lunch': False,
            })
            current = next_time

        target_faculty = None
        teacher_id_param = request.GET.get('teacher_id')
        if teacher_id_param:
            try:
                target_faculty = models.FacultyProfile.objects.get(
                    employee_id=teacher_id_param, is_active=True
                )
            except models.FacultyProfile.DoesNotExist:
                return JsonResponse({'error': 'Selected teacher not found.'}, status=404)

        only_unscheduled = request.GET.get('only_unscheduled') == '1'

        subjects_data = []
        if target_faculty:
            # ✅ Only fetch subjects that have an active offering in the selected semester
            if active_semester:
                subjects_qs = _active_subjects().filter(
                    faculty=target_faculty,
                    offerings__semester=active_semester,
                    offerings__is_active=True,
                ).distinct()
            else:
                subjects_qs = _active_subjects().filter(faculty=target_faculty)

            for s in subjects_qs:
                lecture_used = models.ClassSchedule.hours_used(s, 'LECTURE', semester=active_semester)
                lab_used     = models.ClassSchedule.hours_used(s, 'LAB', semester=active_semester)
                
                lecture_max = float(s.lecture_hours_per_week or 0)
                lab_max     = s.raw_lab_hours_per_week

                lecture_remaining = max(0.0, lecture_max - lecture_used)
                lab_remaining     = max(0.0, lab_max - lab_used)

                lecture_done = (not s.has_lecture) or (lecture_remaining <= 0.0)
                lab_done     = (not s.is_laboratory) or (lab_remaining <= 0.0)
                is_fully_scheduled = lecture_done and lab_done

                if only_unscheduled and is_fully_scheduled:
                    continue

                subjects_data.append({
                    'id': s.id,
                    'subject_code': s.subject_code,
                    'subject_description': s.subject_description,
                    'section_display': s.get_subject_display(),
                    'has_lecture': s.has_lecture,
                    'is_laboratory': s.is_laboratory,
                    'faculty': s.faculty.full_name,
                    'lecture_max': lecture_max,
                    'lab_max': lab_max,
                    'lecture_used': lecture_used,
                    'lab_used': lab_used,
                    'lecture_remaining': lecture_remaining,
                    'lab_remaining': lab_remaining,
                    'is_fully_scheduled': is_fully_scheduled,
                })

        signatures = _get_room_signatures(room)

        college = getattr(room, 'department_name', '') or room.building_name

        return JsonResponse({
            'room': {
                'id': room.id,
                'code': room.room_code,
                'name': room.room_name,
                'building': room.building_name,
                'floor': room.floor_number,
                'program': room.program,
            },
            'schedules': schedule_list,
            'time_rows': time_rows,
            'days': days,
            'subjects': subjects_data,
            'target_teacher': {
                'id': target_faculty.employee_id,
                'full_name': target_faculty.full_name,
            } if target_faculty else None,
            'can_edit': True,
            'term': str(active_semester) if active_semester else 'Current Semester',
            'college': college,
            'department': room.get_program_display().upper(),
            'signatures': signatures,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': f'Server error: {str(e)}'}, status=500)
    
def room_list(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        # ── Semester logic ──
        all_semesters = models.Semester.objects.select_related('academic_year').order_by('-academic_year__year_label', 'semester')
        semester_q = request.GET.get('semester', '').strip()
        
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current() or all_semesters.first()

        # ── Fetch Rooms & Annotate with Semester-specific schedule count ──
        if active_semester:
            rooms = models.Room.objects.annotate(
                sem_schedule_count=Count(
                    'schedules',
                    filter=Q(
                        schedules__offering__semester=active_semester,
                        schedules__offering__is_active=True,
                        schedules__offering__subject__is_active=True,
                        schedules__offering__subject__faculty__is_active=True
                    ),
                    distinct=True
                )
            ).order_by('building_name', 'room_name')
        else:
            rooms = models.Room.objects.annotate(
                sem_schedule_count=Count('schedules', distinct=True)
            ).order_by('building_name', 'room_name')

        import re
        clean_semesters = []
        for sem in all_semesters:
            raw_name = str(sem)
            # Strips out the year (e.g., "2026-2027") and "S.Y."
            short_name = re.sub(r'(S\.Y\.|SY)\s*\d{4}[-\s]?\d{4}', '', raw_name, flags=re.IGNORECASE)
            short_name = re.sub(r'\d{4}[-\s]?\d{4}', '', short_name).strip(', ')
            
            clean_semesters.append({
                'id': sem.id,
                'short_name': short_name
            })

        context = {
            'faculty': faculty,
            'rooms': rooms,
            'room_count': rooms.count(),
            'programs': models.Room.PROGRAM_CHOICES,
            'clean_semesters': clean_semesters,
            'active_semester': active_semester,
            'semester_q': semester_q,
        }
        return render(request, 'room_list.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

# ============================================================
# ALL SUBJECTS — with server-side pagination + filters
# ============================================================

# views.py — all_subjects
def all_subjects(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

    # ── Semester selection ──
    # We fetch all semesters and use the model's built-in display method
    raw_semesters = (
        models.Semester.objects
        .select_related('academic_year')
        .annotate(
            offering_count=Count(
                'offerings',
                filter=Q(offerings__is_active=True, offerings__subject__is_active=True)
            )
        )
        .order_by('-academic_year__year_label', 'semester')
    )

    all_semesters = []
    clean_semesters = []
    seen_labels = set()

    for s in raw_semesters:
        # Your model uses '1ST' and '2ND'. This will return 'First Semester' or 'Second Semester'.
        raw_label = s.get_semester_display()
        
        # Only keep First and Second (ignores Summer, Midyear, etc. if added later)
        label_lower = str(raw_label).lower()
        if 'first' in label_lower:
            clean_label = 'First Semester'
        elif 'second' in label_lower:
            clean_label = 'Second Semester'
        else:
            continue

        all_semesters.append(s)

        # Keep only the most recent semester per label (avoids duplicates from old academic years)
        if clean_label in seen_labels:
            continue
        seen_labels.add(clean_label)
        clean_semesters.append({'id': s.id, 'label': clean_label})

    # ── Determine active semester ──
    semester_id = request.GET.get('semester', '').strip()
    active_semester = None

    if semester_id:
        active_semester = next((s for s in all_semesters if str(s.id) == semester_id), None)

    if active_semester is None:
        # Try to get the current semester using your model's built-in method
        current = models.Semester.get_current()
        if current and current in all_semesters:
            active_semester = current

    if active_semester is None and all_semesters:
        active_semester = all_semesters[0]

    # ── Subject queryset ──
    subjects_qs = models.Subject.objects.filter(
        is_active=True,
        faculty__is_active=True,
    )
    
    if active_semester:
        subjects_qs = subjects_qs.filter(
            offerings__semester=active_semester,
            offerings__is_active=True,
        )
    
    subjects_qs = (
        subjects_qs
        .select_related('year_section', 'faculty')
        .prefetch_related(
            Prefetch(
                'offerings',
                queryset=models.SubjectOffering.objects
                    .filter(semester=active_semester, is_active=True)
                    .select_related('subject__faculty', 'semester__academic_year'),
                to_attr='active_offerings',
            )
        )
        .distinct()
    )

    # ── Filters ──
    search_q = request.GET.get('q', '').strip()
    year_q   = request.GET.get('year', '').strip()
    type_q   = request.GET.get('type', '').strip()

    if search_q:
        subjects_qs = subjects_qs.filter(
            Q(subject_code__icontains=search_q) |
            Q(subject_description__icontains=search_q) |
            Q(faculty__full_name__icontains=search_q) |
            Q(year_section__course_code__icontains=search_q)
        )
    if year_q in ('1', '2', '3', '4'):
        subjects_qs = subjects_qs.filter(year_section__year_level=int(year_q))

    if type_q == 'lecture':
        subjects_qs = subjects_qs.filter(lecture_hours_per_week__gt=0, lab_hours_per_week=0)
    elif type_q == 'lab':
        subjects_qs = subjects_qs.filter(lecture_hours_per_week=0, lab_hours_per_week__gt=0)
    elif type_q == 'both':
        subjects_qs = subjects_qs.filter(lecture_hours_per_week__gt=0, lab_hours_per_week__gt=0)

    # ── Annotate progress ──
    subjects_list = _annotate_subject_progress(list(subjects_qs), semester=active_semester)

    for s in subjects_list:
        offering = getattr(s, 'active_offerings', None)
        s.offering = offering[0] if offering else None
        s.lecture_max = float(s.lecture_hours_per_week or 0)
        s.lab_max = s.raw_lab_hours_per_week

    # ── Pagination ──
    paginator = Paginator(subjects_list, 10)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    params = request.GET.copy()
    params.pop('page', None)
    querystring = params.urlencode()

    context = {
        'faculty': faculty,
        'subjects': page_obj.object_list,
        'subject_count': paginator.count,
        'page_obj': page_obj,
        'paginator': paginator,
        'querystring': querystring,
        'search_q': search_q,
        'year_q': year_q,
        'type_q': type_q,
        'year_section_list': list(models.YearSection.objects.all().order_by('course_code', 'year_level', 'section')),
        'faculty_list': list(models.FacultyProfile.objects.filter(is_active=True)),
        'all_semesters': all_semesters,
        'clean_semesters': clean_semesters,
        'active_semester': active_semester,
    }
    return render(request, 'all_subjects.html', context)

def my_subjects(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])

        # ✅ Fixed: order by year_section FK fields + select_related for major
        subjects = _active_subjects().filter(
            faculty=faculty,
        ).select_related('year_section').order_by(
            'year_section__course_code',
            'year_section__year_level',
            'year_section__section',
        )

        total_hours = sum(
            float(s.lecture_hours_per_week or 0) + float(s.effective_lab_hours_per_week() or 0)
            for s in subjects
        )

        scheduled_count = 0
        for s in subjects:
            if models.ClassSchedule.objects.filter(offering__subject=s).exists():
                scheduled_count += 1

        context = {
            'faculty': faculty,
            'subjects': subjects,
            'subject_count': subjects.count(),
            'total_hours': total_hours,
            'scheduled_count': scheduled_count,
        }
        return render(request, 'my_subjects.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

# ============================================================
# CLASS PROGRAM VIEWS
# ============================================================
def class_program_view(request, employee_id):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )

        if not current_faculty.is_chairman and current_faculty.employee_id != employee_id:
            messages.error(
                request,
                'You do not have permission to view this faculty member\'s class program.'
            )
            return redirect('dashboard')

        viewing_faculty = get_object_or_404(
            models.FacultyProfile, employee_id=employee_id, is_active=True
        )

        # ── Semester resolution (?semester= → current) ──
        # ✅ ROBUST FIX: don't hardcode semester__in=[1, 2] here.
        # The Semester.semester field may be an IntegerField (1/2) OR a
        # CharField ('FIRST'/'SECOND'). Hardcoding 1/2 silently empties the
        # queryset in the latter case, which hides the dropdown in the
        # template (it's wrapped in {% if all_semesters %}).
        #
        # Strategy:
        #   1. Fetch ALL semesters (annotated with offering_count).
        #   2. If the field is integer-like, keep only 1/2 (exclude summer 3).
        #   3. If it's not integer-like, keep everything (let get_current pick).
        # The dropdown will render as long as at least one row remains.
        all_semesters_qs = (
            models.Semester.objects
            .select_related('academic_year')
            .annotate(
                offering_count=Count(
                    'offerings',
                    filter=Q(offerings__is_active=True, offerings__subject__is_active=True)
                )
            )
            .order_by('-academic_year__year_label', 'semester')
        )

        # Peek at the field type by sampling distinct values.
        sample_values = list(
            models.Semester.objects
            .values_list('semester', flat=True)
            .distinct()[:5]
        )
        if sample_values and all(isinstance(v, int) for v in sample_values):
            # Integer field: keep only First (1) and Second (2), drop Summer (3).
            all_semesters = all_semesters_qs.filter(semester__in=[1, 2])
        else:
            # CharField / other: keep every semester that is not obviously summer.
            # (No hardcoded 1/2 — we don't know the encoding.)
            all_semesters = all_semesters_qs

        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
            # If get_current() returns a semester we excluded above (e.g. Summer),
            # fall back to the first visible one so ?semester= highlighting is sane.
            if active_semester is not None and not all_semesters.filter(pk=active_semester.pk).exists():
                active_semester = None
        if active_semester is None:
            active_semester = all_semesters.first()

        # ── Grid + workload (shared builder — one source of truth) ──
        payload = services.build_class_program_payload(
            viewing_faculty, active_semester
        )

        schedules = payload['schedules']
        days = services.DAYS
        time_slots = []
        for slot in payload['time_slots']:
            slot = dict(slot)
            for day in days:
                slot[day.lower()] = slot['schedules'].get(day)
            time_slots.append(slot)

        faculty_subjects = payload['faculty_subjects']
        workload_rows = payload['workload_rows']
        total_lec = payload['total_lec']
        total_lab = payload['total_lab']
        total_lab_raw = payload['total_lab_raw']
        total_weighted = payload['total_weighted']
        total_students = payload['total_students']

        # ── Distinct sections / preparations ─────────────────────
        distinct_sections = len({
            (s.year_section.course_code, s.year_section.year_level, s.year_section.section)
            for s in faculty_subjects
            if s.year_section_id
        })

        no_preparations = payload['no_preparations']


        rooms = models.Room.objects.filter(
            is_available=True
        ).order_by('building_name', 'room_code')

        # ── Other Functions & Designations ───────────────────────
        other_assignments_list = payload['other_assignments']
        other_assignments_other = payload['other_assignments_other']
        other_assignments_desig = payload['other_assignments_desig']

        other_hours_total = payload['other_hours_total']
        desig_hours_total = payload['desig_hours_total']

        grand_total_hours = payload['grand_total_hours']

        can_edit_other = (
            current_faculty.is_chairman
            or current_faculty.employee_id == viewing_faculty.employee_id
        )

        actions = payload['actions']

        if active_semester is not None:
            term_display = str(active_semester)
        else:
            term_display = viewing_faculty.term or 'First Semester 2026-2027'

        context = {
            'faculty': current_faculty,
            'viewing_faculty': viewing_faculty,
            'schedules': schedules,
            'schedule_count': len(schedules),
            'time_slots': time_slots,
            'days': days,
            'faculty_subjects': faculty_subjects,
            'rooms': rooms,
            'can_edit': current_faculty.is_chairman,

            'term':           term_display,
            'effective_date': viewing_faculty.effective_date or 'Aug. 3, 2026',
            'college':        'College of Teacher Education',
            'actions':        actions,
            'can_edit_actions': can_edit_other,

            'grand_total_hours': round(grand_total_hours, 1),
            'overload_hours': viewing_faculty.overload_hours,
            'overload_subjects': viewing_faculty.overload_subjects,
            'service_credits': viewing_faculty.service_credits,
            'adviser_notes': viewing_faculty.adviser_notes,

            'workload_rows': workload_rows,
            'total_lec': round(total_lec, 1),
            'total_lab': round(total_lab, 1),
            'total_lab_raw': round(total_lab_raw, 1),
            'total_weighted': round(total_weighted, 1),
            'total_students': total_students,
            'no_preparations': no_preparations,
            'total_teaching_hours': round(total_lec + total_lab, 1),
            'distinct_sections': distinct_sections,

            'other_assignments': other_assignments_list,
            'other_assignments_other': other_assignments_other,
            'other_assignments_desig': other_assignments_desig,
            'can_edit_other': can_edit_other,

            'other_hours_total': round(other_hours_total, 1),
            'desig_hours_total': round(desig_hours_total, 1),

            'all_semesters': all_semesters,
            'active_semester': active_semester,
            'semester_id': active_semester.id if active_semester else None,
        }
        return render(request, 'class_program.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')
    
# ============================================================
# CLASS SCHEDULE ASSIGNMENT (AJAX)  — Chairman only
# ============================================================

def get_faculty_subjects(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        # ── Semester resolution (?semester= → current) ──
        all_semesters = (
            models.Semester.objects
            .select_related('academic_year')
            .order_by('-academic_year__year_label', 'semester')
        )
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            active_semester = all_semesters.first()

        # ── Subjects: scoped to semester when possible ──
        if active_semester is not None:
            subjects_qs = _active_subjects().filter(
                faculty__employee_id=employee_id,
                offerings__semester=active_semester,
                offerings__is_active=True,
            ).distinct()
        else:
            subjects_qs = _active_subjects().filter(faculty__employee_id=employee_id)

        data = []
        for s in subjects_qs:
            lecture_remaining = max(
                0.0,
                float(s.lecture_hours_per_week or 0)
                - models.ClassSchedule.hours_used(s, 'LECTURE', semester=active_semester)
            )
            lab_max = s.raw_lab_hours_per_week
            lab_remaining = max(
                0.0,
                lab_max - models.ClassSchedule.hours_used(s, 'LAB', semester=active_semester)
            )
            data.append({
                'id': s.id,
                'subject_code': s.subject_code,
                'subject_description': s.subject_description,
                'section_display': s.get_subject_display(),
                'has_lecture': s.has_lecture,
                'is_laboratory': s.is_laboratory,
                'lecture_remaining': round(lecture_remaining, 1),
                'lab_remaining': round(lab_remaining, 1),
            })

        rooms = models.Room.objects.filter(is_available=True).values(
            'id', 'room_code', 'room_name'
        )
        return JsonResponse({
            'subjects': data,
            'rooms': list(rooms),
            'semester_id': active_semester.id if active_semester else None,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)

def assign_schedule(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        viewing_faculty = get_object_or_404(
            models.FacultyProfile, employee_id=employee_id, is_active=True
        )

        subject_id     = request.POST.get('subject_id')
        room_id        = request.POST.get('room_id')
        day            = request.POST.get('day', '').upper()
        start_time_str = request.POST.get('start_time')
        end_time_str   = request.POST.get('end_time')
        schedule_type  = request.POST.get('schedule_type', 'LECTURE').upper()
        schedule_id    = request.POST.get('schedule_id')

        if not subject_id:
            return JsonResponse({'error': 'Please select a subject.'}, status=400)
        if not room_id:
            return JsonResponse({'error': 'Please select a room.'}, status=400)
        if not day:
            return JsonResponse({'error': 'Day is required.'}, status=400)
        if not start_time_str or not end_time_str:
            return JsonResponse({'error': 'Start and end time are required.'}, status=400)
        if schedule_type not in ('LECTURE', 'LAB'):
            return JsonResponse({'error': 'Invalid schedule type.'}, status=400)

        # ── Semester resolution (?semester= → current) ──
        semester_q = request.POST.get('semester', '').strip() or request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        subject = get_object_or_404(models.Subject, id=subject_id, is_active=True)
        room    = get_object_or_404(models.Room, id=room_id)

        if subject.faculty_id != viewing_faculty.employee_id:
            return JsonResponse(
                {'error': (
                    f'Subject {subject.subject_code} does not belong to '
                    f'{viewing_faculty.full_name}.'
                )},
                status=400
            )

        start_time = datetime.strptime(start_time_str, '%H:%M').time()
        end_time   = datetime.strptime(end_time_str, '%H:%M').time()
        if start_time >= end_time:
            return JsonResponse({'error': 'End time must be after start time.'}, status=400)

        if schedule_type == 'LECTURE' and not subject.has_lecture:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no lecture component.'},
                status=400
            )
        if schedule_type == 'LAB' and not subject.is_laboratory:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no laboratory component.'},
                status=400
            )

        exclude_pk = schedule_id or None
        max_hours = float(
            subject.lab_hours_per_week if schedule_type == 'LAB'
            else subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            return JsonResponse({
                'error': (
                    f'{subject.subject_code} has no '
                    f'{schedule_type.lower()} hours allocated.'
                )
            }, status=400)

        already_used = models.ClassSchedule.hours_used(
            subject, schedule_type, exclude_pk=exclude_pk, semester=active_semester
        )
        this_hours = (
            (end_time.hour + end_time.minute / 60) -
            (start_time.hour + start_time.minute / 60)
        )
        if already_used + this_hours > max_hours:
            over = already_used + this_hours - max_hours
            return JsonResponse({
                'error': (
                    f'Exceeds {schedule_type.lower()} hour limit for '
                    f'{subject.subject_code}. '
                    f'Allowed: {max_hours}h, Already used: {already_used}h, '
                    f'This schedule: {this_hours}h (over by {over}h).'
                )
            }, status=400)

        offering = _resolve_offering(subject, semester=active_semester, create=True)
        if not offering:
            return JsonResponse(
                {'error': f'No offering found for subject {subject.subject_code}.'},
                status=400
            )

        if schedule_id:
            schedule = get_object_or_404(models.ClassSchedule, id=schedule_id)

            # ── Refuse to move a schedule across semesters ──
            if schedule.offering_id and active_semester is not None \
                    and schedule.offering.semester_id != active_semester.id:
                return JsonResponse({
                    'error': (
                        f'This schedule belongs to a different semester '
                        f'({schedule.offering.semester}). Switch to that semester to edit it.'
                    )
                }, status=400)

            schedule.offering      = offering
            schedule.room          = room
            schedule.day           = day
            schedule.start_time    = start_time
            schedule.end_time      = end_time
            schedule.schedule_type = schedule_type
            schedule.full_clean()
            schedule.save()
            created = False
        else:
            schedule = models.ClassSchedule(
                offering=offering,
                room=room,
                day=day,
                start_time=start_time,
                end_time=end_time,
                schedule_type=schedule_type,
                created_by=current_faculty,
            )
            schedule.full_clean()
            schedule.save()
            created = True

        return JsonResponse({
            'success': True,
            'created': created,
            'schedule': {
                'id': schedule.id,
                'subject_code': subject.subject_code,
                'subject_description': subject.subject_description,
                'section_display': subject.get_subject_display(),
                'room': room.room_code,
                'day': schedule.day,
                'start_time': schedule.start_time.strftime('%I:%M %p'),
                'end_time': schedule.end_time.strftime('%I:%M %p'),
                'schedule_type': schedule.schedule_type,
            }
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def bulk_assign_schedule(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        viewing_faculty = get_object_or_404(
            models.FacultyProfile, employee_id=employee_id, is_active=True
        )

        subject_id    = request.POST.get('subject_id')
        room_id       = request.POST.get('room_id')
        schedule_type = request.POST.get('schedule_type', 'LECTURE').upper()
        slots_raw     = request.POST.get('slots', '[]')

        try:
            slots = json.loads(slots_raw)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid slots data.'}, status=400)

        if not slots:
            return JsonResponse({'error': 'No cells selected.'}, status=400)
        if not subject_id:
            return JsonResponse({'error': 'Please select a subject.'}, status=400)
        if not room_id:
            return JsonResponse({'error': 'Please select a room.'}, status=400)
        if schedule_type not in ('LECTURE', 'LAB'):
            return JsonResponse({'error': 'Invalid schedule type.'}, status=400)

        subject = get_object_or_404(models.Subject, id=subject_id, is_active=True)
        room    = get_object_or_404(models.Room, id=room_id)

        if subject.faculty_id != viewing_faculty.employee_id:
            return JsonResponse(
                {'error': (
                    f'Subject {subject.subject_code} does not belong to '
                    f'{viewing_faculty.full_name}.'
                )},
                status=400
            )

        if schedule_type == 'LECTURE' and not subject.has_lecture:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no lecture component.'},
                status=400
            )
        if schedule_type == 'LAB' and not subject.is_laboratory:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no laboratory component.'},
                status=400
            )

                # ── Semester resolution (?semester= / POST semester → current) ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)
            
        # ── Hour budget ──────────────────────────────────────
        requested_hours = 0.0
        for slot in slots:
            s_str = slot.get('start_time')
            e_str = slot.get('end_time')
            if not s_str or not e_str:
                continue
            try:
                st = datetime.strptime(s_str, '%H:%M')
                en = datetime.strptime(e_str, '%H:%M')
            except ValueError:
                continue
            requested_hours += max(0, (en - st).total_seconds() / 3600)

        max_hours = float(
            subject.lab_hours_per_week if schedule_type == 'LAB'
            else subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            return JsonResponse({
                'error': (
                    f'{subject.subject_code} has no '
                    f'{schedule_type.lower()} hours allocated.'
                )
            }, status=400)

        already_used = models.ClassSchedule.hours_used(
            subject, schedule_type, semester=active_semester
        )
        sids = [s.get('schedule_id') for s in slots if s.get('schedule_id')]
        if sids:
            for old in models.ClassSchedule.objects.filter(id__in=sids).select_related('offering'):
                if active_semester is None or (old.offering_id and old.offering.semester_id == active_semester.id):
                    already_used -= old.duration_hours()
                if old:
                    # Only subtract if this schedule lives in the same semester
                    if active_semester is None or (
                        old.offering_id and old.offering.semester_id == active_semester.id
                    ):
                        already_used -= old.duration_hours()
        already_used = max(0.0, already_used)

        if already_used + requested_hours > max_hours:
            over = already_used + requested_hours - max_hours
            return JsonResponse({
                'error': (
                    f'⚠️ {subject.subject_code} is over its '
                    f'{schedule_type.lower()} hour limit. '
                    f'Allowed: {max_hours}h per week · '
                    f'Currently scheduled: {already_used}h · '
                    f'Requested: {requested_hours}h · '
                    f'Over by: {over}h.'
                )
            }, status=400)

        # ── Group and merge contiguous slots per day ─────────
        grouped = {}
        for slot in slots:
            day = (slot.get('day') or '').upper()
            if not day or not slot.get('start_time') or not slot.get('end_time'):
                continue
            grouped.setdefault(day, []).append(slot)

        merged_slots = []
        for day, day_slots in grouped.items():
            day_slots.sort(key=lambda s: s['start_time'])
            current_block = None
            for s in day_slots:
                if current_block is None:
                    current_block = {
                        'day': day,
                        'start_time': s['start_time'],
                        'end_time': s['end_time'],
                        'schedule_id': s.get('schedule_id'),
                    }
                elif s['start_time'] == current_block['end_time']:
                    current_block['end_time'] = s['end_time']
                    if not current_block.get('schedule_id') and s.get('schedule_id'):
                        current_block['schedule_id'] = s.get('schedule_id')
                else:
                    merged_slots.append(current_block)
                    current_block = {
                        'day': day,
                        'start_time': s['start_time'],
                        'end_time': s['end_time'],
                        'schedule_id': s.get('schedule_id'),
                    }
            if current_block:
                merged_slots.append(current_block)

        offering = _resolve_offering(subject, semester=active_semester, create=True)
        if not offering:
            return JsonResponse(
                {'error': f'No offering found for subject {subject.subject_code}.'},
                status=400
            )

        created_count = 0
        updated_count = 0
        errors = []

        try:
            with transaction.atomic():
                for slot in merged_slots:
                    day         = slot['day']
                    start_str   = slot['start_time']
                    end_str     = slot['end_time']
                    schedule_id = slot.get('schedule_id')

                    try:
                        start_time = datetime.strptime(start_str, '%H:%M').time()
                        end_time   = datetime.strptime(end_str, '%H:%M').time()
                    except ValueError:
                        raise ValidationError(
                            f"{day.title()} {start_str}–{end_str}: invalid time"
                        )

                    if start_time >= end_time:
                        raise ValidationError(
                            f"{day.title()} {start_str}–{end_str}: end time must be after start time"
                        )

                    if schedule_id:
                        schedule = models.ClassSchedule.objects.filter(
                            id=schedule_id
                        ).first()
                        if not schedule:
                            raise ValidationError(
                                f"{day.title()} {start_str}–{end_str}: schedule {schedule_id} not found"
                            )
                        # ── Refuse to move a schedule across semesters ──
                        if schedule.offering_id and active_semester is not None \
                                and schedule.offering.semester_id != active_semester.id:
                            raise ValidationError(
                                f"{day.title()} {start_str}–{end_str}: schedule belongs to a "
                                f"different semester ({schedule.offering.semester})."
                            )
                        schedule.offering      = offering
                        schedule.room          = room
                        schedule.day           = day
                        schedule.start_time    = start_time
                        schedule.end_time      = end_time
                        schedule.schedule_type = schedule_type
                        schedule.full_clean()
                        schedule.save()
                        updated_count += 1
                    else:
                        schedule = models.ClassSchedule(
                            offering=offering,
                            room=room,
                            day=day,
                            start_time=start_time,
                            end_time=end_time,
                            schedule_type=schedule_type,
                            created_by=current_faculty,
                        )
                        schedule.full_clean()
                        schedule.save()
                        created_count += 1
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return JsonResponse({'success': False, 'error': msg}, status=400)
        except (ValidationError, IntegrityError, ValueError, TypeError) as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

        return JsonResponse({
            'success': True,
            'created': created_count,
            'updated': updated_count,
            'errors': errors,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

def delete_schedule(request, employee_id, schedule_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        schedule = get_object_or_404(
            models.ClassSchedule.objects.select_related(
                'offering__subject__faculty'
            ),
            id=schedule_id,
        )

        actual_faculty = (
            schedule.offering.subject.faculty
            if schedule.offering and schedule.offering.subject
            else None
        )
        if not actual_faculty or actual_faculty.employee_id != employee_id:
            return JsonResponse(
                {'error': 'Schedule does not belong to this faculty.'},
                status=400
            )

        schedule.delete()
        return JsonResponse({'success': True})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def all_class_programs(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        # ── Semester resolution (?semester= → current) ──
        all_semesters = (
            models.Semester.objects
            .select_related('academic_year')
            .order_by('-academic_year__year_label', 'semester')
        )
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current() or all_semesters.first()

        # ── Prefetch subjects scoped to the active semester ──
        subj_prefetch_qs = models.Subject.objects.filter(
            is_active=True,
            faculty__is_active=True,
        )
        if active_semester is not None:
            subj_prefetch_qs = subj_prefetch_qs.filter(
                offerings__semester=active_semester,
                offerings__is_active=True,
            ).distinct()

        faculty_qs = (
            models.FacultyProfile.objects
            .filter(is_active=True)
            .prefetch_related(Prefetch('subjects', queryset=subj_prefetch_qs))
            .order_by('full_name')
        )

        search_q = request.GET.get('q', '').strip()
        dept_q   = request.GET.get('dept', '').strip()

        if search_q:
            faculty_qs = faculty_qs.filter(
                Q(full_name__icontains=search_q) |
                Q(employee_id__icontains=search_q) |
                Q(department__icontains=search_q)
            )

        if dept_q and dept_q.lower() != 'all':
            faculty_qs = faculty_qs.filter(department__iexact=dept_q)

        per_page = 10
        paginator = Paginator(faculty_qs, per_page)
        page_number = request.GET.get('page', 1)

        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        faculty_page = list(page_obj.object_list)
        total_classes = 0

        for member in faculty_page:
            # ── schedule_count scoped to semester ──
            sched_qs = models.ClassSchedule.objects.filter(
                offering__subject__faculty=member,
                offering__subject__is_active=True,
                offering__is_active=True,
            )
            if active_semester is not None:
                sched_qs = sched_qs.filter(offering__semester=active_semester)
            member.schedule_count = sched_qs.count()
            total_classes += member.schedule_count

            # Prefetched above, already semester-scoped → no extra query
            member.student_total = sum(
                s.total_students for s in member.subjects.all()
            )

        page_obj.object_list = faculty_page

        departments = models.FacultyProfile.objects.filter(
            is_active=True
        ).values_list('department', flat=True).distinct().order_by('department')

        params = request.GET.copy()
        params.pop('page', None)
        querystring = params.urlencode()

        # ── clean semesters for dropdown ──
        import re
        clean_semesters = []
        for sem in all_semesters:
            raw_name = str(sem)
            short_name = re.sub(r'(S\.Y\.|SY)\s*\d{4}[-\s]?\d{4}', '', raw_name, flags=re.IGNORECASE)
            short_name = re.sub(r'\d{4}[-\s]?\d{4}', '', short_name).strip(', ')
            clean_semesters.append({'id': sem.id, 'short_name': short_name})

        context = {
            'faculty': faculty,
            'faculty_members': faculty_page,
            'total_classes': total_classes,
            'departments': departments,
            'page_obj': page_obj,
            'paginator': paginator,
            'querystring': querystring,
            'search_q': search_q,
            'dept_q': dept_q,
            'clean_semesters': clean_semesters,
            'active_semester': active_semester,
            'semester_q': semester_q,
        }
        return render(request, 'all_class_programs.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')


def profile(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        return render(request, 'profile.html', {'faculty': faculty})

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')


def class_program_data(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )

        if not current_faculty.is_chairman and current_faculty.employee_id != employee_id:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        viewing_faculty = get_object_or_404(
            models.FacultyProfile, employee_id=employee_id, is_active=True
        )

        # ── Semester resolution (?semester= → current → none) ──
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()

        # ── Grid + workload (shared builder — one source of truth) ──
        payload = services.build_class_program_payload(
            viewing_faculty, active_semester
        )

        schedules = payload['schedules']
        time_slots = payload['time_slots']
        workload_rows = payload['workload_rows']
        total_lec = payload['total_lec']
        total_lab = payload['total_lab']
        total_lab_raw = payload['total_lab_raw']
        total_weighted = payload['total_weighted']
        total_students = payload['total_students']
        no_preparations = payload['no_preparations']
        other_data = payload['other_data']
        desig_data = payload['desig_data']
        other_hours_total = payload['other_hours_total']
        desig_hours_total = payload['desig_hours_total']
        grand_total_hours = payload['grand_total_hours']
        actions = payload['actions']
        schedule_count = len(schedules)


        return JsonResponse({
            'faculty_name': viewing_faculty.full_name,
            'employee_id': viewing_faculty.employee_id,
            'department': viewing_faculty.department,
            'academic_rank': viewing_faculty.academic_rank or '',
            'employment_type': viewing_faculty.get_employment_type_display() or 'Instructor I',

            'term': str(active_semester) if active_semester else (viewing_faculty.term or 'First Semester 2026-2027'),
            'semester_id': active_semester.id if active_semester else None,
            'effective_date': viewing_faculty.effective_date or 'Aug. 3, 2026',
            'college': 'College of Teacher Education',
            'actions': actions,
            'can_edit_actions': (
                current_faculty.is_chairman
                or current_faculty.employee_id == viewing_faculty.employee_id
            ),

            'schedule_count': schedule_count,

            'overload_hours': viewing_faculty.overload_hours or '',
            'overload_subjects': viewing_faculty.overload_subjects or '',
            'service_credits': viewing_faculty.service_credits or '',
            'adviser_notes': viewing_faculty.adviser_notes or '',

            'time_slots': time_slots,

            'total_lec': round(total_lec, 1),
            'total_lab': round(total_lab, 1),
            'total_lab_raw': round(total_lab_raw, 1),
            'total_weighted': round(total_weighted, 1),

            'workload_rows': workload_rows,
            'total_students': total_students,
            'no_preparations': no_preparations,
            'total_teaching_hours': round(total_lec + total_lab, 1),

            'other_assignments_other': other_data,
            'other_assignments_desig': desig_data,
            'other_hours_total': round(other_hours_total, 1),
            'desig_hours_total': round(desig_hours_total, 1),
            'grand_total_hours': round(grand_total_hours, 1),

            'can_edit_other': (
                current_faculty.is_chairman
                or current_faculty.employee_id == viewing_faculty.employee_id
            ),
        })
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        import traceback
        traceback.print_exc() # This will print the exact line to your terminal if it crashes again
        return JsonResponse({'error': str(e)}, status=500)
    
def utilization(request):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        rooms = models.Room.objects.all().order_by('building_name', 'room_name')

        # ── Semester Filter Logic ──
        all_semesters = (
            models.Semester.objects
            .select_related('academic_year')
            .order_by('-academic_year__year_label', 'semester')
        )
        semester_q = request.GET.get('semester', '').strip()

        active_semester = None
        if semester_q:
            active_semester = all_semesters.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current() or all_semesters.first()

        import re
        clean_semesters = []
        for sem in all_semesters:
            raw_name = str(sem)
            short_name = re.sub(r'(S\.Y\.|SY)\s*\d{4}[-\s]?\d{4}', '', raw_name, flags=re.IGNORECASE)
            short_name = re.sub(r'\d{4}[-\s]?\d{4}', '', short_name).strip(', ')
            clean_semesters.append({'id': sem.id, 'short_name': short_name})

        # ── Shared capacity (Mon–Sat, matches RoomUtilizationCalculator) ──
        capacity = models.RoomUtilizationCalculator._weekly_capacity()   # 66h

        # ─────────────────────────────────────────────────────
        # 1. LIST VIEW DATA
        # ─────────────────────────────────────────────────────
        room_stats = []
        total_used = 0.0
        total_available = 0.0

        for room in rooms:
            schedules = models.ClassSchedule.objects.filter(
                room=room,
                offering__subject__is_active=True,
                offering__subject__faculty__is_active=True,
            ).exclude(day='SUNDAY')
            if active_semester is not None:
                schedules = schedules.filter(offering__semester=active_semester)

            used_hours = 0.0
            for schedule in schedules:
                used_hours += (
                    (schedule.end_time.hour + schedule.end_time.minute / 60) -
                    (schedule.start_time.hour + schedule.start_time.minute / 60)
                )

            available = capacity
            pct = (used_hours / available) * 100 if available else 0

            total_used += used_hours
            total_available += available

            room_stats.append({
                'room': room,
                'room_code': room.room_code,
                'room_name': room.room_name,
                'building': room.building_name,
                'floor': room.floor_number,
                'program': room.program,
                'program_display': room.get_program_display(),
                'schedule_count': schedules.count(),
                'used_hours': round(used_hours, 1),
                'available_hours': available,
                'utilization': round(pct, 1),
                'status': 'High' if pct >= 70 else 'Medium' if pct >= 40 else 'Low',
            })

        overall_pct = (total_used / total_available) * 100 if total_available else 0

        # ─────────────────────────────────────────────────────
        # 2. WEEKLY OVERVIEW DATA
        # ─────────────────────────────────────────────────────
        DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY']
        START = 7 * 60
        END = 19 * 60

        def fmt(minutes):
            h24 = minutes // 60
            m = minutes % 60
            suffix = 'AM' if h24 < 12 else 'PM'
            h12 = h24 % 12 or 12
            return f"{h12}:{m:02d} {suffix}"

        time_rows = []
        t = START
        while t < END:
            if t == 12 * 60:
                time_rows.append({'key': '12:00', 'display': '12:00 PM – 1:00 PM', 'is_lunch': True})
                t = 13 * 60
                continue
            time_rows.append({
                'key': f"{t // 60:02d}:{t % 60:02d}",
                'display': f"{fmt(t)} – {fmt(t + 30)}",
                'is_lunch': False,
            })
            t += 30

        week_schedules = (
            models.ClassSchedule.objects
            .select_related('offering__subject__faculty', 'room')
            .filter(
                room__is_available=True,
                offering__subject__is_active=True,
                offering__subject__faculty__is_active=True,
            )
            .exclude(day='SUNDAY')
            .order_by('room__room_name', 'day', 'start_time')
        )
        if active_semester is not None:
            week_schedules = week_schedules.filter(offering__semester=active_semester)

        palette = [
            'color-0', 'color-1', 'color-2', 'color-3', 'color-4',
            'color-5', 'color-6', 'color-7', 'color-8', 'color-9',
        ]
        subject_colors = {}
        ci = 0
        for s in week_schedules:
            subj = s.offering.subject if s.offering else None
            code = subj.subject_code if subj else None
            if code and code not in subject_colors:
                subject_colors[code] = palette[ci % len(palette)]
                ci += 1

        index = {}
        for s in week_schedules:
            day = (s.day or '').upper()
            if day not in DAYS:
                continue
            start_min = s.start_time.hour * 60 + s.start_time.minute
            end_min = s.end_time.hour * 60 + s.end_time.minute
            span = max(1, (end_min - start_min) // 30)
            if start_min < START or start_min >= END:
                continue
            start_key = f"{start_min // 60:02d}:{start_min % 60:02d}"
            subj = s.offering.subject if s.offering else None
            fac = subj.faculty if subj else None
            index[(s.room_id, day, start_key)] = {
                'schedule_id': s.id,
                'span': span,
                'subject': subj.subject_code if subj else '',
                'description': subj.subject_description if subj else '',
                # Section only (course/year-section/major) — the code is
                # already printed on the line above, so including it again
                # duplicated the subject code inside every grid cell.
                'section': subj.section_display if subj else '',
                'faculty': fac.full_name if fac else '',
                'schedule_type': (s.schedule_type or 'LECTURE').upper(),
                'color': subject_colors.get(subj.subject_code if subj else '', 'color-0'),
            }

        room_grids = []
        for r in rooms.filter(is_available=True):
            day_columns = {}
            schedule_count = 0
            for d in DAYS:
                col = []
                skip_until = -1
                for i, row in enumerate(time_rows):
                    if row['is_lunch']:
                        col.append({'type': 'lunch'})
                        continue
                    if i <= skip_until:
                        col.append({'type': 'skip'})
                        continue
                    blk = index.get((r.id, d, row['key']))
                    if blk:
                        skip_until = i + blk['span'] - 1
                        col.append({'type': 'sched', 'block': blk, 'span': blk['span']})
                        schedule_count += 1
                    else:
                        col.append({'type': 'empty'})
                day_columns[d] = col

            grid_rows = []
            for i, row in enumerate(time_rows):
                cells = [day_columns[d][i] for d in DAYS]
                grid_rows.append({'time': row, 'cells': cells})

            room_grids.append({
                'room': r,
                'rows': grid_rows,
                'schedule_count': schedule_count,
            })

        context = {
            'faculty': faculty,
            'room_stats': room_stats,
            'total_rooms': rooms.count(),
            'total_used': round(total_used, 1),
            'total_available': round(total_available, 1),
            'overall_pct': round(overall_pct, 1),
            'days': DAYS,
            'time_rows': time_rows,
            'room_grids': room_grids,
            'subject_colors': subject_colors,
            'clean_semesters': clean_semesters,
            'active_semester': active_semester,
            'semester_q': semester_q,
        }
        return render(request, 'utilization.html', context)

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')
    

def assign_room_schedule(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)

        subject_id    = request.POST.get('subject_id')
        day           = request.POST.get('day', '').upper()
        start_str     = request.POST.get('start_time')
        end_str       = request.POST.get('end_time')
        schedule_type = request.POST.get('schedule_type', 'LECTURE').upper()
        schedule_id   = request.POST.get('schedule_id')

        if not subject_id or not day or not start_str or not end_str:
            return JsonResponse({'error': 'Please fill in all fields.'}, status=400)
        if schedule_type not in ('LECTURE', 'LAB'):
            return JsonResponse({'error': 'Invalid schedule type.'}, status=400)

        # ── Semester resolution ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        subject = get_object_or_404(models.Subject, id=subject_id, is_active=True)

        start_time = datetime.strptime(start_str, '%H:%M').time()
        end_time   = datetime.strptime(end_str, '%H:%M').time()

        if start_time >= end_time:
            return JsonResponse({'error': 'End time must be after start time.'}, status=400)

        if schedule_type == 'LECTURE' and not subject.has_lecture:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no lecture component.'},
                status=400
            )
        if schedule_type == 'LAB' and not subject.is_laboratory:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no laboratory component.'},
                status=400
            )

        max_hours = float(
            subject.lab_hours_per_week if schedule_type == 'LAB'
            else subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            return JsonResponse({
                'error': (
                    f'{subject.subject_code} has no '
                    f'{schedule_type.lower()} hours allocated.'
                )
            }, status=400)

        exclude_pk = schedule_id or None
        already_used = models.ClassSchedule.hours_used(
            subject, schedule_type, exclude_pk=exclude_pk, semester=active_semester
        )
        this_hours = (
            (end_time.hour + end_time.minute / 60) -
            (start_time.hour + start_time.minute / 60)
        )
        if already_used + this_hours > max_hours:
            over = already_used + this_hours - max_hours
            return JsonResponse({
                'error': (
                    f'Exceeds {schedule_type.lower()} hour limit for '
                    f'{subject.subject_code}. '
                    f'Allowed: {max_hours}h, Used: {already_used}h, '
                    f'This: {this_hours}h (over by {over}h).'
                )
            }, status=400)

        offering = _resolve_offering(subject, semester=active_semester, create=True)
        if not offering:
            return JsonResponse(
                {'error': f'No offering found for subject {subject.subject_code}.'},
                status=400
            )

        if schedule_id:
            schedule = get_object_or_404(models.ClassSchedule, id=schedule_id)
            if schedule.offering_id and active_semester is not None \
                    and schedule.offering.semester_id != active_semester.id:
                return JsonResponse({
                    'error': (
                        f'This schedule belongs to a different semester '
                        f'({schedule.offering.semester}). Switch to that semester to edit it.'
                    )
                }, status=400)
        else:
            schedule = models.ClassSchedule()
            schedule.created_by = current_faculty

        schedule.offering      = offering
        schedule.room          = room
        schedule.day           = day
        schedule.start_time    = start_time
        schedule.end_time      = end_time
        schedule.schedule_type = schedule_type
        schedule.full_clean()
        schedule.save()

        return JsonResponse({'success': True, 'id': schedule.id})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def assign_room_bulk(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)

        subject_id = request.POST.get('subject_id')
        schedule_type = request.POST.get('schedule_type', 'LECTURE').upper()
        slots_raw = request.POST.get('slots', '[]')
        teacher_id = request.POST.get('teacher_id')

        try:
            slots = json.loads(slots_raw)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid slots data.'}, status=400)

        if not slots:
            return JsonResponse({'error': 'No cells selected.'}, status=400)
        if not subject_id:
            return JsonResponse({'error': 'Please select a subject.'}, status=400)
        if schedule_type not in ('LECTURE', 'LAB'):
            return JsonResponse({'error': 'Invalid schedule type.'}, status=400)

        # ── Semester ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        subject = get_object_or_404(models.Subject, id=subject_id, is_active=True)

        if teacher_id:
            target_faculty = get_object_or_404(
                models.FacultyProfile, employee_id=teacher_id, is_active=True
            )
        else:
            target_faculty = subject.faculty

        if subject.faculty_id != target_faculty.employee_id:
            return JsonResponse({
                'error': f'Subject {subject.subject_code} does not belong to '
                         f'{target_faculty.full_name}.'
            }, status=400)

        if schedule_type == 'LECTURE' and not subject.has_lecture:
            return JsonResponse({'error': f'{subject.subject_code} has no lecture component.'}, status=400)
        if schedule_type == 'LAB' and not subject.is_laboratory:
            return JsonResponse({'error': f'{subject.subject_code} has no laboratory component.'}, status=400)

        max_hours = float(
            subject.lab_hours_per_week if schedule_type == 'LAB'
            else subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            return JsonResponse({
                'error': f'{subject.subject_code} has no {schedule_type.lower()} hours allocated.'
            }, status=400)

        grouped = {}
        for slot in slots:
            day = (slot.get('day') or '').upper()
            if not day or not slot.get('start_time') or not slot.get('end_time'):
                continue
            grouped.setdefault(day, []).append(slot)

        gaps = []
        for day, day_slots in grouped.items():
            day_slots.sort(key=lambda s: s['start_time'])
            for i in range(1, len(day_slots)):
                prev_end = day_slots[i - 1]['end_time']
                curr_start = day_slots[i]['start_time']
                if curr_start != prev_end:
                    gaps.append(
                        f"{day.title()}: gap between "
                        f"{day_slots[i - 1]['start_time']}–{prev_end} "
                        f"and {curr_start}–{day_slots[i]['end_time']}"
                    )

        if gaps:
            return JsonResponse({
                'error': (
                    "❌ Selected cells must be contiguous (no gaps). "
                    "Nothing was saved.\n"
                    + "\n".join("• " + g for g in gaps)
                ),
                'errors': gaps,
            }, status=400)

        merged_slots = []
        for day, day_slots in grouped.items():
            day_slots.sort(key=lambda s: s['start_time'])
            merged_slots.append({
                'day': day,
                'start_time': day_slots[0]['start_time'],
                'end_time': day_slots[-1]['end_time'],
            })

        requested_hours = 0.0
        for block in merged_slots:
            st = datetime.strptime(block['start_time'], '%H:%M')
            en = datetime.strptime(block['end_time'], '%H:%M')
            requested_hours += (en - st).total_seconds() / 3600

        already_used = models.ClassSchedule.hours_used(
            subject, schedule_type, semester=active_semester
        )
        if already_used + requested_hours > max_hours:
            over = already_used + requested_hours - max_hours
            return JsonResponse({
                'error': (
                    f'⚠️ {subject.subject_code} is over its {schedule_type.lower()} hour limit. '
                    f'Allowed: {max_hours}h per week · '
                    f'Currently scheduled: {already_used}h · '
                    f'Requested: {requested_hours}h · '
                    f'Over by: {over}h. Nothing was saved.'
                )
            }, status=400)

        offering = _resolve_offering(subject, semester=active_semester, create=True)
        if not offering:
            return JsonResponse(
                {'error': f'No offering found for subject {subject.subject_code}.'},
                status=400
            )

        created_count = 0

        try:
            with transaction.atomic():
                for block in merged_slots:
                    day = block['day']
                    start_str = block['start_time']
                    end_str = block['end_time']

                    try:
                        st = datetime.strptime(start_str, '%H:%M').time()
                        en = datetime.strptime(end_str, '%H:%M').time()
                    except ValueError:
                        raise ValidationError(
                            f"{day.title()} {start_str}–{end_str}: invalid time"
                        )

                    if st >= en:
                        raise ValidationError(
                            f"{day.title()} {start_str}–{end_str}: end time must be after start time"
                        )

                    # ✅ No `faculty=` — derived from offering.subject.faculty
                    schedule = models.ClassSchedule(
                        offering=offering,
                        room=room,
                        day=day,
                        start_time=st,
                        end_time=en,
                        schedule_type=schedule_type,
                        created_by=current_faculty,
                    )
                    schedule.full_clean()
                    schedule.save()
                    created_count += 1

        except ValidationError as e:
            msg = " | ".join(e.messages) if getattr(e, 'messages', None) else str(e)
            return JsonResponse({
                'success': False,
                'error': f"❌ Nothing was saved: {msg}",
                'errors': [msg],
            }, status=400)
        except (ValidationError, IntegrityError, ValueError, TypeError) as e:
            return JsonResponse({
                'success': False,
                'error': f"❌ Nothing was saved: {str(e)}",
                'errors': [str(e)],
            }, status=500)

        return JsonResponse({
            'success': True,
            'created': created_count,
            'errors': [],
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def room_edit(request, room_id):
    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(employee_id=request.session['faculty_id'])
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('dashboard')

        room = get_object_or_404(models.Room, id=room_id)

        if request.method == 'POST':
            room_name = request.POST.get('room_name', '').strip()
            room_code = request.POST.get('room_code', '').strip().upper()
            building_name = request.POST.get('building_name', '').strip()
            floor_number = request.POST.get('floor_number')
            is_available = request.POST.get('is_available') == 'on'
            program = request.POST.get('program', '').strip()

            has_error = False

            if not room_name:
                messages.error(request, 'Room name is required.')
                has_error = True
            if not room_code:
                messages.error(request, 'Room code is required.')
                has_error = True
            if not building_name:
                messages.error(request, 'Building name is required.')
                has_error = True
            if not floor_number:
                messages.error(request, 'Floor number is required.')
                has_error = True
            if not program:
                messages.error(request, 'Program is required.')
                has_error = True

            if room_code:
                duplicate = models.Room.objects.filter(
                    room_code=room_code
                ).exclude(id=room.id).exists()
                if duplicate:
                    messages.error(request, f'A room with code "{room_code}" already exists.')
                    has_error = True

            if has_error:
                return render(request, 'room_edit.html', {
                    'faculty': faculty,
                    'room': room,
                    'form_data': request.POST,
                })

            try:
                room.room_name = room_name
                room.room_code = room_code
                room.building_name = building_name
                room.floor_number = int(floor_number)
                room.is_available = is_available
                room.program = program
                room.full_clean()
                room.save()

                messages.success(request, f'Room "{room_code}" updated successfully!')
                return redirect('room_list')

            except ValidationError as e:
                messages.error(request, f'Validation error: {", ".join(e.messages)}')
            except IntegrityError:
                messages.error(request, 'A room with this code already exists.')
            except (ValidationError, IntegrityError, ValueError, TypeError) as e:
                messages.error(request, f'An error occurred: {str(e)}')

            return render(request, 'room_edit.html', {
                'faculty': faculty,
                'room': room,
                'form_data': request.POST,
            })

        return redirect('room_list')

    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')


@require_POST
def room_toggle_availability(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)

        was_available = room.is_available
        room.is_available = not room.is_available
        room.save(update_fields=['is_available'])

        # ── CASCADE: deschedule everything in this room ─────────
        descheduled_count = 0
        if was_available and not room.is_available:
            affected = models.ClassSchedule.objects.filter(room=room)
            descheduled_count = affected.count()
            affected.delete()
        # ────────────────────────────────────────────────────────

        return JsonResponse({
            'success': True,
            'id': room.id,
            'is_available': room.is_available,
            'descheduled_count': descheduled_count,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

def room_delete(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)
        
        room_code = room.room_code
        room.delete()

        return JsonResponse({
            'success': True,
            'message': f'Room {room_code} deleted successfully.'
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def room_update_ajax(request, room_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        room = get_object_or_404(models.Room, id=room_id)

        room_name     = (request.POST.get('room_name') or '').strip()
        room_code     = (request.POST.get('room_code') or '').strip().upper()
        building_name = (request.POST.get('building_name') or '').strip()
        floor_number  = request.POST.get('floor_number')
        is_available  = request.POST.get('is_available') == 'on'
        program       = (request.POST.get('program') or '').strip()

        if not room_name:
            return JsonResponse({'error': 'Room name is required.'}, status=400)
        if not room_code:
            return JsonResponse({'error': 'Room code is required.'}, status=400)
        if not building_name:
            return JsonResponse({'error': 'Building name is required.'}, status=400)
        if not floor_number:
            return JsonResponse({'error': 'Floor number is required.'}, status=400)
        if not program:
            return JsonResponse({'error': 'Program is required.'}, status=400)

        if models.Room.objects.filter(room_code=room_code).exclude(id=room.id).exists():
            return JsonResponse({'error': f'A room with code "{room_code}" already exists.'}, status=400)

        was_available = room.is_available

        room.room_name     = room_name
        room.room_code     = room_code
        room.building_name = building_name
        room.floor_number  = int(floor_number)
        room.is_available  = is_available
        room.program       = program
        room.full_clean()
        room.save()

        # ── CASCADE: deschedule everything in this room ─────────
        descheduled_count = 0
        if was_available and not room.is_available:
            affected = models.ClassSchedule.objects.filter(room=room)
            descheduled_count = affected.count()
            affected.delete()
        # ────────────────────────────────────────────────────────

        return JsonResponse({
            'success': True,
            'descheduled_count': descheduled_count,
            'room': {
                'id': room.id,
                'room_name': room.room_name,
                'room_code': room.room_code,
                'building_name': room.building_name,
                'floor_number': room.floor_number,
                'is_available': room.is_available,
                'program': room.program,
                'program_display': room.get_program_display(),
            }
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

# ============================================================
# ROOM SCHEDULE — CHAIRMAN EDIT (reassign / delete)
# ============================================================

def room_schedule_update(request, schedule_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        schedule = get_object_or_404(models.ClassSchedule, id=schedule_id)

        subject_id = request.POST.get('subject_id')
        teacher_id = request.POST.get('teacher_id')
        schedule_type = request.POST.get('schedule_type', 'LECTURE').upper()

        if not subject_id:
            return JsonResponse({'error': 'Subject is required.'}, status=400)
        if not teacher_id:
            return JsonResponse({'error': 'Teacher is required.'}, status=400)
        if schedule_type not in ('LECTURE', 'LAB'):
            return JsonResponse({'error': 'Invalid schedule type.'}, status=400)

        # ── Semester ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None and schedule.offering_id:
            active_semester = schedule.offering.semester
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        subject = get_object_or_404(models.Subject, id=subject_id, is_active=True)
        teacher = get_object_or_404(
            models.FacultyProfile, employee_id=teacher_id, is_active=True
        )

        if subject.faculty_id != teacher.employee_id:
            return JsonResponse({
                'error': (
                    f'Subject {subject.subject_code} does not belong to '
                    f'{teacher.full_name}.'
                )
            }, status=400)

        if schedule_type == 'LECTURE' and not subject.has_lecture:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no lecture component.'},
                status=400
            )
        if schedule_type == 'LAB' and not subject.is_laboratory:
            return JsonResponse(
                {'error': f'{subject.subject_code} has no laboratory component.'},
                status=400
            )

        max_hours = float(
            subject.lab_hours_per_week if schedule_type == 'LAB'
            else subject.lecture_hours_per_week
        ) or 0
        if max_hours <= 0:
            return JsonResponse({
                'error': (
                    f'{subject.subject_code} has no '
                    f'{schedule_type.lower()} hours allocated.'
                )
            }, status=400)

        already_used = models.ClassSchedule.hours_used(
            subject, schedule_type, exclude_pk=schedule.pk, semester=active_semester
        )
        this_hours = schedule.duration_hours()
        if already_used + this_hours > max_hours:
            over = already_used + this_hours - max_hours
            return JsonResponse({
                'error': (
                    f'Exceeds {schedule_type.lower()} hour limit for '
                    f'{subject.subject_code}. '
                    f'Allowed: {max_hours}h, Used: {already_used}h, '
                    f'This: {this_hours}h (over by {over}h).'
                )
            }, status=400)

        offering = schedule.offering or _resolve_offering(subject, semester=active_semester, create=True)
        if not offering:
            return JsonResponse(
                {'error': f'No offering found for subject {subject.subject_code}.'},
                status=400
            )

        if schedule.offering_id and active_semester is not None \
                and schedule.offering.semester_id != active_semester.id:
            return JsonResponse({
                'error': (
                    f'This schedule belongs to a different semester '
                    f'({schedule.offering.semester}). Switch to that semester to edit it.'
                )
            }, status=400)

        # ✅ No `schedule.faculty = teacher` — derived from offering.subject.faculty
        schedule.offering = offering
        schedule.schedule_type = schedule_type
        schedule.full_clean()
        schedule.save()

        return JsonResponse({
            'success': True,
            'schedule': {
                'id': schedule.id,
                'subject_code': subject.subject_code,
                'subject_description': subject.subject_description,
                'section_display': subject.get_subject_display(),
                'faculty': subject.faculty.full_name if subject.faculty else '',
                'schedule_type': schedule.schedule_type,
            }
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except ValidationError as e:
        msg = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def room_schedule_delete(request, schedule_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current_faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        if not current_faculty.is_chairman:
            return JsonResponse({'error': 'Permission denied'}, status=403)

        schedule = get_object_or_404(models.ClassSchedule, id=schedule_id)
        schedule.delete()
        return JsonResponse({'success': True})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

# ============================================================
# CLASS LIST — printable schedule per faculty / section
# ============================================================

def class_list(request):
    import re

    if not request.session.get('faculty_id'):
        messages.error(request, 'Please login to access this page.')
        return redirect('login')

    try:
        faculty = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
    except models.FacultyProfile.DoesNotExist:
        messages.error(request, 'Faculty profile not found.')
        return redirect('login')

    # ── Filters ──────────────────────────────────────────────
    faculty_q = request.GET.get('faculty', '').strip()
    section_q = request.GET.get('section', '').strip()
    semester_q = request.GET.get('semester', '').strip()
    course_q = request.GET.get('course', '').strip()
    major_q = request.GET.get('major', '').strip()
    year_q = request.GET.get('year', '').strip()
    sec_name_q = request.GET.get('sec_name', '').strip()

    # ── Target faculty ───────────────────────────────────────
    if faculty_q:
        if not faculty.is_chairman:
            messages.error(request, 'You do not have permission to view other faculty.')
            return redirect('class_list')
        try:
            target = models.FacultyProfile.objects.get(employee_id=faculty_q, is_active=True)
        except models.FacultyProfile.DoesNotExist:
            messages.error(request, 'Selected faculty not found.')
            return redirect('class_list')
    else:
        target = faculty

    # ── Semesters ────────────────────────────────────────────
    all_semesters = (
        models.Semester.objects
        .select_related('academic_year')
        .annotate(
            offering_count=Count(
                'offerings',
                filter=Q(offerings__is_active=True, offerings__subject__is_active=True)
            )
        )
        .order_by('-academic_year__year_label', 'semester')
    )
    active_semester = None
    if semester_q:
        active_semester = all_semesters.filter(pk=semester_q).first()
    if active_semester is None:
        active_semester = models.Semester.get_current() or all_semesters.first()

    # ── Target section ───────────────────────────────────────
    all_sections = models.YearSection.objects.all().order_by('year_level', 'section')
    target_section = None
    if section_q and not section_q.startswith('dummy-'):
        target_section = all_sections.filter(pk=section_q).first()

    if not target_section:
        default_qs = all_sections
        if course_q:
            default_qs = default_qs.filter(course_code=course_q)
        if major_q:
            default_qs = default_qs.filter(major=major_q)
        target_section = default_qs.first() or all_sections.first()

    if target_section:
        if not course_q:
            course_q = target_section.course_code
        if not major_q:
            major_q = getattr(target_section, 'major', '')
        if not year_q:
            year_q = target_section.year_level
        if not sec_name_q:
            sec_name_q = target_section.section

    # ── Build 8 buttons ──────────────────────────────────────
    TARGET_COMBOS = [
        ('1', 'A'), ('1', 'B'), ('2', 'A'), ('2', 'B'),
        ('3', 'A'), ('3', 'B'), ('4', 'A'), ('4', 'B'),
    ]

    class DummySection:
        def __init__(self, y, s, course, major):
            self.id = f"dummy-{y}-{s}"
            self.year_level = y
            self.section = s
            self.course_code = course
            self.major = major

    unique_sections = []
    for year, sec in TARGET_COMBOS:
        match = all_sections.filter(
            year_level=year,
            section=sec,
            course_code=course_q,
            major=major_q,
        ).first()
        if match:
            unique_sections.append(match)
        else:
            unique_sections.append(DummySection(year, sec, course_q, major_q))

    # ── Schedules (semester-scoped, no removed `faculty` field) ──
    schedules_qs = (
        models.ClassSchedule.objects
        .filter(
            offering__subject__faculty=target,
            offering__subject__is_active=True,
            offering__is_active=True,
        )
        .select_related('offering__subject__faculty', 'room')
        .order_by('day', 'start_time')
    )
    if active_semester is not None:
        schedules_qs = schedules_qs.filter(offering__semester=active_semester)

    if target_section and not str(target_section.id).startswith('dummy'):
        schedules_qs = schedules_qs.filter(offering__subject__year_section=target_section)
    elif year_q and sec_name_q:
        schedules_qs = schedules_qs.filter(
            offering__subject__year_section__year_level=year_q,
            offering__subject__year_section__section=sec_name_q,
        )

    schedules = list(schedules_qs)

    # ── 30-min grid (Mon–Sat) ────────────────────────────────
    DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY']
    start_t = datetime.strptime('07:00', '%H:%M')
    end_t = datetime.strptime('19:00', '%H:%M')
    DUMMY_DATE = datetime(2000, 1, 1)

    PALETTE = ['bg-blue', 'bg-pink', 'bg-green', 'bg-yellow', 'bg-purple', 'bg-orange', 'bg-teal']
    subject_color = {}
    ci = 0
    for s in schedules:
        subj = s.offering.subject if s.offering else None
        code = subj.subject_code if subj else None
        if code and code not in subject_color:
            subject_color[code] = PALETTE[ci % len(PALETTE)]
            ci += 1

    schedules_by_time = {}

    def _inject(day, start_dt, end_dt, info):
        slot_dt = start_dt
        slot_keys = []
        while slot_dt < end_dt:
            slot_keys.append(slot_dt.strftime('%H:%M'))
            slot_dt += timedelta(minutes=30)
        total = len(slot_keys)
        for idx, key in enumerate(slot_keys):
            day_map = schedules_by_time.setdefault(key, {})
            if day in day_map:
                continue
            day_map[day] = {
                'info': info,
                'is_start': (idx == 0),
                'rowspan': total if idx == 0 else 0,
            }

    for s in schedules:
        subj = s.offering.subject if s.offering else None
        start_dt = datetime.combine(DUMMY_DATE, s.start_time)
        end_dt = datetime.combine(DUMMY_DATE, s.end_time)
        info = {
            'subject_code': subj.subject_code if subj else '',
            'faculty_name': subj.faculty.full_name if subj and subj.faculty else '',
            'room': s.room.room_code if s.room else '',
            'color': subject_color.get(subj.subject_code if subj else '', 'bg-blue'),
        }
        _inject((s.day or '').upper(), start_dt, end_dt, info)

    time_slots = []
    current = start_t
    while current < end_t:
        time_str = current.strftime('%H:%M')
        display_str = current.strftime('%I:%M').lstrip('0')

        if time_str == '12:00':
            noon_end = current + timedelta(hours=1)
            time_slots.append({
                'key': time_str,
                'display': f'{display_str} - {noon_end.strftime("%I:%M").lstrip("0")}',
                'is_lunch': True,
                'monday': None, 'tuesday': None, 'wednesday': None,
                'thursday': None, 'friday': None, 'saturday': None,
            })
            current = noon_end
            continue

        next_time = current + timedelta(minutes=30)
        slot = {
            'key': time_str,
            'display': f'{display_str} - {next_time.strftime("%I:%M").lstrip("0")}',
            'is_lunch': False,
            'monday': None, 'tuesday': None, 'wednesday': None,
            'thursday': None, 'friday': None, 'saturday': None,
        }
        if time_str in schedules_by_time:
            for day in DAYS:
                if day in schedules_by_time[time_str]:
                    slot[day.lower()] = schedules_by_time[time_str][day]

        time_slots.append(slot)
        current = next_time

    # ── Header context ───────────────────────────────────────
    course_code = target_section.course_code if target_section else course_q
    year_level = year_q or (target_section.year_level if target_section else '—')
    section_name = sec_name_q or (target_section.section if target_section else '—')
    major = target_section.major if target_section else major_q
    if not major:
        major = 'None'

    if active_semester:
        raw = str(active_semester)
        raw = re.sub(r'(S\.Y\.|SY)\s*\d{4}[-\s]?\d{4}', '', raw, flags=re.IGNORECASE)
        sem_display = re.sub(r'\d{4}[-\s]?\d{4}', '', raw).strip(', ')
    else:
        sem_display = "Current Semester"

    # ── Semesters dropdown (regex-stripped, matches room_list) ──
    import re
    clean_semesters = []
    for sem in all_semesters:
        raw_name = str(sem)
        short_name = re.sub(r'(S\.Y\.|SY)\s*\d{4}[-\s]?\d{4}', '', raw_name, flags=re.IGNORECASE)
        short_name = re.sub(r'\d{4}[-\s]?\d{4}', '', short_name).strip(', ')
        clean_semesters.append({'id': sem.id, 'short_name': short_name})

    context = {
        'faculty': faculty,
        'viewing_faculty': target,
        'course_list': sorted({s.course_code for s in all_sections if s.course_code}),
        'major_list': sorted({
            m for m in all_sections.exclude(major='').values_list('major', flat=True) if m
        }),
        'year_section_list': unique_sections,
        'sections': unique_sections,
        'section': target_section,
        'time_slots': time_slots,
        'days': DAYS,
        'course_code': course_code,
        'year_level': year_level,
        'section_name': section_name,
        'major': major,
        'course_q': course_q,
        'major_q': major_q,
        'signatures': _get_class_list_signatures(),
        'clean_semesters': clean_semesters,
        'active_semester': active_semester,
        'sem_display': sem_display,
        'semester_q': semester_q,
    }
    return render(request, 'class_list.html', context)
    
    
# ============================================================
# OTHER FUNCTIONS & DESIGNATION
# ============================================================

def other_assignment_list(request, employee_id):
    """Return all other-function/designation entries for a faculty (semester-scoped)."""
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_other_assignment(current, target):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        # ── Semester resolution (?semester= → current) ──
        semester_q = request.GET.get('semester', '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        qs = models.FacultyOtherAssignment.objects.filter(
            faculty=target, is_active=True
        ).select_related('room').order_by('category', 'work_type')
        if active_semester is not None:
            qs = qs.filter(semester=active_semester)

        data = [{
            'id': a.id,
            'category': a.category,
            'work_type': a.work_type,
            'work_type_display': a.get_work_type_display(),
            'title': a.title,
            'hours_per_week': float(a.hours_per_week or 0),
            'room_id': a.room_id,
            'room_code': a.room.room_code if a.room else '',
            'day': a.day or '',
            'start_time': a.start_time.strftime('%H:%M') if a.start_time else '',
            'end_time': a.end_time.strftime('%H:%M') if a.end_time else '',
            'remarks': a.remarks or '',
        } for a in qs]

        return JsonResponse({
            'assignments': data,
            'can_edit': True,
            'semester_id': active_semester.id if active_semester else None,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def other_assignment_create(request, employee_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_other_assignment(current, target):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        category = (request.POST.get('category') or '').strip().upper()
        work_type = (request.POST.get('work_type') or '').strip().upper()
        title = (request.POST.get('title') or '').strip()
        hours = request.POST.get('hours_per_week') or 0
        room_id = request.POST.get('room_id') or None
        day = (request.POST.get('day') or '').strip().upper() or None
        start_str = request.POST.get('start_time') or None
        end_str = request.POST.get('end_time') or None
        remarks = (request.POST.get('remarks') or '').strip()

        if category not in ('OTHER_FUNCTION', 'DESIGNATION'):
            return JsonResponse({'error': 'Invalid category.'}, status=400)
        if work_type not in dict(models.FacultyOtherAssignment.WORK_TYPE_CHOICES):
            return JsonResponse({'error': 'Invalid work type.'}, status=400)
        if not title:
            return JsonResponse({'error': 'Title is required.'}, status=400)

        # ── Semester (required) ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        room = None
        if room_id:
            room = get_object_or_404(models.Room, id=room_id)

        start_time = datetime.strptime(start_str, '%H:%M').time() if start_str else None
        end_time = datetime.strptime(end_str, '%H:%M').time() if end_str else None

        conflict_msg = _check_other_assignment_conflict(
            faculty=target,
            day=day,
            start_time=start_time,
            end_time=end_time,
            semester=active_semester,
        )
        if conflict_msg:
            return JsonResponse({'error': conflict_msg}, status=400)

        obj = models.FacultyOtherAssignment(
            faculty=target,
            semester=active_semester,
            category=category,
            work_type=work_type,
            title=title,
            hours_per_week=hours or 0,
            room=room,
            day=day,
            start_time=start_time,
            end_time=end_time,
            remarks=remarks,
            is_active=True,
            created_by=current,
        )
        obj.full_clean()
        obj.save()

        return JsonResponse({'success': True, 'id': obj.id})

    except ValidationError as e:
        msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
        return JsonResponse({'error': msg}, status=400)
    except models.FacultyProfile.DoesNotExist:
        return JsonResponse({'error': 'Faculty not found'}, status=404)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def other_assignment_update(request, employee_id, assignment_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_other_assignment(current, target):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        obj = get_object_or_404(
            models.FacultyOtherAssignment, id=assignment_id, faculty=target
        )

        category = (request.POST.get('category') or obj.category).strip().upper()
        work_type = (request.POST.get('work_type') or obj.work_type).strip().upper()
        title = (request.POST.get('title') or obj.title).strip()
        hours = request.POST.get('hours_per_week', obj.hours_per_week) or 0
        room_id = request.POST.get('room_id') or None
        day = (request.POST.get('day') or '').strip().upper() or None
        start_str = request.POST.get('start_time') or None
        end_str = request.POST.get('end_time') or None
        remarks = (request.POST.get('remarks') or '').strip()

        if category not in ('OTHER_FUNCTION', 'DESIGNATION'):
            return JsonResponse({'error': 'Invalid category.'}, status=400)
        if work_type not in dict(models.FacultyOtherAssignment.WORK_TYPE_CHOICES):
            return JsonResponse({'error': 'Invalid work type.'}, status=400)
        if not title:
            return JsonResponse({'error': 'Title is required.'}, status=400)

        # ── Semester: POST/GET → object's own → current ──
        semester_q = (request.POST.get('semester') or '').strip() or (request.GET.get('semester') or '').strip()
        active_semester = None
        if semester_q:
            active_semester = models.Semester.objects.filter(pk=semester_q).first()
        if active_semester is None and obj.semester_id:
            active_semester = obj.semester
        if active_semester is None:
            active_semester = models.Semester.get_current()
        if active_semester is None:
            return JsonResponse({'error': 'No active semester configured.'}, status=409)

        start_time = datetime.strptime(start_str, '%H:%M').time() if start_str else None
        end_time = datetime.strptime(end_str, '%H:%M').time() if end_str else None

        conflict_msg = _check_other_assignment_conflict(
            faculty=target,
            day=day,
            start_time=start_time,
            end_time=end_time,
            exclude_id=obj.id,
            semester=active_semester,
        )
        if conflict_msg:
            return JsonResponse({'error': conflict_msg}, status=400)

        obj.category = category
        obj.work_type = work_type
        obj.title = title
        obj.hours_per_week = hours or 0
        obj.room = get_object_or_404(models.Room, id=room_id) if room_id else None
        obj.day = day
        obj.start_time = start_time
        obj.end_time = end_time
        obj.remarks = remarks
        obj.semester = active_semester
        obj.full_clean()
        obj.save()

        return JsonResponse({'success': True})

    except ValidationError as e:
        msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
        return JsonResponse({'error': msg}, status=400)
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)


def other_assignment_delete(request, employee_id, assignment_id):
    if not request.session.get('faculty_id'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_other_assignment(current, target):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        obj = get_object_or_404(
            models.FacultyOtherAssignment, id=assignment_id, faculty=target
        )
        obj.delete()
        return JsonResponse({'success': True})

    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse({'error': str(e)}, status=500)

# ============================================================
# STUDENT COUNTS — per-subject and grand-total (DB-backed)
# ============================================================

def _can_edit_students(requesting_faculty, target_faculty):
    """Chairman can edit anyone's; a faculty can edit only their own."""
    return (
        requesting_faculty.is_chairman
        or requesting_faculty.employee_id == target_faculty.employee_id
    )


@require_POST
def update_subject_students(request, employee_id, subject_id):
    """
    Save the per-row student count for a single subject.
    Body: {"students": 42}
    """
    if not request.session.get('faculty_id'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=401)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_students(current, target):
            return JsonResponse(
                {'success': False, 'error': 'Permission denied'}, status=403
            )

        subject = get_object_or_404(
            models.Subject, pk=subject_id, faculty=target, is_active=True
        )

        try:
            payload = json.loads(request.body.decode('utf-8'))
            students = int(payload.get('students', 0))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return JsonResponse(
                {'success': False, 'error': f'Invalid value: {e}'}, status=400
            )

        if students < 0:
            return JsonResponse(
                {'success': False, 'error': 'Students must be >= 0.'}, status=400
            )

        subject.total_students = students
        subject.save(update_fields=['total_students'])

        total = (
            models.Subject.objects
            .filter(faculty=target, is_active=True)
            .aggregate(s=Sum('total_students'))['s'] or 0
        )

        return JsonResponse({
            'success': True,
            'subject_id': subject.id,
            'subject_students': subject.total_students,
            'total_students': total,
        })

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse(
            {'success': False, 'error': 'Faculty not found'}, status=404
        )
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse(
            {'success': False, 'error': str(e)}, status=500
        )


@require_POST
def update_total_students(request, employee_id):
    """
    Save the grand total for the faculty by redistributing the requested
    total across subjects proportionally (if any per-row values exist),
    or evenly (if all are zero).

    Body: {"total": 120}
    """
    if not request.session.get('faculty_id'):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=401)

    try:
        current = models.FacultyProfile.objects.get(
            employee_id=request.session['faculty_id']
        )
        target = get_object_or_404(models.FacultyProfile, employee_id=employee_id)

        if not _can_edit_students(current, target):
            return JsonResponse(
                {'success': False, 'error': 'Permission denied'}, status=403
            )

        try:
            payload = json.loads(request.body.decode('utf-8'))
            total = int(payload.get('total', 0))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return JsonResponse(
                {'success': False, 'error': f'Invalid value: {e}'}, status=400
            )

        if total < 0:
            return JsonResponse(
                {'success': False, 'error': 'Total must be >= 0.'}, status=400
            )

        with transaction.atomic():
            subjects = list(
                models.Subject.objects.select_for_update()
                .filter(faculty=target, is_active=True)
                .order_by('pk')
            )
            if not subjects:
                return JsonResponse(
                    {'success': False, 'error': 'No subjects to update.'}, status=400
                )

            current_sum = sum(s.total_students for s in subjects)
            if current_sum == total:
                # Nothing to change — don't touch the rows at all.
                return JsonResponse({'success': True, 'total_students': total})

            if current_sum > 0:
                # Proportional scaling; the last subject absorbs the rounding
                # remainder so the total always matches exactly.
                remaining = total
                for i, s in enumerate(subjects):
                    if i == len(subjects) - 1:
                        s.total_students = remaining
                    else:
                        share = round(s.total_students / current_sum * total)
                        share = max(0, min(share, remaining))
                        s.total_students = share
                        remaining -= share
                    s.save(update_fields=['total_students'])
            else:
                # Even split
                per = total // len(subjects)
                remainder = total - per * len(subjects)
                for i, s in enumerate(subjects):
                    s.total_students = per + (1 if i < remainder else 0)
                    s.save(update_fields=['total_students'])

        return JsonResponse({'success': True, 'total_students': total})

    except models.FacultyProfile.DoesNotExist:
        return JsonResponse(
            {'success': False, 'error': 'Faculty not found'}, status=404
        )
    except (ValidationError, IntegrityError, ValueError, TypeError) as e:
        return JsonResponse(
            {'success': False, 'error': str(e)}, status=500
        )

