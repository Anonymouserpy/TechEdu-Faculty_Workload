from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html, mark_safe
from django.urls import reverse
from django.db.models import Count, Q, Sum
from django.utils import timezone
from . import models


# ==================== BASE ADMIN CONFIGURATIONS ====================

class BaseAdmin(admin.ModelAdmin):
    """Base admin class with common configurations"""
    save_on_top = True
    list_per_page = 25


# ==================== FACULTY PROFILE ADMIN ====================

@admin.register(models.FacultyProfile)
class FacultyProfileAdmin(admin.ModelAdmin):
    list_display = [
        'employee_id', 'full_name', 'department', 'employment_type',
        'is_chairman', 'is_active', 'subject_count', 'student_count',
        'last_login',
    ]
    list_filter = ['department', 'employment_type', 'is_chairman', 'is_active']
    search_fields = ['employee_id', 'full_name', 'specialization']
    list_editable = ['is_active']
    readonly_fields = ['last_login', 'password']
    fieldsets = (
        ('Personal Information', {
            'fields': ('employee_id', 'full_name', 'specialization', 'attained_degree')
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
        count = obj.subjects.filter(is_active=True).count()
        if count > 0:
            url = reverse('admin:workload_subject_changelist') + f'?faculty__id__exact={obj.employee_id}'
            return mark_safe(f'<a href="{url}">{count} subjects</a>')
        return '0 subjects'
    subject_count.short_description = 'Subjects'

    def student_count(self, obj):
        """Sum of total_students across this faculty's active subjects."""
        total = (
            obj.subjects.filter(is_active=True)
            .aggregate(s=Sum('total_students'))['s'] or 0
        )
        if total > 0:
            url = reverse('admin:workload_subject_changelist') + (
                f'?faculty__id__exact={obj.employee_id}&is_active__exact=1'
            )
            return mark_safe(f'<a href="{url}">👥 {total}</a>')
        return '0'
    student_count.short_description = 'Students'
    student_count.admin_order_field = 'subjects__total_students'

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
        """Sum of total_students across this section's active subjects."""
        total = (
            obj.subjects.filter(is_active=True)
            .aggregate(s=Sum('total_students'))['s'] or 0
        )
        if total > 0:
            url = reverse('admin:workload_subject_changelist') + (
                f'?year_section__id__exact={obj.id}&is_active__exact=1'
            )
            return mark_safe(f'<a href="{url}">👥 {total}</a>')
        return '0'
    student_count.short_description = 'Students'
    student_count.admin_order_field = 'subjects__total_students'


# ==================== ROOM ADMIN ====================

class RoomScheduleInline(admin.TabularInline):
    model = models.ClassSchedule
    fk_name = 'room'
    extra = 0
    fields = ['subject', 'day', 'start_time', 'end_time', 'schedule_type']
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
        """Calculate and display room utilization"""
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

class SubjectScheduleInline(admin.TabularInline):
    model = models.ClassSchedule
    fk_name = 'subject'
    extra = 0
    fields = ['faculty', 'room', 'day', 'start_time', 'end_time', 'schedule_type']
    readonly_fields = ['created_at']
    show_change_link = True
    max_num = 20


@admin.register(models.Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = [
        'subject_code',
        'subject_description_short',
        'year_section',
        'faculty',
        'has_lecture',
        'is_laboratory',
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
        'has_lecture',
        'is_laboratory',
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
    inlines = [SubjectScheduleInline]
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
        ('Enrolment', {
            'fields': ('total_students',),
            'description': (
                'Total number of students enrolled in this class. '
                'Displayed on the Instructor\'s Class Program and '
                'visible to the chairman in All Class Programs.'
            ),
        }),
        ('Status', {
            'fields': ('has_lecture', 'is_laboratory', 'is_active', 'allow_cohort_overlap')
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
        """Bulk-reset the enrolled student count to 0 on selected subjects."""
        updated = queryset.update(total_students=0)
        self.message_user(
            request,
            f'Reset student counts for {updated} subject(s).',
        )
    reset_student_counts.short_description = "Reset student counts to 0"


# ==================== CLASS SCHEDULE ADMIN ====================

@admin.register(models.ClassSchedule)
class ClassScheduleAdmin(admin.ModelAdmin):
    list_display = ['schedule_display', 'faculty_display', 'subject_code_display',
                   'room_display', 'day', 'time_display', 'schedule_type', 'created_by']
    list_filter = ['day', 'schedule_type', 'faculty', 'room']
    search_fields = ['subject__subject_code', 'faculty__full_name', 'room__room_code']
    date_hierarchy = 'created_at'
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['subject', 'faculty', 'room']
    fieldsets = (
        ('Schedule Information', {
            'fields': ('subject', 'faculty', 'room', 'day', 'start_time', 'end_time')
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

    def schedule_display(self, obj):
        return obj.subject.get_subject_display()
    schedule_display.short_description = 'Schedule'

    def faculty_display(self, obj):
        return obj.assigned_faculty.full_name
    faculty_display.short_description = 'Faculty'

    def subject_code_display(self, obj):
        return obj.subject.subject_code
    subject_code_display.short_description = 'Subject'

    def room_display(self, obj):
        return obj.room.room_code
    room_display.short_description = 'Room'

    def time_display(self, obj):
        return f"{obj.start_time.strftime('%I:%M %p')} - {obj.end_time.strftime('%I:%M %p')}"
    time_display.short_description = 'Time'

    def export_schedule(self, request, queryset):
        """Export selected schedules as CSV."""
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="schedules_export.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Subject', 'Section', 'Faculty', 'Room', 'Day',
            'Start Time', 'End Time', 'Type', 'Students',
        ])

        for schedule in queryset.select_related('subject', 'faculty', 'room'):
            writer.writerow([
                schedule.subject.subject_code,
                schedule.subject.get_subject_display(),
                schedule.assigned_faculty.full_name,
                schedule.room.room_code,
                schedule.day,
                schedule.start_time.strftime('%H:%M'),
                schedule.end_time.strftime('%H:%M'),
                schedule.schedule_type,
                schedule.subject.total_students or 0,
            ])

        return response
    export_schedule.short_description = "Export selected as CSV"

    def save_model(self, request, obj, form, change):
        if not change:
            if hasattr(request.user, 'facultyprofile'):
                obj.created_by = request.user.facultyprofile
        super().save_model(request, obj, form, change)


# ==================== ADMIN SITE CUSTOMIZATION ====================

admin.site.site_header = "Faculty Workload System Administration"
admin.site.site_title = "Faculty Workload System"
admin.site.index_title = "Welcome to Faculty Workload System Administration"