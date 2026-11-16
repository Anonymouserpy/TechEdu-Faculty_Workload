# context_processors.py
from . import models

def sidebar_context(request):
    """Context processor to provide sidebar data to all templates"""
    context = {}
    
    if request.session.get('faculty_id'):
        try:
            faculty = models.FacultyProfile.objects.get(
                employee_id=request.session['faculty_id'],
                is_active=True
            )
            
            context['faculty'] = faculty
            
            # Get counts for sidebar badges
            context['subject_count'] = models.Subject.objects.filter(
                faculty=faculty, 
                is_active=True
            ).count()
            
            context['class_program_count'] = models.ClassSchedule.objects.filter(
                subject__faculty=faculty
            ).count()
            
            context['room_count'] = models.Room.objects.filter(
                is_available=True
            ).count()
            
            if faculty.is_chairman:
                context['total_faculty'] = models.FacultyProfile.objects.filter(
                    is_active=True
                ).count()
                context['total_subjects_all'] = models.Subject.objects.filter(
                    is_active=True
                ).count()
            
        except models.FacultyProfile.DoesNotExist:
            pass
    
    return context