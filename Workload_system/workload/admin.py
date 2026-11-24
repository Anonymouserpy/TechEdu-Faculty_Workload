from django.contrib import admin
from django.utils.html import format_html, mark_safe
from django.urls import reverse
from django.db.models import Count, Q, Sum
from . import models

# ==================== BASE ADMIN CONFIGURATIONS ====================

class BaseAdmin(admin.ModelAdmin):
    """Base admin class with common configurations"""
    save_on_top = True
    list_per_page = 25


# ==================== ACADEMIC YEAR / SEMESTER / OFFERING ADMIN ====================

@admin.register(models.AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ['year_label', 'is_current', 'semester_count']
    list_filter = ['is_current']
    search_fields = ['year_label']
    list_editable = ['is_current']
    list_select_related = True

    def semester_count(self, obj):
        return obj.semesters.count()
    semester_count.short_description = 'Semesters'


@admin.register(models.Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'academic_year', 'semester', 'is_active', 'offering_count']
    list_filter = ['academic_year', 'semester', 'is_active']
    search_fields = ['academic_year__year_label']
    autocomplete_fields = ['academic_year']
    list_select_related = ['academic_year']

    def offering_count(self, obj):
        count = obj.offerings.filter(is_active=True).count()
        if count > 0:
            url = (
                reverse('admin:workload_subjectoffering_changelist')
                + f'?semester__id__exact={obj.id}'
            )
            return mark_safe(f'<a href="{url}">{count} offerings</a>')
        return '0'
    offering_count.short_description = 'Active Offerings'


@admin.register(models.SubjectOffering)
class SubjectOfferingAdmin(admin.ModelAdmin):
    list_display = [
        'subject_display', 'semester', 'effective_faculty_display',
        'is_active', 'schedule_count',
    ]
    list_filter = [
        'semester__academic_year',
        'semester__semester',
        'subject__year_section__course_code',
        'subject__year_section__year_level',
        'is_active',
    ]
    search_fields = [
        'subject__subject_code',
        'subject__subject_description',
        'subject__year_section__course_code',
        'subject__year_section__section',
    ]
    # FIXED: Removed 'faculty' from autocomplete (field no longer exists)
    autocomplete_fields = ['subject', 'semester']
    list_select_related = ['subject', 'subject__faculty', 'semester']
    list_editable = ['is_active']
    inlines = []  # populated below
    fieldsets = (
        ('Offering', {
            'fields': ('subject', 'semester', 'is_active')
        }),
        ('Assignment', {
            # FIXED: Removed 'faculty'. Faculty is derived from Subject.
            'fields': ('total_students',)
        }),
        ('Notes', {
            'fields': ('notes',),
            'classes': ('collapse',)
        }),
    )
    actions = ['activate_offerings', 'deactivate_offerings', 'reset_offering_students']

    def subject_display(self, obj):
        return obj.subject.get_subject_display()
    subject_display.short_description = 'Subject'

    def effective_faculty_display(self, obj):
        # FIXED: Uses the new property that falls back to Subject.faculty
        f = obj.effective_faculty
        return f.full_name if f else '—'
    effective_faculty_display.short_description = 'Faculty'

    def schedule_count(self, obj):
        count = obj.schedules.count()
        if count > 0:
            url = (
                reverse('admin:workload_classschedule_changelist')
                + f'?offering__id__exact={obj.id}'
            )
            return mark_safe(f'<a href="{url}">{count} schedules</a>')
        return '0'
    schedule_count.short_description = 'Schedules'

    def activate_offerings(self, request, queryset):
        queryset.update(is_active=True)
    activate_offerings.short_description = "Activate Offerings"

    def deactivate_offerings(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_offerings.short_description = "Deactivate Offerings"

    def reset_offering_students(self, request, queryset):
        updated = queryset.update(total_students=0)
        self.message_user(request, f'Reset student counts for {updated} offering(s).')
    reset_offering_students.short_description = "Reset student counts to 0"


# ==================== FACULTY PROFILE ADMIN ====================

@admin.register(models.FacultyProfile)
class FacultyProfileAdmin(admin.ModelAdmin):
    list_display = [
        'employee_id', 'full_name', 'academic_rank', 'department', 'employment_type',
        'is_chairman', 'is_active', 'subject_count', 'student_count',
        'last_login',
    ]
    list_filter = ['department', 'employment_type', 'is_chairman', 'is_active']
    search_fields = ['employee_id', 'full_name', 'specialization', 'academic_rank']
    list_editable = ['is_active']
    readonly_fields = ['last_login', 'password']
    fieldsets = (
        ('Personal Information', {
            'fields': ('employee_id', 'full_name', 'specialization', 'attained_degree', 'academic_rank')
        }),
        ('Employment Details', {
            'fields': ('department', 'employment_type', 'is_chairman')
        }),
        ('Authentication', {
            'fields': ('password', 'is_active', 'last_login'),
            'classes': ('collapse',)
        }),
    )
    actions = ['make_chairman', 'remove_chairman', 'activate_faculty', 'deactivate_faculty']

    def subject_count(self, obj):
        # Uses the Prefetch cache if available
        count = obj.subjects.filter(is_active=True).count()
        if count > 0:
            url = reverse('admin:workload_subject_changelist') + f'?faculty__id__exact={obj.employee_id}'
            return mark_safe(f'<a href="{url}">{count} subjects</a>')
        return '0 subjects'
    subject_count.short_description = 'Subjects'

    def student_count(self, obj):
        """Sum of total_students across this faculty's active SUBJECT OFFERINGS."""
        total = (
            models.SubjectOffering.objects
            .filter(subject__faculty=obj, subject__is_active=True, is_active=True)
            .aggregate(s=Sum('total_students'))['s'] or 0
        )
        if total > 0:
            url = (
                reverse('admin:workload_subjectoffering_changelist')
                + f'?subject__faculty__id__exact={obj.employee_id}&is_active__exact=1'
            )
            return mark_safe(f'<a href="{url}">👥 {total}</a>')
        return '0'
    student_count.short_description = 'Students'

    def make_chairman(self, request, queryset):
        queryset.update(is_chairman=True)
    make_chairman.short_description = "Set as Chairman"

    def remove_chairman(self, request, queryset):
        queryset.update(is_chairman=False)
    remove_chairman.short_description = "Remove as Chairman"

    def activate_faculty(self, request, queryset):
        queryset.update(is_active=True)
    activate_faculty.short_description = "Activate Faculty"

    def deactivate_faculty(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_faculty.short_description = "Deactivate Faculty"


# ==================== YEAR SECTION ADMIN ====================

@admin.register(models.YearSection)
class YearSectionAdmin(admin.ModelAdmin):
    list_display = [
        'course_code', 'year_level', 'section', 'major',
        'subject_count', 'student_count',
    ]
    list_filter = ['course_code', 'year_level', 'major']
    search_fields = ['course_code', 'major', 'section']
    ordering = ['course_code', 'year_level', 'section']

    def subject_count(self, obj):
        count = obj.subjects.filter(is_active=True).count()
        if count > 0:
            url = reverse('admin:workload_subject_changelist') + f'?year_section__id__exact={obj.id}'
            return mark_safe(f'<a href="{url}">{count} subjects</a>')
        return '0 subjects'
    subject_count.short_description = 'Active Subjects'

    def student_count(self, obj):
        """Sum of total_students across this section's active SUBJECT OFFERINGS."""
        total = (
            models.SubjectOffering.objects
            .filter(
                subject__year_section=obj,
                subject__is_active=True,
                is_active=True,
            )
            .aggregate(s=Sum('total_students'))['s'] or 0
        )
        if total > 0:
            url = (
                reverse('admin:workload_subjectoffering_changelist')
                + f'?subject__year_section__id__exact={obj.id}&is_active__exact=1'
            )
            return mark_safe(f'<a href="{url}">👥 {total}</a>')
        return '0'
    student_count.short_description = 'Students'


# ==================== ROOM ADMIN ====================

class RoomScheduleInline(admin.TabularInline):
    model = models.ClassSchedule
    fk_name = 'room'
    extra = 0
    fields = ['offering', 'day', 'start_time', 'end_time', 'schedule_type']
    autocomplete_fields = ['offering']
    readonly_fields = ['created_at']
    show_change_link = True
    max_num = 20


@admin.register(models.Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['room_code', 'room_name', 'program', 'building_name', 'floor_number',
                   'is_available', 'schedule_count', 'utilization_percentage']
    list_filter = ['program', 'building_name', 'is_available']
    search_fields = ['room_code', 'room_name', 'building_name']
    list_editable = ['is_available']
    inlines = [RoomScheduleInline]
    fieldsets = (
        ('Room Information', {
            'fields': ('room_code', 'room_name', 'program', 'building_name', 'floor_number')
        }),
        ('Availability', {
            'fields': ('is_available',)
        }),
    )
    actions = ['mark_available', 'mark_unavailable']

    def schedule_count(self, obj):
        count = obj.schedules.count()
        if count > 0:
            url = reverse('admin:workload_classschedule_changelist') + f'?room__id__exact={obj.id}'
            return mark_safe(f'<a href="{url}">{count} schedules</a>')
        return '0'
    schedule_count.short_description = 'Schedules'

    def utilization_percentage(self, obj):
        try:
            utilization = models.RoomUtilizationCalculator.calculate_room_utilization(obj)
            utilization = float(utilization) if utilization is not None else 0
        except Exception:
            utilization = 0

        if utilization > 0:
            if utilization >= 70:
                color = 'green'
            elif utilization >= 40:
                color = 'orange'
            else:
                color = 'red'
            return mark_safe(
                f'<span style="background: {color}; color: white; padding: 2px 8px; '
                f'border-radius: 3px;">{utilization:.1f}%</span>'
            )
        return mark_safe('<span style="color: gray;">N/A</span>')
    utilization_percentage.short_description = 'Utilization'

    def mark_available(self, request, queryset):
        queryset.update(is_available=True)
    mark_available.short_description = "Mark as Available"

    def mark_unavailable(self, request, queryset):
        queryset.update(is_available=False)
    mark_unavailable.short_description = "Mark as Unavailable"


# ==================== SUBJECT ADMIN ====================

class SubjectOfferingInline(admin.TabularInline):
    """Offerings on the Subject page — the real owner of a class schedule."""
    model = models.SubjectOffering
    fk_name = 'subject'
    extra = 0
    fields = ['semester', 'total_students', 'is_active']
    autocomplete_fields = ['semester']
    show_change_link = True
    max_num = 40


@admin.register(models.Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = [
        'subject_code',
        'subject_description_short',
        'year_section',
        'faculty',
        # FIXED: Removed 'has_lecture' and 'is_laboratory' from list (now properties)
        'lecture_hours',
        'lab_hours',
        'total_hours',
        'total_students',
        'is_active',
    ]
    list_filter = [
        'year_section__course_code',
        'year_section__year_level',
        'faculty',
        # FIXED: Removed 'has_lecture' and 'is_laboratory' (cannot filter on properties)
        'is_active',
    ]
    search_fields = [
        'subject_code',
        'subject_description',
        'year_section__course_code',
        'year_section__major',
        'year_section__section',
    ]
    list_editable = ['total_students', 'is_active']
    inlines = [SubjectOfferingInline]
    autocomplete_fields = ['year_section', 'faculty']
    fieldsets = (
        ('Subject Identification', {
            'fields': ('year_section', 'subject_code', 'subject_description')
        }),
        ('Hours Breakdown', {
            'fields': ('lecture_hours_per_week', 'lab_hours_per_week')
        }),
        ('Assignment', {
            'fields': ('faculty',)
        }),
        ('Enrolment (default)', {
            'fields': ('total_students',),
            'description': (
                'Default enrollment. Per-semester enrollment is set on the '
                'SubjectOffering rows below. The Subject is retained across '
                'academic years; only SubjectOfferings are recreated.'
            ),
        }),
        ('Status', {
            # FIXED: Removed 'has_lecture' and 'is_laboratory' from editable fields
            'fields': ('is_active', 'allow_cohort_overlap')
        }),
    )
    actions = ['activate_subjects', 'deactivate_subjects', 'reset_student_counts']

    def subject_description_short(self, obj):
        if not obj.subject_description:
            return '-'
        return obj.subject_description[:50] + '...' if len(obj.subject_description) > 50 else obj.subject_description
    subject_description_short.short_description = 'Description'

    def lecture_hours(self, obj):
        return obj.lecture_hours_per_week
    lecture_hours.short_description = 'Lecture Hrs'

    def lab_hours(self, obj):
        return obj.lab_hours_per_week
    lab_hours.short_description = 'Lab Hrs'

    def total_hours(self, obj):
        return obj.total_hours_per_week
    total_hours.short_description = 'Total Hrs'

    def total_students(self, obj):
        return obj.total_students
    total_students.short_description = 'Students'
    total_students.admin_order_field = 'total_students'

    def activate_subjects(self, request, queryset):
        queryset.update(is_active=True)
    activate_subjects.short_description = "Activate Subjects"

    def deactivate_subjects(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_subjects.short_description = "Deactivate Subjects"

    def reset_student_counts(self, request, queryset):
        updated = queryset.update(total_students=0)
        self.message_user(
            request,
            f'Reset student counts for {updated} subject(s).',
        )
    reset_student_counts.short_description = "Reset student counts to 0"


# ==================== CLASS SCHEDULE ADMIN ====================

@admin.register(models.ClassSchedule)
class ClassScheduleAdmin(admin.ModelAdmin):
    list_display = [
        'schedule_display', 'faculty_display', 'subject_code_display',
        'semester_display', 'room_display', 'day', 'time_display',
        'schedule_type', 'created_by',
    ]
    list_filter = [
        'day', 'schedule_type', 'room',
        # FIXED: Removed 'faculty' (field deleted from model)
        'offering__semester__academic_year',
        'offering__semester__semester',
    ]
    search_fields = [
        'offering__subject__subject_code',
        'offering__subject__subject_description',
        'offering__subject__year_section__course_code',
        'offering__subject__faculty__full_name', # FIXED: Changed traversal
        'room__room_code',
    ]
    date_hierarchy = 'created_at'
    readonly_fields = ['created_at', 'updated_at']
    # FIXED: Removed 'faculty' from autocomplete (field deleted)
    autocomplete_fields = ['offering', 'room']
    list_select_related = ['offering__subject', 'offering__semester', 'offering__subject__faculty', 'room']
    
    fieldsets = (
        ('Schedule Information', {
            # FIXED: Removed 'faculty' from fields
            'fields': ('offering', 'room', 'day', 'start_time', 'end_time')
        }),
        ('Schedule Type', {
            'fields': ('schedule_type',)
        }),
        ('Additional Information', {
            'fields': ('notes',)
        }),
        ('Audit Information', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    actions = ['export_schedule']

    @admin.display(description='Schedule')
    def schedule_display(self, obj):
        return obj.subject.get_subject_display()

    @admin.display(description='Faculty')
    def faculty_display(self, obj):
        f = obj.assigned_faculty
        return f.full_name if f else '—'

    @admin.display(description='Subject')
    def subject_code_display(self, obj):
        return obj.subject.subject_code

    @admin.display(description='Semester')
    def semester_display(self, obj):
        return str(obj.offering.semester)

    @admin.display(description='Room')
    def room_display(self, obj):
        return obj.room.room_code

    @admin.display(description='Time')
    def time_display(self, obj):
        return f"{obj.start_time.strftime('%I:%M %p')} - {obj.end_time.strftime('%I:%M %p')}"

    def export_schedule(self, request, queryset):
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="schedules_export.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Subject', 'Section', 'Semester', 'Faculty', 'Room', 'Day',
            'Start Time', 'End Time', 'Type', 'Students',
        ])

        for schedule in queryset.select_related(
            'offering__subject', 'offering__semester', 'room'
        ):
            subj = schedule.offering.subject
            fac = schedule.assigned_faculty
            writer.writerow([
                subj.subject_code,
                subj.get_subject_display(),
                str(schedule.offering.semester),
                fac.full_name if fac else '',
                schedule.room.room_code,
                schedule.day,
                schedule.start_time.strftime('%H:%M'),
                schedule.end_time.strftime('%H:%M'),
                schedule.schedule_type,
                schedule.offering.total_students or subj.total_students or 0,
            ])

        return response
    export_schedule.short_description = "Export selected as CSV"

    def save_model(self, request, obj, form, change):
        if not change:
            # FIXED: Safely get the created_by from session since request.user is AnonymousUser
            faculty_id = request.session.get('faculty_id')
            if faculty_id:
                try:
                    obj.created_by = models.FacultyProfile.objects.get(employee_id=faculty_id)
                except models.FacultyProfile.DoesNotExist:
                    pass
        super().save_model(request, obj, form, change)


# ==================== FACULTY OTHER ASSIGNMENT ADMIN ====================

@admin.register(models.FacultyOtherAssignment)
class FacultyOtherAssignmentAdmin(admin.ModelAdmin):
    list_display = [
        'faculty', 'get_work_type_display', 'title', 'semester',
        'day', 'start_time', 'end_time', 'duration_hours', 'is_active',
    ]
    list_filter = [
        'category', 'work_type',
        'semester__academic_year', 'semester__semester',
        'is_active',
    ]
    search_fields = ['faculty__full_name', 'title']
    autocomplete_fields = ['faculty', 'room', 'semester']
    list_select_related = ['faculty', 'semester', 'room']

    @admin.display(description='Hours')
    def duration_hours(self, obj):
        return obj.duration_hours()


# ==================== ADMIN SITE CUSTOMIZATION ====================

admin.site.site_header = "Faculty Workload System Administration"
admin.site.site_title = "Faculty Workload System"
admin.site.index_title = "Welcome to Faculty Workload System Administration"